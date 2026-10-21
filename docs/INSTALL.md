# Installation

> Orchivio is pre-alpha. APIs and data formats will change without notice. Do not run against data you cannot afford to lose.

---

## Prerequisites

| Path | Requirements |
|---|---|
| Docker | Docker Engine 24+ and Docker Compose V2 (`docker compose`) |
| Manual | Python 3.11+, Node.js 18+, PostgreSQL 14+ (or SQLite for development) |

Clone the repository in all cases:

```bash
git clone https://github.com/itanaka66/Orchivio.git
cd Orchivio
```

---

## Docker Compose — quickest start

The compose file brings up the API, an nginx serving the Studio, and PostgreSQL.

### 1. Create your environment file

```bash
cp .env.example .env
```

Open `.env` and set the one required value:

```
POSTGRES_PASSWORD=   # replace with a strong random string
```

Generate a suitable value:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

### 2. Build and start

```bash
docker compose up --build
```

### 3. Open the Studio

Navigate to **http://localhost**. The API is also reachable at **http://localhost:8000**.

Register the first account, then close public sign-up:

```
ORCHIVIO_ALLOW_REGISTRATION=false
```

Restart the stack after any `.env` change.

### Useful commands

```bash
docker compose up -d                          # detached
docker compose logs -f api                    # follow API logs
docker compose down                           # stop
docker compose down -v                        # stop and delete the database volume
docker compose exec api alembic upgrade head  # run migrations after an update
```

---

## Manual installation

### Backend (Python)

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

pip install -e ".[postgres,server]"

export ORCHIVIO_DATABASE_URL=postgresql+psycopg://user:pass@localhost/orchivio

alembic upgrade head               # required before first start

uvicorn orchivio.api.main:app --reload
```

The API listens on **http://localhost:8000**. Add `--host 0.0.0.0` to bind to all interfaces.

### Frontend (Node.js)

```bash
cd apps/web
npm install
npm run dev        # dev server on http://localhost:5173, /api proxies to :8000
```

### Production frontend build

```bash
cd apps/web
npm run build      # output: apps/web/dist
```

Serve `apps/web/dist` from nginx at the same origin as the API.

---

## Creating the first user

When `ORCHIVIO_ALLOW_REGISTRATION=true` (default), any visitor can register. For a closed install:

```bash
# Interactive wizard
scripts/add-user.sh

# Non-interactive
scripts/add-user.sh -e admin@example.com -n "Ada Lee" -o acme -r owner

# Windows
scripts\add-user.bat admin@example.com "Ada Lee" acme owner

# Python CLI (what the scripts call)
orchivio user add --email admin@example.com --name "Ada Lee" --org acme --role owner
```

> **Security note:** The password is never a command-line argument. The scripts prompt for it without echo, read it from stdin (`-p`), or take it from `ORCHIVIO_NEW_PASSWORD`.

Valid roles: `owner`, `admin`, `editor`, `viewer`.

> **Note:** The `--org` slug must refer to an organization that already exists. There is no CLI command to create an organization; use the Studio UI (sign in → `+ New organization`) or the API (`POST /api/v1/organizations`).

After the admin account is created, disable registration:

```
ORCHIVIO_ALLOW_REGISTRATION=false
```

---

## Enabling the AI Builder

All Studio features work without an LLM key. The AI Builder is additive.

### 1. Install the Anthropic extra

```bash
pip install -e ".[postgres,server,anthropic]"
```

The Docker Compose image includes this by default.

### 2. Set the provider and key

```
ORCHIVIO_LLM_PROVIDER=anthropic
ORCHIVIO_ANTHROPIC_API_KEY=sk-ant-…
ORCHIVIO_LLM_MODEL=                 # optional; defaults to claude-sonnet-4-5
```

### 3. Enable AI per project

Each project must opt in via **Project → Settings → AI Builder** (`ai_allow_external` flag) before the AI can access its model. Prompts are never stored; AI calls appear in the audit log with token counts but no prompt or output text.

---

## Production configuration

### Required changes from dev defaults

| Variable | Value | Why |
|---|---|---|
| `ORCHIVIO_ENV` | `prod` | Enforces Secure cookies; requires separate preview DB. |
| `ORCHIVIO_DATABASE_URL` | `postgresql+psycopg://…` | Point to a managed PostgreSQL instance. |
| `ORCHIVIO_PREVIEW_DATABASE_URL` | `postgresql+psycopg://…` | Must differ from main DB in prod. |
| `ORCHIVIO_PUBLIC_URL` | `https://studio.example.com` | Required for OIDC redirect URIs. |
| `ORCHIVIO_COOKIE_SECURE` | `true` | Auto-set by `ENV=prod`; set explicitly behind a TLS proxy. |
| `ORCHIVIO_ALLOW_REGISTRATION` | `false` | Disable public sign-up. |
| `ORCHIVIO_PACKAGE_SIGNING_KEY` | base64 Ed25519 key | Signs delivery packages. Generate with `orchivio keys generate`. |
| `ORCHIVIO_METRICS_TOKEN` | random secret | Enables the `/metrics` Prometheus endpoint. |

