# Implementation Status

Last updated: 2026-10-17 (initial implementation). Statuses: **Done** (implemented and
tested), **Library** (implemented as a tested library but not yet wired into a running
service), **Schema** (data structures only), **Planned**.

## Requirements

| ID | Requirement | Status | Notes |
|---|---|---|---|
| FR-001 | Registration, login, logout | Done | Sessions + CSRF, scrypt, throttling, API tokens |
| FR-002 | Organizations and projects | Done | Create/list/update/delete, members, tenant isolation tests |
| FR-003 | Platform RBAC | Done | owner/admin/editor/viewer enforced on every platform endpoint |
| FR-004 | Project activity history | Done | `GET /organizations/{id}/audit-events` (admin) |
| FR-005 | Project export/import | Done (model scope) | Canonical per-element files, reproducible zip archive with manifest and hashes; export is admin-only and audited; import becomes a proposal. Does not include data, users or history |
| FR-093 | Git-native canonical file format | Done | One file per element, stable ordering, lossless round trip (property-tested); the Git synchronisation / pull-request generation itself is not built |
| FR-010 | Accept natural-language requirements | Partial | Sent per request to `ai/analyze` / `ai/propose`; only a hash is stored, no conversation history |
| FR-011 | Structured decomposition | Done | `Analysis` and `AppModel` outputs validated against schemas |
| FR-012 | Implementation plan | Planned | No task/dependency plan yet |
| FR-013/014 | Diffs before application; approval | Done | Proposals, risk levels, role-gated approval, stale detection |
| FR-015 | AI execution history | Done | `ai_jobs`: provider, model, template version, usage, status (inputs hashed) |
| FR-016 | Failure analysis and repair proposals | Partial | `test-runs/{id}/ai/repair` proposes from failed tests; no root-cause classification |
| FR-017 | AI budgets | Partial | Monthly USD budget per project; only as accurate as the cost the provider adapter reports (the Anthropic adapter reports 0) |
| FR-018 | Provider adapters | Done (not verified live) | Interface, mock, Anthropic adapter tested against a mocked transport only |
| FR-020 | Entities, fields, relationships | Done | Schema + validation (no runtime) |
| FR-021/022 | Pages, forms, lists | Schema | Validated; no renderer (the data API exists, see FR-030) |
| FR-023 | Declarative validation | Done | Enforced server-side by the preview data service |
| FR-025 | Immutable revisions | Done | `RevisionRepository` and `/applications/{id}/revisions`; PostgreSQL trigger **not yet run against PostgreSQL** |
| FR-026 | Diff and rollback | Done | API + repository; rollback creates a new revision; A3+ changes need `acknowledge_risks` |
| FR-027 | Static model checks | Done | See MODEL-SPEC.md rule table |
| FR-028 | Enumerations | Done | Removal of a value is flagged risky |
| FR-029 | Modules | Schema | Multiple modules validate; cross-module enum use in expressions is limited |
| FR-044 | Backup and restore | Done (platform data) | Verified logical backup/restore, atomic, tested on SQLite; PostgreSQL test written but unrun; no scheduling or retention (docs/OPERATIONS.md) |
| FR-045 | Metrics and health checks | Done | liveness, readiness, token-protected Prometheus metrics; no worker status (no workers yet) |
| FR-043 | Audit logs | Partial | Auth, membership, project, token and revision events recorded and queryable; append-only trigger is PostgreSQL-only and untested |
| FR-050 | Typed expression language | Done | Parser, checker, interpreter, limits |
| FR-051 | Computed fields | Done | Type-checked, cycle detection; virtual (read) and stored (write) in the preview runtime |
| FR-060..063 | Two-plane security, row constraints | Partial | Enforced by the preview data service on every operation, for simulated identities and for real end users |
| FR-064 | Field-level read/write access | Done (preview runtime) | Allow-lists per rule; removed from responses, not filterable or sortable, write checks, flows act as the caller; other query paths (exports, aggregates) do not exist yet |
| FR-067 | End-user authentication for generated apps | Partial | Local accounts, sessions, forced first-login password change, anonymous opt-in; no OIDC/SAML, MFA, self-registration or email reset; serves preview data only |
| FR-066 | Consistency checker | Done | 5 checks |
| FR-086 | Studio MCP server | Partial | Read/search/usages/lint/validate/proposal/tests tools, proposal-only writes, audit, rate limit; no `model.explain`, no SSE; exercised only with a test client, not a real MCP agent |
| FR-087 | MCP client for the AI Builder | Not started | |
| FR-094 | Environments and promotion pipeline | Partial | Ordered stages, stage/test/migration/approval gates, separation of duties; records the deployment only, no executor |
| FR-095 | Immutable deployment packages | Done (control plane) | Reproducible zip, expand/contract plan, SBOM of direct dependencies, Ed25519 signature; no transitive SBOM |
| FR-096 | Environment constants and secret references | Partial | Stored and audited; references are not resolved anywhere yet |
| FR-097 | Near-zero-downtime migrations | Not started | Plans only |
| FR-081 | Consumed REST via OpenAPI import | Done (MVP) | Typed operations, import as an A3 proposal, allowlisted destinations, SSRF-pinned calls; per application, not per environment; no pagination or retries |
| FR-082 | Inbound and outbound webhooks | Done (MVP) | Outbound: signed CloudEvents, retries, dead letters, circuit breaker, SSRF guard. Inbound: signature-verified, replay-protected, payload-mapped, queued flow runs; secrets by reference only; limiter is per process |
| FR-083 | Business events | Done (outbox) | Transactional outbox for record created/updated/deleted, relayed at-least-once; no brokers, no custom events |
| FR-089 | Data import/export with mapping | Partial | CSV only: per-row errors, modes, dry run, column mapping, all through app security; no Excel, no upsert |
| FR-074 | Data grid (export part) | Partial | Permissioned (`export` operation), audited, formula-safe CSV export; no grid UI |
| FR-090 | Branch-ready revision graph | Done | `parent_revision_ids`, `branches` |
| FR-091 | Branches and semantic merge | Done (API) | Three-way merge by element id with typed conflicts and per-conflict resolution; no UI yet, no per-branch preview |
| FR-052 | Server Flows | Done (preview) | Modelled, validated, edited visually and executed transactionally under the caller's security; no elevation, no production deployment |
| FR-053 | Client Flows | Partial | Modelled and validated (decision/call/end only); no browser runtime |
| FR-055 | Scheduled jobs | Partial | Cron schedules on server flows, planned once across workers, missed-run policy; runs in the preview runtime only (no environments) |
| FR-056 | Background task queues | Done (preview) | Leases, retry with exponential backoff, dead-letter queue, per-app concurrency; at-least-once. PostgreSQL path untested |
| FR-054 | Entity event hooks | Done (preview) | before/after create/update/delete bound to server flows, atomic, bounded recursion |
| FR-057 | Flow debugger | Partial | Step trace with ids and an overlay in the editor; no breakpoints or stepping |
| FR-058 | Flow unit tests | Done | Declarative tests in the model, run in generated test runs |
| FR-122 | Dependency graph | Done | Built and stored per revision; find-usages endpoint |
| FR-120 | Model linter | Done | 21 rules, suppressions with justification, blocking errors, stored per revision |
| FR-030 | CRUD API runtime | Partial | Preview environment only (docs/RUNTIME.md) |
| FR-032 | OpenAPI for generated apps | Done | `build_openapi`, endpoint and CLI; schemas validated against live responses and request validation in tests |
| FR-031/033/034/036 | UI renderer, typed clients, data migrations, build export | Planned | |
| FR-035 | Preview isolated from production data | Done | Separate database, per-app table prefix; prod requires a different preview URL |
| FR-040 | Test generation and execution | Done (preview scope) | Access, row-constraint, CRUD, validation cases; reports persisted; run comparison |
| FR-127 | Explain mode | Done | Read-only; citations are checked against the model |
| FR-128 | Synthetic test data | Done | Deterministic, constraint-aware, model-only |
| FR-068 | View as role in preview | Done | `X-Preview-User` / `X-Preview-Roles` |

