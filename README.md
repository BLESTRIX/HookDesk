# HookDesk

A multi-tenant webhook inbox for a simulated shipment courier, built with Python 3.12, FastAPI, Jinja2, HTMX, Bootstrap 5, SQLAlchemy, PostgreSQL 17, Redis, Celery and Alembic. PostgreSQL stores both accepted events and pending work. Redis is a delivery mechanism for workers.

The application includes registration, cookie sessions, organization roles, source configuration and encrypted secret rotation, an event inbox, processing histories, shipment histories, notifications, audit history and safe replay. This is a working learning project; it is not a claim of production readiness.

## Run locally

Install Docker Desktop/Engine with Compose v2, Python 3.12, and [uv](https://docs.astral.sh/uv/). Run these commands from the repository root:

```powershell
python scripts/configure.py
uv sync --frozen
docker compose up --build -d
```

`configure.py` creates an ignored `.env` with random database credentials and a Fernet encryption key. It preserves any existing `.env`. The example file has no real secrets. Do not lose the encryption key: existing signing secrets need it to be decrypted.

Open **http://localhost:8000/register**, create an organization, then create a source. Copy the one-time signing secret into a password manager or your simulator environment. Open **http://localhost:8000/docs** for the management and ingestion API. All frontend assets, including Swagger UI, are served locally; no CDN is required at runtime.

```powershell
docker compose ps
docker compose logs -f web worker beat
docker compose run --rm migrate
docker compose stop
```

The database persists in the `postgres-data` volume. Stopping services preserves it. Migrations run through a one-shot service before web/worker/Beat start. Application containers share one image and run as an unprivileged user. Only local loopback ports are published.

For local Python development, run PostgreSQL and Redis in Compose and start the application on the host:

```powershell
docker compose up -d postgres redis
uv run alembic upgrade head
uv run uvicorn app.main:app --reload --no-access-log
```

Run the worker and scheduler in Docker on Windows: Celery prefork workers are intended for Linux. A regular deployment uses separate web, worker and Beat processes. Keep one Beat scheduler for the deployment.

## Demo accounts and shipments

Demo mode is opt-in and rejected when `ENVIRONMENT=production`. Use the explicit override for faster retries and recovery:

```powershell
docker compose -f compose.yaml -f compose.demo.yaml up --build -d
docker compose -f compose.yaml -f compose.demo.yaml exec web uv run --frozen --no-dev python -m app.manage seed
```

The seed command securely prompts for a password of at least 12 characters, or reads `DEMO_PASSWORD` from the environment / local `.env`. No fixed password is embedded in the application. Use the chosen password for these accounts:

| Account | Organization | Role |
| --- | --- | --- |
| `admin@example.com` | Northstar Shop | ADMIN |
| `operator@example.com` | Northstar Shop | OPERATOR |
| `viewer@example.com` | Northstar Shop | VIEWER |
| `other@example.com` | Second Shop | ADMIN |

Each organization gets a Demo Courier source and `SHIP-1001` through `SHIP-1005`. Re-running seed preserves existing users, passwords, sources and shipment state. Source secrets are never printed by seed; use **Sources → Demo Courier → Rotate secret** to obtain a fresh secret once. The second organization demonstrates isolation even when shipment external IDs match.

The trusted administrator CLI can create additional memberships or shipments. Get organization and source IDs from the API or seed output:

```powershell
uv run python -m app.manage member --organization ORG_UUID --actor-email admin@example.com --email colleague@example.com --role OPERATOR
uv run python -m app.manage shipment --organization ORG_UUID --actor-email admin@example.com --source SOURCE_UUID --external-id SHIP-2001
```

The first command prompts for a new user's password, or uses `MEMBER_PASSWORD`. Existing users gain/update a membership without changing their password. Both commands check the actor's admin membership and scope resources to that organization. These are privileged host commands; `--actor-email` is attribution, not remote authentication. Do not grant host/container-shell access to ordinary users.

## Courier contract and simulator

Send `POST /webhooks/{public_source_id}` with these headers:

```text
Content-Type: application/json
X-HookDesk-Timestamp: <Unix seconds>
X-HookDesk-Signature: sha256=<lowercase hex digest>
```

The signature is:

```python
hmac.new(
    signing_secret.encode(),
    timestamp.encode("ascii") + b"." + exact_raw_request_body,
    hashlib.sha256,
).hexdigest()
```

The verifier compares signatures in constant time before parsing JSON. Default timestamp tolerance is 300 seconds in either direction. Each delivery retry must generate a fresh timestamp/signature while preserving **exactly the same event ID and body bytes**, including whitespace and key order. Signing reserialized JSON does not authenticate the received bytes.

```json
{
  "event_id": "evt_demo_001",
  "event_type": "shipment.status_changed",
  "occurred_at": "2026-10-16T03:00:00Z",
  "data": {
    "shipment_id": "SHIP-1001",
    "sequence": 1,
    "status": "shipped"
  }
}
```

`event_id` is a nonempty string of at most 128 characters. Shipment IDs have a 100-character limit. Sequence is a strictly positive integer; booleans and numeric strings are rejected. `occurred_at` must be an ISO timestamp string with a timezone. Status is `shipped` or `delivered`. Unsupported event types, unknown fields, duplicate JSON keys, non-finite JSON numbers and invalid UTF-8 are rejected. The body limit defaults to 256 KiB and is enforced while reading the stream, independently of `Content-Length`.

| Delivery | Response |
| --- | --- |
| New valid event durably committed | `202`, `duplicate: false` |
| Same ID and identical body, with fresh valid authentication | `202`, `duplicate: true` |
| Same ID, different bytes | `409 event_id_conflict` |
| Invalid signature / timestamp | `401` |
| Invalid envelope / duplicate keys | `422` |
| Oversized body | `413` |
| Wrong media type | `415` |
| Missing or inactive source | `404` |
| Database persistence failure | `503`; sender should retry |

Invalid deliveries never enter the accepted-event inbox. Rejection counters retain only fixed operational codes and counts, without untrusted bodies. They are available to a host administrator in `operational_counters`.

The simulator reads `HOOKDESK_SIGNING_SECRET` from the environment or prompts securely. It never prints the secret. Set the full endpoint URL from the source page:

```powershell
$endpoint = 'http://localhost:8000/webhooks/PUBLIC_SOURCE_ID'
uv run python -m simulator valid --url $endpoint --shipment SHIP-1001
uv run python -m simulator duplicate --url $endpoint --count 5
uv run python -m simulator invalid --url $endpoint
uv run python -m simulator stale --url $endpoint
uv run python -m simulator conflict --url $endpoint
uv run python -m simulator out-of-order --url $endpoint --shipment SHIP-1002
uv run python -m simulator concurrent --url $endpoint --count 20 --concurrency 10
uv run python -m simulator batch --url $endpoint --count 100 --concurrency 10
```

Duplicate/concurrent modes reuse one in-memory body for every delivery. Across separate CLI invocations, use `--body-file path/to/event.json` to preserve exact bytes. Merely reusing `--event-id` generates a different `occurred_at`, and therefore a conflict. Batch sizes are bounded at 1,000 requests and concurrency at 25. Synthetic shipments are not automatically created; batch events can demonstrate sequence handling for an existing shipment or unknown-shipment failures.

## Architecture and transactions

```mermaid
flowchart LR
    C[Courier simulator] -->|Raw signed JSON| W[FastAPI web]
    U[Operator browser] -->|Jinja / HTMX / JSON API| W
    W -->|Commit event + run + job| P[(PostgreSQL 17)]
    B[Celery Beat] -->|Periodic discovery task| R[(Redis broker)]
    R --> K[Celery worker]
    K -->|Discover queued, due, abandoned jobs| P
    K -->|Publish durable job IDs| R
    K -->|Claim with lease + token| P
    K --> H[Shipment handler]
    H -->|One transaction: shipment + effect + notification + success| P
```

Routes handle HTTP concerns. Services implement authorization, transactions and business rules. Repositories provide scoped resource lookups and pagination. The shipment handler contains business state rules; Celery tasks invoke the same processing services tested directly. The API and HTML forms share services. Each request/task gets one synchronous SQLAlchemy session.

**Acceptance:** the source is locked for shared reading, so rotation/deactivation cannot race through acceptance. A PostgreSQL unique constraint on `(source_id, external_event_id)` arbitrates concurrent deliveries. An `INSERT … ON CONFLICT` determines whether to create work; duplicates compare the original body hash. The new event, first run and durable job commit in a single transaction before the endpoint returns. There is no `.delay()` or Redis dependency in this transaction.

**Discovery:** Beat asks a worker to scan due PostgreSQL jobs periodically. Queued jobs, due retries and expired leases remain discoverable until completed. Publishing a job repeatedly is safe: workers claim the job with `FOR UPDATE SKIP LOCKED`. Redis loss, lost publication or worker death cannot erase the PostgreSQL job. After broker recovery, Beat publishes discovery again and work resumes.

**Processing:** claim commits a random ownership token, lease expiry and attempt record. The processing transaction locks the job, checks its ownership token/expiry, locks the shipment, applies effects, checks ownership/expiry again and commits. A stale worker cannot commit after another claim takes over; an expired transaction rolls back its local effects. The job row stays locked during the handler transaction. Handlers must finish within the lease; this version intentionally has no long-running external handler or lease-renewal path.

**Shipment rules:** lower sequence → `IGNORED`; equal sequence and same status → `IGNORED`; equal sequence and different status → permanent failure. A higher sequence can advance pending → shipped → delivered, including pending → delivered. A regression fails permanently. A higher sequence with the same status advances sequence without another status-change notification. Shipment identity is unique per source. Unknown shipments fail immediately, and can be created before replay.

**Local effects:** shipment updates, the status-change business-effect record, internal notification and run success commit together. A unique effect key prevents duplicate local notifications. This guarantees local database effects under these transactions; it does **not** promise exactly-once external delivery. External email or forwarding would need its own durable outbox and recipient idempotency. HookDesk does not forward arbitrary user URLs.

```mermaid
stateDiagram-v2
    [*] --> QUEUED: accepted or replayed
    QUEUED --> PROCESSING: atomic claim
    RETRY_WAIT --> PROCESSING: due retry claim
    PROCESSING --> RETRY_WAIT: temporary error, budget remains
    PROCESSING --> SUCCEEDED: business transaction commits
    PROCESSING --> IGNORED: older or already-applied state
    PROCESSING --> FAILED: permanent error or exhausted budget
    PROCESSING --> PROCESSING: expired lease, fresh owner/attempt
    FAILED --> QUEUED: manual replay creates a new run
```

The final replay arrow represents a **new** run. Earlier run states and attempts are preserved.

```mermaid
erDiagram
    Organization ||--o{ Membership : has
    User ||--o{ Membership : joins
    Membership ||--o{ Session : authenticates
    Organization ||--o{ WebhookSource : owns
    WebhookSource ||--o{ EncryptedSigningSecret : verifies
    WebhookSource ||--o{ WebhookEvent : receives
    WebhookEvent ||--o{ ProcessingRun : processes
    ProcessingRun ||--|| DurableJob : schedules
    ProcessingRun ||--o{ ProcessingAttempt : records
    WebhookSource ||--o{ Shipment : identifies
    Shipment ||--o{ BusinessEffect : changes
    WebhookEvent ||--o{ BusinessEffect : causes
    BusinessEffect ||--|| InternalNotification : announces
    Organization ||--o{ IdempotencyRecord : scopes
    User ||--o{ IdempotencyRecord : requests
    Organization ||--o{ AuditEvent : records
    User ||--o{ AuditEvent : acts
```

Tenant-owned cross-resource references use composite foreign keys including organization ID. Query indexes cover organization/received time, state, shipment identity and due work. Accepted raw bytes, parsed JSON, body hash and original metadata are protected from updates by a PostgreSQL trigger.

## Authentication, roles and API use

Passwords use Argon2id. Cookies hold opaque random tokens; the database stores only token hashes and expiry. Logout deletes the server-side session. Cookies are HttpOnly for sessions, SameSite=Lax, and Secure when configured. API writes require `X-CSRF-Token`; web forms carry the session's synchronizer token. Registration/login use a pre-authentication double-submit challenge and validate supplied Origin headers. Login throttles both client IP and normalized email in PostgreSQL, so a Redis failure does not disable authentication controls.

| Capability | ADMIN | OPERATOR | VIEWER |
| --- | --- | --- | --- |
| Inspect dashboard, events, attempts, shipments, notifications, audit | Yes | Yes | Yes |
| Replay failed events with a reason | Yes | Yes | No |
| Manage sources and rotate signing secrets | Yes | No | No |
| Trusted CLI membership/shipment management | Yes | No | No |

All owned-resource lookups are organization-scoped in services. Foreign-tenant resources return 404. A session selects one membership; accounts with several organizations may pass `organization_id` at login. Public webhook requests authenticate with the signature, independent of browser cookies and CSRF.

Minimal HTTPX management example (also automated by the local Swagger UI):

```python
import getpass
import httpx

with httpx.Client(base_url="http://localhost:8000") as client:
    preauth = client.get("/api/v1/auth/csrf").json()["csrf_token"]
    login = client.post(
        "/api/v1/auth/login",
        headers={"X-CSRF-Token": preauth},
        json={"email": "admin@example.com", "password": getpass.getpass()},
    )
    login.raise_for_status()
    client.headers["X-CSRF-Token"] = login.json()["csrf_token"]
    print(client.get("/api/v1/events", params={"state": "FAILED", "page": 1}).json())
```

Management endpoints live under `/api/v1`: `/auth/{register,login,logout,me,csrf}`, `/sources` and source detail/update/rotation, `/events` and detail/runs/attempts/replay, `/shipments` and detail/history, `/dashboard`, `/notifications`, `/audit-events`, and `/health/{live,ready}`. OpenAPI exposes request/response schemas, pagination and validated filters. Lists default to 25 records and allow at most 100 per page. Event filters include source, event type, state, aware received-from/to timestamps, exact external event ID and exact shipment ID. All API errors use `code`, `message`, and `request_id`.

## Retry, replay and operational visibility

Defaults: five attempts per run, base backoff 5 seconds, cap 300 seconds. Delay is half to all of `min(cap, base × 2^(attempt−1))`, using jitter. Temporary dependency timeouts/database errors retry; permanent unknown-shipment, conflicting-sequence or regression errors fail immediately. Expired leases mark unfinished attempts `ABANDONED` and consume the same run's budget.

Admins/operators replay only FAILED events. A replay reason and `Idempotency-Key` (1–200 characters) are required. Keys are scoped to organization, user and operation, with a request fingerprint including event ID/reason and a stored response. Repeating identical input returns the original replay result even after processing completes. Reusing a key with different input gives 409. Concurrent replays serialize on the event row; active or successful events cannot gain another run. Replay uses the immutable original payload and **currently deployed handler**.

The dashboard distinguishes current states from the 24-hour received-event and median-duration windows. Duration is claim-to-completion processing time, excluding queue waiting; abandoned attempts are excluded from the median. Oldest queued age measures current queued-run waiting. Duplicate counts are lifetime. Event details show correlation ID, timestamps, run/attempt numbers, safe errors, handler versions, scheduled retries and replay attribution.

`/health/live` checks the process. `/health/ready` performs a real write in a rolled-back PostgreSQL probe transaction and separately reports Redis and discovery heartbeat degradation. It remains 200 while Redis is unavailable, because acceptance still works. A discovery heartbeat means a worker recently executed the discovery task, not a guarantee that every worker process is healthy. Logs are structured JSON and include request/event/run/job identifiers where relevant. Signing secrets, session tokens and raw bodies are not logged by application code.

Development-only failure injection is configured by host CLI, never webhook fields or production API controls:

```powershell
uv run python -m app.manage fault --organization ORG_UUID --actor-email admin@example.com --source SOURCE_UUID --mode timeout --remaining 100
uv run python -m app.manage fault --organization ORG_UUID --actor-email admin@example.com --source SOURCE_UUID --mode off
uv run python -m app.manage fault --organization ORG_UUID --actor-email admin@example.com --source SOURCE_UUID --mode crash --remaining 1
```

`crash` terminates a Celery prefork child **after** its claim/fault consumption commits. The supervisor replaces the child, and discovery recovers the expired lease. These commands and seed refuse production or disabled demo mode. If using the host CLI with the Compose demo override, also set `DEMO_ENABLED=true` in your local `.env` or process environment.

## Reproducible live demonstration

Start the demo Compose configuration and seed the accounts as above. Then run from the host with `DEMO_ENABLED=true` and the same password in `DEMO_PASSWORD` (or enter it at the secure prompt):

```powershell
$env:DEMO_ENABLED = 'true'
uv run python -m scripts.demo --redis-outage --crash-recovery
```

This script logs in as the demo admin, creates a fresh source and shipments, and asserts the following against the actual HTTP API, database and running worker:

1. Accept a signed shipment event with a configured transient fault.
2. Observe retries, exhaust the five-attempt budget and find the event in the failed queue.
3. Remove the fault and request replay with a reason and idempotency key.
4. Observe success, two preserved runs, one shipment status-change record and one notification.
5. Deliver the exact event five more times; verify duplicate acknowledgments and no new job/effect.
6. Temporarily stop this project's Redis container, accept a second event, and verify readiness still reports writable storage.
7. Restart Redis in a `finally` block and wait for Beat/worker to process the durable event automatically.
8. With `--crash-recovery`, terminate one worker child after claim, then verify an ABANDONED attempt, successful replacement attempt and one status change.

It prints the event URL for inspection in the admin workspace. `--redis-outage` deliberately restarts only the project's Redis service; omit it for the retry/replay demonstration alone. The fast demo override usually completes within a minute, but the script uses bounded polling rather than assuming a particular machine speed. Automated tests use injected clocks instead of waiting for backoff.

For a visual walkthrough, sign in as `admin@example.com`, watch **Event inbox**, **Failed events**, the event's **Processing runs / Attempt history**, **Shipments**, and **Notifications** as the script progresses. Sign in as the operator/viewer to compare available actions, and as `other@example.com` to inspect the isolated second workspace.

## Checks and migrations

```powershell
uv run ruff check .
uv run ruff format --check .
uv run python scripts/test.py --cov=app --cov-report=term-missing
uv run alembic check
```

The test helper creates a dedicated `hookdesk_test` database on the configured local PostgreSQL server, then runs migrations and pytest. Every database test truncates only a database with a `hookdesk_test*` name; it never clears the application database. The local database user needs permission to create the test database. On a restricted server, provision it first and set `TEST_DATABASE_URL` explicitly:

```powershell
$env:TEST_DATABASE_URL = 'postgresql+psycopg://USER:PASSWORD@localhost:5432/hookdesk_test'
uv run pytest --cov=app
```

Without `TEST_DATABASE_URL`, direct pytest runs unit tests and explicitly skips database tests. Use the helper for the full suite. Concurrency tests open separate SQLAlchemy sessions and assert separate PostgreSQL backend PIDs. SQLite is never used. Tests cover commit visibility, concurrent duplicate delivery, overlapping claims/replays, shipment ordering, stale-worker rollback, lease recovery, retry exhaustion, local-effect uniqueness, tenant/role/CSRF controls and escaped payload rendering.

GitHub Actions installs the frozen lockfile, starts PostgreSQL 17/Redis, runs Ruff, checks migrations, and runs pytest with coverage. Create future schema changes with `uv run alembic revision --autogenerate -m descriptive_name`; inspect the migration before `uv run alembic upgrade head`. Migration `0002_integrity` additionally defines the immutable-envelope trigger and readiness table.

## Limitations and next steps

- No benchmarks or production security assessment have been performed. Pinned versions need an ongoing dependency/security update process.
- Offset pagination suits this demo; large inboxes would need measured query plans, cursor pagination, retention and partitioning. Idempotency records, sessions, counters and history currently have no retention job.
- One short database-only shipment handler is supported. Long-running handlers need lease renewal and appropriate time limits; network side effects need an outbox. Redis delivery can be duplicated and processing is at least once.
- Login limits use the direct client IP. A trusted reverse proxy must be configured explicitly before relying on forwarded addresses. Proxy request timeouts, TLS, ingress rate limits, monitoring/alerts, backups and encryption-key recovery are deployment responsibilities.
- There is no email invitation, password reset, SSO, API-key authentication, arbitrary handler execution or outgoing webhook forwarding.
- Browser automation was blocked by the local Docker engine failure recorded in `docs/VERIFICATION.md`; responsive templates and HTTP rendering checks are distinct from browser layout verification.
- Starlette currently emits a deprecation warning for its HTTPX-based test client; tests still execute using the required HTTPX dependency.

See [implementation assumptions](docs/ASSUMPTIONS.md) and [verification evidence](docs/VERIFICATION.md).

## Interview discussion points

- Explain the acknowledgment boundary: why a database commit, rather than a successful `.delay()`, defines accepted work.
- Trace two concurrent deliveries through the unique constraint and compare-and-ack path.
- Describe the separate lifetimes of delivery, event, processing run, attempt and business effect.
- Show how ownership tokens plus row locks prevent a recovered worker from being overwritten by its stale predecessor.
- Explain why a failed handler must roll back before persisting a retry outcome in a new transaction.
- Demonstrate sequence handling when delivered arrives before shipped, including a higher sequence with unchanged status.
- Trace a concurrent replay through organization/user-scoped idempotency and the event-row eligibility check.
- Distinguish one committed local effect from exactly-once external delivery, and describe the additional outbox protocol an external email integration would require.
- Explain why broker degradation does not imply an ingestion readiness failure, and what the discovery heartbeat actually proves.
