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

## 4. Scheduled jobs and the task queue

A server flow can carry a `schedule` (docs/MODEL-SPEC.md §12): UTC cron, the application role it runs as, literal
arguments, `missed_runs` (`skip`|`run_once`), `max_attempts` and `backoff_seconds`. Runs go through the
`job_runs` table, so manual runs (`POST /applications/{id}/jobs`) and scheduled runs share one queue.

* **Running a worker.** `orchivio worker` (add `--once` for one pass, e.g. from cron) polls every
  `--interval` seconds; or set `ORCHIVIO_WORKER_ENABLED=true` to run one inside the API process. Start as many
  as you like.
* **Single execution.** The planner advances `schedule_state.last_planned_at` by compare-and-set and the queue
  has a unique `(application, flow, scheduled_for)`, so an occurrence is queued once however many workers run.
  Claims are compare-and-set updates; a claim is a lease (`ORCHIVIO_WORKER_LEASE_SECONDS`, default 120). A
  worker that dies leaves an expired lease, which counts as a failed attempt: delivery is **at-least-once**, so
  flows with side effects outside the database should be idempotent.
* **Retries.** A failed attempt (the flow failed, the preview was not built, a crash) is requeued after
  `backoff_seconds * 2^(attempt-1)` (at most a day); after `max_attempts` the run is **dead** (the dead-letter
  queue, shown by `GET /applications/{id}/jobs?status=dead`), a `job.dead` audit event is written, and it
  can be given fresh attempts with `POST /jobs/{id}/retry`.
* **Concurrency.** At most `ORCHIVIO_WORKER_PER_APP_LIMIT` (default 2) runs per application at a time.
* **Missed runs.** `skip` runs only the latest occurrence of a window and only if it is at most two minutes old;
  `run_once` runs the latest however late and collapses the earlier ones. The look-back is capped at 24 hours
  and the first sighting of a schedule never replays history.
* **Where it runs.** Against the application's preview runtime (there are no production environments yet),
  under the system identity `system:schedule:<flow>` holding only `run_as_role`. The preview must be built for
  the current model, otherwise the attempt fails with a hint.

## 5. Configuration reference

All settings are environment variables with the `ORCHIVIO_` prefix (see `.env.example`):
`ENV` (`dev`/`prod`), `DATABASE_URL`, `PREVIEW_DATABASE_URL` (must differ in prod), `ARTIFACT_DIR`,
`ALLOW_REGISTRATION`, `SESSION_TTL_HOURS`, `COOKIE_SECURE`, `LOGIN_MAX_FAILURES`, `LOG_JSON`,
`MAX_BODY_BYTES`, `MAX_IMPORT_BYTES`, `METRICS_TOKEN`, `PACKAGE_SIGNING_KEY`, `PACKAGE_TRUSTED_PUBLIC_KEYS`, `WORKER_ENABLED`, `INBOUND_MAX_BODY_BYTES`, `INBOUND_RATE_PER_MINUTE`, `WEBHOOK_ALLOWED_HOSTS` (also the allowlist for integration destinations and MCP servers), `MCP_CLIENT_MAX_CALLS`, `WEBHOOK_ALLOW_HTTP`, `WEBHOOK_ALLOW_PRIVATE_NETWORKS`, `WEBHOOK_TIMEOUT_SECONDS`, `WEBHOOK_MAX_ATTEMPTS`, `WEBHOOK_BACKOFF_SECONDS`, `WEBHOOK_PAUSE_AFTER_FAILURES`, `WORKER_INTERVAL_SECONDS`, `WORKER_LEASE_SECONDS`, `WORKER_PER_APP_LIMIT`, `LLM_PROVIDER`, `ANTHROPIC_API_KEY`, `LLM_MODEL`.

Run migrations explicitly (`alembic upgrade head`); the API does not migrate on start.

## 5a. Processes

Timers and task escalation are driven by the worker (`orchivio worker`, or the in-process worker): each tick fires due
timers and escalates overdue tasks. Without a running worker a timer never fires and nothing escalates; tasks and
completions still work. See docs/PROCESS-SPEC.md.

## 6. Known gaps

No retention jobs for sessions, audit events or conversations; no automatic scheduling of backups (use cron
or your orchestrator); no log shipping or OpenTelemetry traces (planned, FR-123); no rate limiting beyond the
login throttle; the login throttle is per process. Jobs have no UI, no per-environment schedule enablement and
no queue metrics yet.