### HTTPS

Terminate TLS at a reverse proxy (nginx, Caddy, or a load balancer). Pass `ORCHIVIO_COOKIE_SECURE=true` so session cookies carry the Secure flag.

### Studio caching rules

```
/index.html, /sw.js, /manifest.webmanifest  →  Cache-Control: no-cache
/assets/*                                   →  Cache-Control: public, max-age=31536000, immutable
```

### Migrations

The API does **not** auto-migrate on start. Run explicitly before restarting after an upgrade:

```bash
alembic upgrade head
```

Until migrations complete, pods return `503` from `/readyz` with `"migrations": "behind"`.

---

## Environment variable reference

All variables use the `ORCHIVIO_` prefix. See `.env.example` for the annotated list.

### Core

| Variable | Default | Description |
|---|---|---|
| `ORCHIVIO_ENV` | `dev` | `dev` or `prod`. |
| `ORCHIVIO_DATABASE_URL` | SQLite | Primary database. |
| `ORCHIVIO_PREVIEW_DATABASE_URL` | same as DB | Preview/runtime database. Must differ in prod. |
| `ORCHIVIO_ARTIFACT_DIR` | `/data/artifacts` | File storage path. |
| `ORCHIVIO_PUBLIC_URL` | — | Public base URL. Required in prod for OIDC. |
| `ORCHIVIO_ALLOW_REGISTRATION` | `true` | `false` disables new sign-ups. |
| `ORCHIVIO_SESSION_TTL_HOURS` | `720` | Session lifetime (30 days). |
| `ORCHIVIO_COOKIE_SECURE` | auto | Secure flag on session cookies. Auto-true when `ENV=prod`. |
| `ORCHIVIO_LOGIN_MAX_FAILURES` | `10` | Failed login attempts before temporary lock. |
| `ORCHIVIO_LOG_JSON` | `false` | Emit structured JSON logs. |
| `ORCHIVIO_MAX_BODY_BYTES` | `10485760` | Max request body size (10 MB). |

### Security & delivery

| Variable | Default | Description |
|---|---|---|
| `ORCHIVIO_METRICS_TOKEN` | — | Bearer token for `GET /metrics`. Endpoint is 404 when unset. |
| `ORCHIVIO_PACKAGE_SIGNING_KEY` | — | Ed25519 private key for delivery package signing. |
| `ORCHIVIO_PACKAGE_TRUSTED_PUBLIC_KEYS` | — | Comma-separated trusted public keys. |
| `ORCHIVIO_WEBHOOK_ALLOWED_HOSTS` | — | Allowlist for webhook targets, integrations, MCP servers. |
| `ORCHIVIO_WEBHOOK_ALLOW_HTTP` | `false` | Allow non-HTTPS webhook targets. |
| `ORCHIVIO_WEBHOOK_ALLOW_PRIVATE_NETWORKS` | `false` | Allow webhooks to private/loopback IPs. |
| `ORCHIVIO_MCP_CLIENT_MAX_CALLS` | `10` | Max MCP tool calls per flow run. |

### Worker & jobs

| Variable | Default | Description |
|---|---|---|
| `ORCHIVIO_WORKER_ENABLED` | `false` | Run a background worker inside the API process. |
| `ORCHIVIO_WORKER_INTERVAL_SECONDS` | `10` | Poll interval for the job queue. |
| `ORCHIVIO_WORKER_LEASE_SECONDS` | `120` | Job claim lease duration. |
| `ORCHIVIO_WORKER_PER_APP_LIMIT` | `2` | Max concurrent runs per application. |

### AI Builder

| Variable | Default | Description |
|---|---|---|
| `ORCHIVIO_LLM_PROVIDER` | `none` | `anthropic` to enable the AI Builder. |
| `ORCHIVIO_ANTHROPIC_API_KEY` | — | Required when provider is `anthropic`. |
| `ORCHIVIO_LLM_MODEL` | — | Override the default model. |

### Observability

