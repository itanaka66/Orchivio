# Platform Database Design

Metadata for Studio users, projects, models, AI jobs and audit. Source of truth:
`src/orchivio/db/models.py` (ORM) and `migrations/versions/` (DDL). The test
`tests/integration/test_migrations.py` fails if they diverge. Generated applications keep their
own data in separate schemas/databases and never share these tables
([ADR-0004](adr/0004-database-and-tenancy.md)).

## 1. Conventions

- UUID primary keys (`audit_events` uses a `BIGINT` identity for append ordering).
- `timestamptz` in UTC, defaulted by the database.
- Constraint names follow a fixed convention (`pk_`, `fk_`, `uq_`, `ix_`, `ck_`) so migrations
  are diff-able.
- Enumerated string columns have `CHECK` constraints (role, status, severity…).
- Emails are unique case-insensitively (`uq_users_email_lower`, an index on `lower(email)`).
- Secrets and tokens are stored hashed (`api_tokens.token_hash`, `users.password_hash`), never
  in plaintext. The API stores only references to secrets in environments (planned).
- Portable types: JSON is `JSONB` on PostgreSQL; `parent_revision_ids` is `UUID[]` on PostgreSQL
  and a JSON array of strings elsewhere (SQLite, used by tests).

## 2. Tables

### Identity and tenancy
| Table | Purpose | Key constraints |
|---|---|---|
| `users` | Studio accounts | unique `lower(email)`; `password_hash` nullable (SSO-only users) |
| `organizations` | Tenant boundary | unique `slug` |
| `memberships` | User ↔ organization with role | PK `(organization_id, user_id)`; role ∈ owner/admin/editor/viewer |
| `projects` | Belongs to one organization | unique `(organization_id, slug)` |
| `sessions` | Server-side login sessions (cookie token and CSRF token stored only as hashes) | unique `token_hash`; `expires_at`, `revoked_at` |
| `api_tokens` | Scoped, expiring tokens (also MCP, planned) | unique `token_hash`; bound to an organization, optionally a project |

### Models and revisions
| Table | Purpose | Key constraints |
|---|---|---|
| `applications` | One model lineage per project | unique `(project_id, key)`; `current_revision_id` |
| `branches` | Model branches; only `main` exposed in the MVP | unique `(application_id, name)`; `head_revision_id` |
| `app_revisions` | **Immutable** model snapshots | unique `(application_id, revision_no)`; `parent_revision_ids` (0–2 parents); `model_hash` |
| `element_refs` | Dependency-graph cache (find usages) | PK `(revision_id, from, to, kind)`; index on `to_element_id` |
| `lint_findings` | Linter results and suppressions | severity ∈ error/warning/info |

