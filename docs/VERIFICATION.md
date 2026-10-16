# Verification record

Verified locally on 16 October 2026, using Windows, CPython 3.12.10 and Docker Desktop's Linux containers.

## Passed

- Frozen dependency installation and `uv lock --check --offline` passed.
- Ruff lint and format checks passed; Python compilation passed.
- The full suite passed: **65 tests, 91% statement coverage**. PostgreSQL tests used a dedicated `hookdesk_test` database on PostgreSQL 17.6, including separate-connection concurrency tests. No SQLite was used. Command: `uv run python scripts/test.py --cov=app --cov-report=term-missing -q`.
- All Alembic migrations applied to the isolated test database, including the immutable-envelope trigger and unbounded sequence columns. An `alembic check` passed before the final sequence migration; final drift checking is listed below.
- Earlier Docker images built successfully and web, worker, Beat, PostgreSQL and Redis ran together. The application database migrations, including the sequence migration, applied through the migration service.
- Seeding was run twice against the application database, preserving both organizations, existing source IDs, users and shipment state. Automated tests also confirmed seed idempotence and no password output.
- A real HTTP/worker/Beat demonstration passed with `uv run python -m scripts.demo --redis-outage`: five attempts exhausted, the event appeared in the failed queue, replay succeeded with two preserved runs, one shipment change and one notification. Five duplicate deliveries created no additional job or effect.
- Redis was actually stopped during that demonstration. Ingestion returned 202, readiness remained 200 with broker degradation, and the durable event processed automatically after Redis restarted.
- HTTP template tests render all required operator pages and exercise escaped payload display, browser registration/logout, CSRF rejection, source creation/update and HTMX one-time secret/rotation responses.

The Celery transport module is exercised by the live demonstration outside pytest's coverage process. Coverage is not a claim that every Celery or browser branch was tested.

## Environment interruption and remaining checks

The final image build reached export/unpacking, then Docker Desktop reported `input/output error` under its containerd overlay filesystem. Its engine subsequently became unavailable (`Docker Desktop is unable to start`). At diagnosis, C: had **338,903,040 bytes free**, while E: had ample space. Storage pressure is a plausible contributor; no Docker volumes, unrelated images or user files were deleted.

The Docker Desktop start command reported Desktop was already running, while engine queries still failed. Repair/free space is needed in the Docker Desktop environment. This is distinct from the earlier temporary automatic-approval usage-limit interruption, which cleared on continuation.

All application processes now share `hookdesk:0.1.0`. The Dockerfile assigns ownership when copying source rather than recursively copying dependency layers through `chown`. **This final packaging adjustment has not been rebuilt successfully in the failed Docker environment.** Compose configuration is checked separately from engine execution.

The following remain unverified on the final packaged version:

- Final image extraction/startup and post-migration `alembic check`.
- The optional actual worker-child crash demonstration. Injected-clock PostgreSQL tests for abandonment, stale-worker fencing, expiry rollback and crash-fault recovery passed.
- Desktop/mobile browser layout and live JavaScript/CSP behavior. An isolated headless Edge check was attempted, but navigation timed out while Docker was unavailable. HTTP rendering/form tests do not establish visual browser correctness.
- GitHub Actions execution on GitHub; the workflow is included.

After restoring Docker Desktop and free disk capacity, rerun from the project root:

```powershell
docker compose -f compose.yaml -f compose.demo.yaml up --build -d
docker compose -f compose.yaml -f compose.demo.yaml ps
docker compose exec web uv run --frozen --no-dev alembic check
$env:DEMO_ENABLED = 'true'
uv run python -m scripts.demo --redis-outage --crash-recovery
uv run python scripts/test.py --cov=app --cov-report=term-missing
```

The demo reads `DEMO_PASSWORD` from the environment / ignored local `.env`, or a secure prompt. Seeded local accounts use the generated password in that file; no password is included here. Browser checks should cover login, source creation/rotation, HTMX filters, replay confirmation, mobile overflow, and `/docs` under the app's CSP.
