# RelayOps

Webhook ingestion, delivery retries, and replay dashboard built with FastAPI,
Next.js, SQLAlchemy, and SQLite. RelayOps stores incoming JSON and delivery jobs
atomically, forwards the original payload, and records delivery attempts.

The dashboard provides source and destination configuration, event inspection,
redacted payload views, delivery history, statistics, and manual replay of failed
jobs. It uses a local SQLite queue without Redis or Celery.

## Architecture

```text
Webhook provider -> Next.js /api proxy -> FastAPI -> SQLite
                                                       ^
                                                       |
                                      Delivery worker -> HTTPS destination
Browser dashboard -> Next.js /api proxy -> FastAPI
```

- `backend/`: FastAPI, authentication, SQLAlchemy models, SQLite storage, and tests.
- `backend/worker.py`: one delivery worker per local database, with a host lock,
  atomic claims, bounded retries, crash recovery, and attempt history.
- `frontend/`: Next.js App Router, TypeScript, Tailwind CSS, and Shadcn components.
  Its route handler proxies `/api/*` to the loopback backend, preserving cookies.
- `scripts/smoke-relayops.py`: isolated end-to-end verification with a mock destination.
- [OPERATIONS.md](OPERATIONS.md): diagnostics, backups, permissions, and incident procedures.

## Requirements

- Linux with readable `/proc/self/status` and local filesystem storage. Startup
  permission hardening and the worker's file lock depend on Linux facilities.
- Python 3.12+, Node.js 22+, and npm.
- A public HTTPS destination for real deliveries. Tests use mock destinations.

## Clone and configure

Replace `YOUR_USERNAME` with the repository owner's GitHub username:

```bash
git clone https://github.com/YOUR_USERNAME/relayops.git
cd relayops
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
test -e .env || cp .env.example .env
python -c 'import secrets; print(secrets.token_urlsafe(48))'
```

Put the generated value in `backend/.env` as `SECRET_KEY`. Never commit real
credentials. Missing, short, and known placeholder keys are rejected. The default
database is `backend/data/app.db`; startup creates tables for a fresh installation.
Keep the API and worker on the same environment and database.

Registration is disabled by default. For initial account creation, set
`REGISTRATION_ENABLED=true` in `backend/.env` before starting the API. Create your
account in the dashboard, then set it back to `false` and restart the API.
Settings are cached until restart. Do not overwrite an existing environment file.

## Run locally

Run each command block in a separate terminal, starting from the repository root.

```bash
# API
cd backend
source .venv/bin/activate
uvicorn main:app --host 127.0.0.1 --port 8000 --reload --no-access-log
```

```bash
# Delivery worker: exactly one per database
cd backend
source .venv/bin/activate
python worker.py
```

```bash
# Dashboard
cd frontend
npm ci
npm run dev
```

Open <http://localhost:3000>. Interactive API docs are at
<http://127.0.0.1:8000/docs>. Ctrl+C stops each process; the worker also handles
SIGTERM by finishing its bounded current attempt. No background service is installed.

If these ports are occupied, choose unused ports: set `RELAYOPS_API_PORT` in the
frontend process to match Uvicorn, and set backend `DASHBOARD_ORIGINS` to the exact
frontend origins. For example, frontend port 3002 requires
`DASHBOARD_ORIGINS=["http://localhost:3002","http://127.0.0.1:3002"]`.
The proxy always connects to `127.0.0.1`.

For a production frontend build, run `npm run build` and `npm run start` from
`frontend/`. `npm run build -- --webpack` is an alternative for constrained build
environments. Keep the backend on loopback behind an HTTPS reverse proxy.

For an optional one-click Linux application-menu launcher on ports 8001/3002,
see [desktop launcher setup](scripts/README-launcher.md). It uses user systemd
services and existing dependencies.

## Send your first event

1. Sign in and create a **Source** with a high-entropy secret of 32–256 characters.
   Save the secret securely; the API never returns it. Copy the generated source ID.
2. Create a **Destination** for that source using a public HTTPS URL, such as
   `https://example.com/webhooks/orders`. URLs cannot include credentials, query
   strings, or fragments. Paths and names must not contain secrets.
3. Set `RELAYOPS_SOURCE_ID` and `RELAYOPS_WEBHOOK_SECRET` in your local shell, then send:

```bash
curl -i "http://localhost:3000/api/hooks/$RELAYOPS_SOURCE_ID" \
  -H 'Content-Type: application/json' \
  -H "X-Webhook-Secret: $RELAYOPS_WEBHOOK_SECRET" \
  -H 'Idempotency-Key: order-123-created' \
  --data '{"type":"order.created","order_id":123}'
```

4. Open **Events** to inspect the event and delivery attempts. Failed jobs expose
   **Retry delivery**, which starts another bounded retry cycle and preserves history.

New events return 202; an identical source/idempotency-key/payload returns 200.
Reusing a key with a different payload returns 409. Without a key, each request
creates a new event. Events without enabled destinations remain unrouted; adding a
later destination does not backfill them.