| Variable | Default | Description |
|---|---|---|
| `ORCHIVIO_OTEL_ENABLED` | `false` | Enable OpenTelemetry export. |
| `ORCHIVIO_OTEL_ENDPOINT` | — | OTLP/HTTP collector endpoint, e.g. `http://collector:4318`. |
| `ORCHIVIO_OTEL_SERVICE_NAME` | `orchivio` | Service name in traces. |
| `ORCHIVIO_OTEL_SAMPLE_RATIO` | `1.0` | Head sampling ratio (0.0–1.0). Metrics are never sampled. |
| `ORCHIVIO_APP_LOG_RETENTION_DAYS` | `30` | Days to keep application log lines in the Studio viewer. |

---

## Health checks

| Endpoint | Auth | Purpose |
|---|---|---|
| `GET /healthz` | none | Liveness. Process is up; never touches the database. |
| `GET /readyz` | none | Readiness. `200` when the database is reachable and migrations are current; `503` otherwise. |
| `GET /metrics` | `Authorization: Bearer <ORCHIVIO_METRICS_TOKEN>` | Prometheus text format. 404 when token is unset. |

Configure your orchestrator: `/healthz` for liveness probes, `/readyz` for readiness probes.

---

## Background worker

Scheduled server flows, process timers, and task escalation all require a running worker.

```bash
# Long-running process (recommended)
orchivio worker

# One-pass mode (suitable for a cron job)
orchivio worker --once

# Run inside the API process (development only)
ORCHIVIO_WORKER_ENABLED=true uvicorn orchivio.api.main:app
```

Multiple worker processes are safe; a flow occurrence is queued exactly once. Job delivery is at-least-once — make flow side-effects outside the database idempotent.

Dead-letter queue (after `max_attempts`):

```
GET  /applications/{id}/jobs?status=dead
POST /jobs/{id}/retry
```

---

## Backup & restore

> **Important:** A backup contains password hashes, every application model, and the full audit log. Encrypt it at rest and restrict access. The CLI writes the file with mode `0600`.

```bash
# Take a backup
orchivio backup backup.zip \
  --database-url postgresql+psycopg://user:pass@host/orchivio \
  --artifacts /data/artifacts

# Verify without touching a database
orchivio verify-backup backup.zip
```

A backup is a consistent snapshot (one `REPEATABLE READ` transaction) containing one JSON-lines file per table, artifact files, and a manifest. Sessions are excluded by default; pass `--include-sessions` only for an immediate failover.

```bash
# Restore (target database must be empty and at the same schema revision)
alembic upgrade head
orchivio restore backup.zip \
  --database-url postgresql+psycopg://user:pass@host/orchivio_restore \
  --artifacts /data/artifacts-restore
```

Restore is all-or-nothing: it refuses a non-empty target, inserts everything in one transaction, and only writes artifacts after row counts match.

> A logical backup complements — but does not replace — database-native backups (`pg_dump`, WAL archiving).

---

## Kubernetes

The `k8s/` directory contains manifests for a minimal cluster deployment:

```bash
kubectl apply -f k8s/namespace.yaml
kubectl apply -f k8s/secret.yaml        # edit with real values first
kubectl apply -f k8s/postgres.yaml      # or point secret at a managed DB

# Run migrations before deploying the API
kubectl run migrate --image=ghcr.io/itanaka66/orchivio:latest \
  --restart=Never --env-from=secret/orchivio-env \
  -- alembic upgrade head

kubectl apply -f k8s/api.yaml
kubectl apply -f k8s/web.yaml
kubectl apply -f k8s/ingress.yaml
```

Configure liveness probes on the API deployment to use `/healthz` and readiness probes to use `/readyz`. Run the worker as a separate `Deployment` with command `orchivio worker`.

---

## Observability

### OpenTelemetry

```bash
pip install "orchivio[otel]"
```

```
ORCHIVIO_OTEL_ENABLED=true
ORCHIVIO_OTEL_ENDPOINT=http://collector:4318
ORCHIVIO_OTEL_SERVICE_NAME=orchivio
ORCHIVIO_OTEL_SAMPLE_RATIO=0.1        # sample 10 % in production
ORCHIVIO_OTEL_LOGS=true               # ship log lines via OTLP
```

Collector credentials go in the standard `OTEL_EXPORTER_OTLP_HEADERS` env var. Without an endpoint, enabling telemetry only logs a warning — nothing breaks.

**What is never recorded:** request/response bodies, headers (including `Authorization`), query strings, SQL, record values, AI prompts and outputs, e-mail addresses, webhook or MCP server URLs.

### Prometheus

Set `ORCHIVIO_METRICS_TOKEN` to enable `GET /metrics`. Scrape with:

```
Authorization: Bearer <ORCHIVIO_METRICS_TOKEN>
```

Key metrics: `orchivio_http_requests_total`, `orchivio_http_request_duration_seconds`, `orchivio_database_up`.