Everything not listed is **Planned**.

## What has not been verified

- **PostgreSQL tests exist but have never run.** `tests/integration/test_postgres.py` (predicate dialect,
  triggers, circular FKs, runtime on real types, generated tests) is skipped locally and runs in the CI
  `postgres-migrations` job; expect it to find real problems on its first run.

- **No test has run against PostgreSQL.** Migrations and repositories are tested on SQLite. The
  PostgreSQL-specific parts (circular foreign keys, immutability triggers, JSONB/UUID[] types,
  `IS DISTINCT FROM` predicates) are exercised only by the `postgres-migrations` CI job, which
  has not run yet.
- `Dockerfile`, `docker-compose.yml` and `.github/workflows/ci.yml` have not been run.
- The Anthropic adapter has never called the live API; it is tested with a mocked transport.
- No AI quality evaluation (schema validity, coverage, rework rate) exists; behaviour against real
  models is unmeasured.
- Cookie behaviour was tested with the Starlette test client, not a real browser (the `Secure` flag
  is asserted on the header only).
- `ruff` was not available when the code was written; lint cleanliness is unchecked.
- The SQL predicate compiler is checked against SQLite only.

## Known limitations

- Flow runs and hooks work in the preview only; `after_*` hooks and flows run synchronously in the request, there
  are no scheduled jobs, task queues or retries (FR-055/056).
