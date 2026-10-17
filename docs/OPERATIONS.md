# Operations

For people running Orchivio. Design requirements: DESIGN.md §4.5 (FR-044, FR-045), §5, §12.

> Orchivio is pre-alpha. This guide describes what exists; it is not a production hardening guide.
> Items marked **untested** have not been exercised against a real PostgreSQL server yet.

## 1. Health, readiness, metrics

| Endpoint | Purpose | Auth |
|---|---|---|
| `GET /healthz` | Liveness: the process is up. Never touches the database. | none |
| `GET /readyz` | Readiness: the platform and preview databases answer and the schema is at the revision this code expects. `200` when ready, `503` otherwise, with `checks` of `ok` / `behind` / `missing` / `unavailable`. Error details go to the log, not the response. | none |
| `GET /metrics` | Prometheus text format: `orchivio_http_requests_total{method,route,status}`, `orchivio_http_request_duration_seconds` (histogram), `orchivio_database_up`, `orchivio_build_info`, `orchivio_uptime_seconds`. | `Authorization: Bearer <ORCHIVIO_METRICS_TOKEN>`; **disabled (404) unless the token is set** |

Route labels are templates (`/api/v1/projects/{project_id}`), never raw paths, and unmatched paths share one
label, so identifiers and scanners cannot create unbounded series. There are no worker processes yet, so no
worker health; AI calls and test runs execute inside API requests.

Use `/readyz` for load-balancer and orchestrator readiness probes, `/healthz` for liveness. After an upgrade
that adds a migration the pods report not-ready (`migrations: behind`) until `alembic upgrade head` has run.

## 2. Backup

```bash
orchivio backup backup.zip --database-url postgresql+psycopg://… --artifacts /data/artifacts
orchivio verify-backup backup.zip
```

A backup is a zip with one JSON-lines file per table (foreign-key order), the artifact files, and a manifest
(row counts, SHA-256 per file, schema revision, creation time). It is taken in one consistent snapshot (a
`REPEATABLE READ` transaction on PostgreSQL), works on SQLite and PostgreSQL, and `verify-backup` checks every
hash without touching a database.

- **It is confidential.** It contains password and token hashes, every application model and the audit log.
  The CLI writes it with mode `0600`; encrypt it at rest and restrict who can read it.
- **Sessions are excluded** by default (restored systems require a fresh login). `--include-sessions` keeps
  them, which is only appropriate for an immediate failover.
- **Not included:** preview/runtime data (synthetic and rebuildable), logs, secrets held outside the database,
  and anything in the generated applications' own production databases (none exist yet).
- **A logical backup complements, not replaces, database-native backups.** For large installations also use
  `pg_dump` or WAL archiving with point-in-time recovery; the audit table's append-only triggers do not stop
  an operator with database superuser rights from altering history, so protect those backups too.

## 3. Restore

```bash
alembic upgrade head          # target database at the same schema revision as the backup
orchivio restore backup.zip --database-url postgresql+psycopg://… --artifacts /data/artifacts
```

Restore is all-or-nothing. It refuses to run unless the target is **empty** and at exactly the backup's
schema revision, inserts everything in one transaction (circular references are filled in afterwards, and on
PostgreSQL the identity sequence is moved past the restored ids), checks the row counts, and only then writes
artifacts. On any failure the database is unchanged.

**Restore drill.** Restoring is only a backup if it has been tried. Periodically restore the latest backup
into a scratch database, start the API against it, check `/readyz`, log in, and compare a model hash with
production. The test suite does exactly this on SQLite (`tests/integration/test_backup.py`); the PostgreSQL
variant runs in CI (`test_backup_and_restore_on_postgres`, **untested until the CI job has run**).

Moving between SQLite and PostgreSQL is not a supported use of the backup format.

## 4. Configuration reference

All settings are environment variables with the `ORCHIVIO_` prefix (see `.env.example`):
`ENV` (`dev`/`prod`), `DATABASE_URL`, `PREVIEW_DATABASE_URL` (must differ in prod), `ARTIFACT_DIR`,
`ALLOW_REGISTRATION`, `SESSION_TTL_HOURS`, `COOKIE_SECURE`, `LOGIN_MAX_FAILURES`, `LOG_JSON`,
`MAX_BODY_BYTES`, `MAX_IMPORT_BYTES`, `METRICS_TOKEN`, `LLM_PROVIDER`, `ANTHROPIC_API_KEY`, `LLM_MODEL`.

Run migrations explicitly (`alembic upgrade head`); the API does not migrate on start.

## 5. Known gaps

No retention jobs for sessions, audit events or conversations; no automatic scheduling of backups (use cron
or your orchestrator); no log shipping or OpenTelemetry traces (planned, FR-123); no rate limiting beyond the
login throttle; the login throttle is per process.