### AI, proposals, build
| Table | Purpose |
|---|---|
| `ai_conversations`, `ai_messages` | Conversation history |
| `ai_jobs` | Provider, model, prompt-template version, usage, status, timings |
| `proposals` | Unified change proposals (`base_revision_id` is null for an application's first revision) from the AI Builder, MCP agents and humans: `change_set_json`, `approval_level` 0–4, `status`, `approved_by`, `result_revision_id` |
| `builds`, `test_runs`, `deployments` | Execution records (references to artifact storage) |

### Jobs
`job_runs` — the task queue and run history (status `queued|running|succeeded|dead|cancelled`, lease columns,
attempts, `scheduled_for`; unique `(application_id, flow, scheduled_for)` makes planning idempotent).
`schedule_state` — where the planner stopped for each scheduled flow (advanced by compare-and-set).

### Delivery
`environments`, `environment_constants` (value XOR secret reference), `deployment_packages` (digest unique; bytes in the
artifact store), `package_deployments` (status `pending_approval|deployed|rejected|superseded`). The older
`deployments` table from 0001 is unused and kept to avoid rewriting history.

### Application end users
`app_users` (per application, lower-case unique username, `must_change_password`), `app_user_roles`, `app_sessions`
(token and CSRF stored as hashes, bound to the application). Unrelated to `users`/`sessions` of the Studio.

### Webhooks
`webhook_subscriptions` (URL, secret *reference*, event patterns, field allow-list, status, failure counter) and
`webhook_deliveries` (one per subscription and event, unique together; payload, attempts, lease, response status).
The outbox itself, `orchivio_outbox`, lives in the **runtime** database next to the application's data so that it is
written in the same transaction (docs/INTEGRATIONS.md).

`inbound_webhooks` (key, target flow, run-as role, secret reference, parameter mapping) and `inbound_deliveries`
(receipts, unique per webhook and event id). `job_runs.trigger` also allows `webhook` and `job_runs.source_ref`
records which request started a run.

`integration_destinations` (per application: logical name, base URL, auth type, header name, credential *reference*).

`mcp_servers` (per organization: name, URL, credential *reference*, status, last discovered tools, per-tool policy).

### Processes
`process_instances` (pinned revision, JSON state with a version for compare-and-set, `next_wake_at` for timers),
`user_tasks` (the inbox: role or user assignment, outcomes, form, due/escalation), `process_events` (the trail).

`app_oidc_providers` (one per application: issuer, client id, secret *reference*, claim and role mapping),
`oidc_login_states` (single-use state, nonce, PKCE verifier; hashed state and browser binding), `app_user_identities`
(unique per application, issuer and subject).

`policy_packs` (per organization: name, rules as validated JSON, status).

`requirements`, `stories`, `trace_links` (from requirement/story to story/element/revision/test, unique edge), `feedback_items` (page, element, text, optional screenshot bytes, status, story).

`git_syncs` (per application: remote, branches, path, token reference, last push/pull commit).

`app_logs` (per application: level, source, short message, request/trace ids, allow-listed scalar fields; pruned by age).

### Audit
`audit_events` — append-only. `plane` (platform/application) and `channel`
(studio/api/mcp) separate streams. **No foreign keys on purpose:** audit history must outlive
the rows it describes.

## 3. Integrity guarantees

| Guarantee | Mechanism | Verified |
|---|---|---|
| Revisions are immutable | `BEFORE UPDATE` trigger on `app_revisions` | PostgreSQL only; **not yet run** |
| Audit is append-only | `BEFORE UPDATE OR DELETE` trigger on `audit_events` | PostgreSQL only; **not yet run** |
| One revision number per application | unique constraint; repository takes a row lock on the application | SQLite test |
| Valid models only | `RevisionRepository.create` validates before insert | SQLite test |
| Circular references `applications`/`branches` ↔ `app_revisions` | FKs added after both tables exist | PostgreSQL only; SQLite omits them |

`app_revisions` rows can still be deleted (cascade from project/application deletion); that is
governed by retention policy, not by ordinary operations. Retention purges of `audit_events`
must go through a dedicated function owned by a separate database role (planned).

## 4. Tenancy checks

Every row ultimately hangs off `organizations` (via `projects`). Queries must join through the
caller's memberships; ids supplied by clients are never trusted
(DESIGN.md §12.2). Cross-organization tests are an MVP exit criterion.

## 5. Working with migrations

```bash
# apply (PostgreSQL)
DATABASE_URL=postgresql+psycopg://user:pass@host/db alembic upgrade head
# create a new migration after changing models
alembic revision --autogenerate -m "describe change"   # then review and hand-edit
```

Rules: migrations are reviewed by hand (autogenerate is a starting point); destructive changes
need A2+ approval (DESIGN.md §12.3); never edit a released migration; keep SQLite-incompatible
statements behind a dialect check so the test suite keeps running.

## 6. Planned tables

From DESIGN.md §9.2, by phase: `app_users`, `app_user_roles` (3), `requirements`, `stories`,
`trace_links`, `feedback_items`, `scheduled_jobs_state`, `task_queue` (8), `outbox_events`,
`webhook_subscriptions`, `mcp_tokens`, `mcp_servers` (9), `environments`,
`environment_constants`, `deployment_packages` (10), `process_instances`, `user_tasks`,
`ai_runs`, `kb_documents`, `kb_chunks` (11), `policy_packs` (12), `app_logs` (12).