## API overview

Administrative endpoints use an httpOnly JWT cookie and scope records to the
signed-in owner. Webhook ingestion uses `X-Webhook-Secret`.

| Endpoint | Purpose |
| --- | --- |
| `/api/auth/register`, `/api/auth/login`, `/api/auth/logout`, `/api/auth/me` | Account and session operations |
| `GET /api/auth/policy` | Registration availability |
| `GET / POST /api/sources` | List or create sources |
| `PATCH /api/sources/{id}` | Enable/disable a source or rotate its secret |
| `GET / POST /api/destinations` | List or create destinations |
| `PATCH /api/destinations/{id}` | Edit destination configuration |
| `POST /api/hooks/{source_id}` | Ingest JSON |
| `GET /api/events`, `/api/events/{id}` | Paginated events and redacted details |
| `GET /api/deliveries/{id}/attempts` | Attempt history |
| `POST /api/deliveries/{id}/retry` | Replay a failed job |
| `GET /api/stats` | Owner-scoped statistics |
| `GET /api/health` | API liveness; not worker readiness |

## Configuration and operations

See `backend/.env.example` and `backend/app/config.py` for settings and validation.
Important defaults include:

| Setting | Default / purpose |
| --- | --- |
| `SECRET_KEY` | Required, at least 32 characters; generate a unique random value |
| `REGISTRATION_ENABLED` | `false`; enable only for intended account creation |
| `COOKIE_SECURE` | `false` locally; set `true` behind HTTPS |
| `DASHBOARD_ORIGINS` | Exact localhost/loopback frontend origins on port 3000 |
| `WEBHOOK_MAX_BYTES` | 1 MiB |
| `WEBHOOK_RATE_LIMIT` / `WEBHOOK_RATE_WINDOW_SECONDS` | 60 requests per source per 60 seconds |
| `DELIVERY_TIMEOUT_SECONDS` / `DELIVERY_MAX_ATTEMPTS` | 15 seconds / 5 attempts per retry cycle |
| `RETRY_BASE_SECONDS` | 5 seconds, exponential backoff capped at 3600 seconds |
| `DEV_ALLOWED_ORIGINS` | Empty; explicit loopback exceptions require development mode |
| `RETENTION_ENABLED` | `false`; enables read-only planning only |

From `backend/`, `.venv/bin/python diagnostics.py` checks worker lock freshness and
aggregate backlog without exposing payloads. Retention planning is available through
`retention.py --dry-run`; consult the runbook before using either command.

Startup uses `create_all`, which creates missing tables but does not migrate existing
columns. Future schema changes require versioned migrations. Back up and restore-test
existing databases before upgrades. Never copy an active SQLite database alone or
remove its journals, WAL files, or worker lock. Existing permissions are not repaired
automatically; follow the runbook for existing installations.

## Testing

Use the dependencies installed during setup. Backend tests use temporary SQLite
files and mocked destinations, not the live database:

```bash
cd backend
source .venv/bin/activate
python -m pytest -q
```

From a separate terminal at the repository root:

```bash
cd frontend
npm run lint
npx tsc --noEmit
npm run build
```

After building the frontend, run from the repository root:

```bash
backend/.venv/bin/python scripts/smoke-relayops.py
```

The smoke test starts temporary services on isolated ports with its own database
and mock destination, then stops them. It covers authentication, ingestion,
deduplication, retries, replay, redaction, pagination, and destination editing.
To avoid touching an active frontend build, build a separate frontend copy and set
`RELAYOPS_SMOKE_FRONTEND=/absolute/path/to/copy` when running the smoke script.

## Limitations

- Single host, local SQLite, and one sending worker per database; no NFS or
  multi-host scheduling. API ingestion quotas are process-local and reset on restart.
- Delivery is **at least once**. A destination may accept a request before its
  response is lost. Destinations must deduplicate the stable job UUID sent in
  `Idempotency-Key`, or the event UUID in `X-RelayOps-Event-ID` at the appropriate scope.
  There is no strict ordering, exactly-once guarantee, or guaranteed eventual success.
- Any 2xx response succeeds. HTTP 408, 429, 5xx, connection errors, and timeouts retry;
  other 4xx responses, redirects, and blocked destinations fail permanently.
- Payloads are stored unencrypted. Dashboard redaction covers known sensitive keys,
  not arbitrary personal data. Restrict storage permissions and encrypt backups.
- Source authentication is a shared header secret. Provider-specific signatures,
  outbound credentials/signing, team roles, bulk replay, and alerting are not implemented.
- Retention only plans candidates; it never deletes payloads or frees disk space.
- Public deployment needs HTTPS, secure cookies, exact origins, reverse-proxy rate
  limits, egress controls, monitoring, and disk quotas. This is a portfolio MVP,
  not a turnkey managed webhook service.

## License

Licensed under the [MIT License](LICENSE).

Copyright (c) 2026 Cedric Cierva Malit.
