# Implementation decisions

- A session selects one organization. A user with multiple memberships can select an organization ID at login; permissions are read from the current membership on every request.
- Memberships and shipments are managed by trusted host-administrator CLI commands. The caller identifies an existing organization admin; host access is itself a privileged capability. There is no invitation or password-reset system.
- Event type is restricted to `shipment.status_changed`; unknown JSON fields are rejected. Unknown shipment IDs are accepted into the inbox and fail during asynchronous business processing.
- An identical delivery means byte-for-byte identical JSON, including whitespace. The SHA-256 of those bytes is the deduplication fingerprint.
- An immediately previous signing secret can remain valid during rotation. Rotating again revokes any older previous secret.
- Local notifications represent status changes, with a unique `(organization_id, shipment/status effect key)`. Sequence-only advancement creates no extra notification.
- Event and run states are updated together. A job exists once per run and uses a lease token; delivery through Redis is deliberately repeatable.
- Handlers are short, database-only transactions. They must finish within their lease; there is no lease renewal for long-running handlers in this version.
- Failed or abandoned attempts consume their processing-run budget. Replays start at attempt one in a separate run and preserve the accepted envelope with a database trigger.
- Metrics label their time windows: received events and median completed attempt duration cover 24 hours; duplicate deliveries are lifetime; state counts and queue age describe current work. Abandoned attempts are excluded from the duration median.
- Date-only/naive values are rejected by the API's date filters. The web interface explicitly interprets its date-time controls as UTC.
- Demo users and faults require explicit `DEMO_ENABLED=true`; production startup rejects this setting. Demo passwords come from secure input or `DEMO_PASSWORD`, never a fixed production default.
- Containers bind local ports to `127.0.0.1`. HTTPS and reverse-proxy configuration belong to the deployment environment; set `PUBLIC_BASE_URL` and `COOKIE_SECURE=true` when using HTTPS.
- Pinned stable dependencies are resolved in `uv.lock`. Infrastructure versions are pinned for a reproducible demonstration, not represented as a security-maintained production deployment.