- A `change` step refreshes its own variable, but copies of the same record held in lists are not refreshed.
- Flow variable scoping is checked as "defined upstream on some path"; the engine fails at run time if a
  variable is unset on the path actually taken.

- The web Studio contains only the flow builder; mouse dragging, zoom and canvas rendering are untested in a browser, and no screen-reader testing was done (apps/web/README.md).
- The CSRF token lives in `sessionStorage`; a new tab needs a new login.

- Request-size limiting trusts `Content-Length`; enforce a body limit at the reverse proxy too.
- The login throttle is per process; running several API replicas needs a shared store.
- No email verification, password reset, MFA or SSO; registration is open unless
  `ORCHIVIO_ALLOW_REGISTRATION=false`. There is no first-user bootstrap other than registering.
- Members can only be added if they already have an account (no invitations).
- Risky revisions are confirmed by a flag from the same caller; a second approver and the
  proposal workflow are planned.
- Database migrations are not run automatically at start-up (`alembic upgrade head`).
- Expressions: no relationship traversal (`record.owner.name`), no empty list literals, no
  object literals; `/` and `%` do not compile to SQL (see EXPRESSION-SPEC.md).
- Enumerations from another module cannot be named inside expressions.
- No application end-user accounts: the runtime exists only as the preview with simulated identities.
- AI calls and test runs are synchronous inside the request; no job-queue offload yet.
- Generated tests run on SQLite only; PostgreSQL SQL paths are unverified.
- No second approver for risky proposals; `acknowledge_risks` is a flag from an admin.
- Linter rules are heuristics (for example sensitive field names); expect false positives and use
  suppressions with a justification.
- Schema 1.1 has no flows, processes, integrations or hooks (entity `hooks` validate with a
  warning only).

## Observations about DESIGN.md

Recorded here rather than silently changed in the design:

1. FR-131..FR-135 sit in the §4.3.1 table next to FR-028/029, breaking numeric order. Harmless,
   but traceability tables should use the IDs as written.
2. §9 lists `users` without a password column; local login (FR-001) needs `password_hash`, which
   the implementation adds (nullable, for future SSO-only users).
3. §9.2 puts `branches` in phase 2 but the MVP needs a `main` branch from the first revision;
   it is created in the initial migration.
4. §10.2 mixes nested (`/projects/{id}/applications`) and flat (`/revisions/{id}/...`) resource
   paths. See API-SPEC.md for the proposed convention.
