# Implementation Status

Last updated: 2026-10-23 (FR-013/015 second approver for A3+ proposals; FR-029 cross-module enum expressions in interpreter; FR-064 status corrected to Done; FR-081 retries; FR-094/FR-097 contract migration endpoint; FR-111 admin reassignment; bootstrap endpoint; auto-migrate setting). Statuses: **Done** (implemented and
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
| FR-093 | Git-native canonical file format | Done | One file per element, stable ordering, lossless round trip (property-tested); Git sync pushes a revision to a branch (and opens a GitHub pull request) and pulls the base branch as a proposal (docs/GIT-SYNC.md); API only, GitHub PRs only |
| FR-010 | Accept natural-language requirements | Done | Sent per request to `ai/analyze` / `ai/propose`; conversation history stored in `AIConversation`/`AIMessage`; last 10 messages prepended to each prompt; list/get history endpoints |
| FR-011 | Structured decomposition | Done | `Analysis` and `AppModel` outputs validated against schemas |
| FR-012 | Implementation plan | Done | `POST /applications/{id}/ai/plan` — two-step AI call (analyze then plan_implementation); returns ordered task list with types (entity/security/page/flow/integration), effort (small/medium/large) and dependency indices |
| FR-013/014 | Diffs before application; approval | Done | Proposals, risk levels, role-gated approval, stale detection |
| FR-015 | AI execution history | Done | `ai_jobs`: provider, model, template version, usage, status (inputs hashed) |
| FR-016 | Failure analysis and repair proposals | Done | `test-runs/{id}/ai/repair` proposes from failed tests; response includes `root_causes` list (kind, count) grouped by test category (access, row-constraint, crud, validation); category summary sent in the AI prompt |
| FR-017 | AI budgets | Done | Monthly USD budget per project; Anthropic adapter now reports USD cost from a built-in per-model pricing table (prefix-matched; overridable via `prices` kwarg) |
| FR-018 | Provider adapters | Done (not verified live) | Interface, mock, Anthropic adapter tested against a mocked transport only |
| FR-020 | Entities, fields, relationships | Done | Schema + validation (no runtime) |
| FR-021/022 | Pages, forms, lists | Done | Validated; rendered by the generated-application shell (appshell) — list, form, detail pages drawn from the page API; Studio page designer: create/edit/delete pages (columns, actions, type, entity, mode) submitted as proposals |
| FR-023 | Declarative validation | Done | Enforced server-side by the preview data service |
| FR-025 | Immutable revisions | Done | `RevisionRepository` and `/applications/{id}/revisions`; PostgreSQL trigger **not yet run against PostgreSQL** |
| FR-026 | Diff and rollback | Done | API + repository; rollback creates a new revision; A3+ changes need `acknowledge_risks` |
| FR-027 | Static model checks | Done | See MODEL-SPEC.md rule table |
| FR-028 | Enumerations | Done | Removal of a value is flagged risky |
| FR-029 | Modules | Done | Multiple modules validate; cross-module enum references in expressions work via 3-stage lookup (same-module, qualified, unambiguous bare name) |
| FR-044 | Backup and restore | Done (platform data) | Verified logical backup/restore, atomic, tested on SQLite; PostgreSQL test written but unrun; no scheduling or retention (docs/OPERATIONS.md) |
| FR-045 | Metrics and health checks | Done | liveness, readiness, token-protected Prometheus metrics; no worker status (no workers yet) |
| FR-043 | Audit logs | Done (trigger verified) | Auth, membership, project, token and revision events recorded and queryable; append-only trigger verified in `tests/integration/test_postgres.py` (UPDATE and DELETE blocked) |
| FR-050 | Typed expression language | Done | Parser, checker, interpreter, limits |
| FR-051 | Computed fields | Done | Type-checked, cycle detection; virtual (read) and stored (write) in the preview runtime |
| FR-060..063 | Two-plane security, row constraints | Done | `resolve_access()` + `row_predicate()` in `security/access.py` enforce role-gated entity access, SQL row predicates and field-allow-lists on every data operation in both preview and production planes (simulated and real end users) |
| FR-064 | Field-level read/write access | Done | Allow-lists per rule; removed from responses, not filterable or sortable, write checks, flows act as the caller; exports and aggregates enforce field-level access via `readable_fields()` in both preview and production stores |
| FR-067 | End-user authentication for generated apps | Partial | Local accounts, forced first-login password change, anonymous opt-in, and OIDC (code flow + PKCE, JIT provisioning, claim-to-role mapping); self-registration (`POST /apps/{id}/auth/register`, controlled by `model.security.allow_registration`); self-service password-reset token flow (`POST /apps/{id}/auth/reset-request` + `reset-confirm`); TOTP MFA (setup/confirm/disable via `/auth/mfa/*`, TOTP code required on login when enabled, `pyotp` optional dep); single logout (`POST /auth/logout-all` revokes all sessions); app-plane endpoints serve production data via `ProductionStore`; no SAML |
| FR-066 | Consistency checker | Done | 5 checks |
| FR-086 | Studio MCP server | Done (MVP) | Read/search/usages/lint/validate/proposal/tests/explain/proposal.await tools, proposal-only writes, audit, rate limit; SSE stream of proposal status events (`GET /mcp`, `EventSourceResponse`); exercised only with a test client, not a real MCP agent |
| FR-087 | MCP client for the AI Builder | Done (MVP) | Organization registry, discovery (tools + resources + prompts), per-tool/resource/prompt policy (A0 only), one bounded planning round, untrusted-output handling, audit; OAuth2 client_credentials (token endpoint + client_id + client_secret via secret_ref, token cached until expiry); no interactive approval for stronger tools |
| FR-094 | Environments and promotion pipeline | Done | Ordered stages, stage/test/migration/approval gates, separation of duties; `ProductionStore.build()` provisions production tables on both approval path (`decide()`) and no-approval path (`promote()`); expand migration auto-runs before `_go_live()` on both paths; contract migration via `POST /deployments/{id}/contract` (see FR-097) |
| FR-095 | Immutable deployment packages | Done (control plane) | Reproducible zip, expand/contract plan, SBOM of direct dependencies, Ed25519 signature; no transitive SBOM |
| FR-096 | Environment constants and secret references | Done | Stored, audited and resolved (`resolve_constants`); wired into preview runner and app-plane flow runner (highest-stage environment's constants); entity-hook FlowHooks now also receive constants; all six secret schemes supported: `env:` (env var), `file:` (text file), `vault:` (HashiCorp Vault KV via HTTP — needs `VAULT_ADDR`/`VAULT_TOKEN`), `aws-sm:` (AWS Secrets Manager via boto3), `gcp-sm:` (GCP Secret Manager via google-cloud-secret-manager), `azure-kv:` (Azure Key Vault via azure-keyvault-secrets + azure-identity); cloud SDKs are optional — missing SDK raises `SecretUnavailable` |
| FR-097 | Near-zero-downtime migrations | Done | `delivery/migrate.py` implements `run_expand` and `run_contract`; expand phase runs automatically before `_go_live()` in both `promote()` and `decide()` (errors logged, not raised); `POST /deployments/{id}/contract` runs the contract phase against the deployment's production database, requires `acknowledge_risks=true`, records `contract_applied_at`, dry-run supported; PostgreSQL DDL only |
| FR-081 | Consumed REST via OpenAPI import | Done (MVP) | Typed operations, import as an A3 proposal, allowlisted destinations, SSRF-pinned calls, retries with exponential backoff (default 2 retries, configurable 0–5 per operation, respects `Retry-After` on 429); per application, not per environment; no pagination |
| FR-082 | Inbound and outbound webhooks | Done (MVP) | Outbound: signed CloudEvents, retries, dead letters, circuit breaker, SSRF guard. Inbound: signature-verified, replay-protected, payload-mapped, queued flow runs; secrets by reference only; limiter is per process |
| FR-083 | Business events | Done (outbox) | Transactional outbox for record created/updated/deleted, relayed at-least-once; no brokers, no custom events |
| FR-089 | Data import/export with mapping | Done | CSV and Excel (.xlsx): per-row errors, modes (all_or_nothing, skip_errors, upsert), dry run, column mapping, all through app security; upsert mode with `key_field`; Excel via openpyxl for both import and export (`/import-xlsx`, `/export-xlsx` on both app-plane and preview) |
| FR-074 | Data grid | Done | Permissioned CSV/xlsx export; cursor-paginated `GET /applications/{id}/preview/grid/{entity}` and `GET /apps/{id}/grid/{entity}` with multi-sort and `filter.<field>` params; `DataGrid` component in Studio wired to preview grid endpoint (sort, filter, inline edit, pagination) |
| FR-110 | Process definitions | Done | All 11 step types implemented end-to-end: start, end, user_task, system_task, decision, parallel_split, parallel_join, timer, catch_message, call_process (sub-process), send_message; form-based ProcessDesigner editor in Studio covers all types; no boundary events |
| FR-111 | Task inbox | Done | Role or expression assignment, claim, release, delegation, due dates, one-shot escalation; admin reassignment via `POST /user-tasks/{id}/reassign` (Studio plane); no notifications |
| FR-112 | Process versioning and instance migration | Done | `migration_policy` column on instances (`pin`=default, `migrate`); `GET /applications/{id}/process-instances/version-check` lists stale instances; `PATCH /process-instances/{id}/policy` and `POST /process-instances/{id}/migrate` for individual instances; `POST /applications/{id}/process-instances/bulk-migrate` migrates all stale `policy=migrate` instances in one call (with dry-run support) |
| FR-113 | Process monitoring | Done | Instance and task listings, event trail; throughput, bottleneck and duration analytics endpoints (`/process-analytics/throughput`, `/bottlenecks`, `/durations`); Studio `ProcessMonitor` dashboard wired into Studio under "Monitor processes" for any app with a revision |
| FR-065 | Page, action and flow access | Done (server side) | Pages need a listed role and the implied entity rights; columns, actions, `visible_expr`/`editable_expr` and flow-bound actions enforced; direct flow runs need `run_roles`; rendered by the appshell |
| FR-073 | Conditional visibility and editability | Done (server side) | `visible_expr` filters actions per row, `editable_expr` is checked before an edit is stored |
| FR-123 | Runtime observability | Done | OpenTelemetry traces and metrics over OTLP/HTTP (requests, flows, jobs, webhooks, consumed APIs, MCP, processes, AI Builder calls), OTLP log export, opt-in outbound `traceparent`, application log store with viewer API and AI log analysis; strict no-data policy. Flow runs logged (flow, outcome, duration_ms); process lifecycle events logged (process.started/completed/cancelled/failed, source="process"); Studio log viewer with AI analysis and filterable by source (flow, job, webhook, integration, process, gitsync) |
| FR-124 | Governance console | Done | Policy packs enforced on every model change, compliance report, app inventory with risk flags, AI usage, audit export; Studio screens for packs, compliance, inventory, AI usage, audit export and project settings; per-project AI quota (set in Project settings screen, enforced at `start_ai_job`) and policy-pack exceptions (managed in Project settings screen); compliance report skips excepted packs |
| FR-098 | Requirements, story board, traceability | Done | Requirements, stories, validated trace links (stories, elements, revisions, generated test cases), coverage report as JSON/CSV, AI extraction of draft requirements, Studio board with drag and drop, bulk reorder (`POST /projects/{id}/stories/reorder`); story editor screen added (click story title or Edit button to open title/body/acceptance editor in the board) |
| FR-099 | Preview feedback widget | Done | Feedback with page, element and optional screenshot, rate limit, triage, AI clustering into proposed stories; Studio preview endpoint (`POST /applications/{id}/feedback`) with element validation; end-user endpoint (`POST /apps/{id}/feedback`) with rate-limiting and screenshot support; feedback button + modal in the generated app shell (`app.js`); element picking (hover-highlight + click to identify field/button/column), screen capture via `getDisplayMedia`, and `element_id`/`screenshot_base64` wired into the feedback payload |
| FR-129 | PWA and offline | Done (not device-tested) | Studio is installable (manifest, icons, service worker for shell only, offline page, update prompt, offline bar, safe-area CSS); offline data and sync: creates + edits + deletes with automatic rebase and conflict-resolution UI; background sync on reconnect; storage quota management; Playwright offline scenarios for Chromium and Firefox (`offline.e2e.ts`; WebKit skipped); never caches API data; not tried on real physical devices. Generated applications: installable PWA shell served at `/apps/{id}/shell/` (manifest, icons, service worker scoped to the shell, offline page, list/form/detail pages from the page API); browser-tested with Playwright |
| FR-126 | Module registry | Done | Signed, semver, immutable module and template packages in the running server: publish, search, install/upgrade/remove as proposals, dependencies resolved with backtracking (newest-first, up to 10 candidates per package), install under another module key, yank, import/export and pull/push with other servers' registries (trusted keys only), templates, Studio screen; auto-update check and bulk upgrade proposal; page keys/routes are intentionally preserved when a module is renamed |
| FR-090 | Branch-ready revision graph | Done | `parent_revision_ids`, `branches` |
| FR-091 | Branches and semantic merge | Done | Three-way merge by element id with typed conflicts and per-conflict resolution; Studio screen for branches and merge; no per-branch preview |
| FR-052 | Server Flows | Done (preview) | Modelled, validated, edited visually and executed transactionally under the caller's security; no elevation, no production deployment |
| FR-053 | Client Flows | Done (browser runtime) | Modelled, validated and executed in the browser: `apps/web/src/flow/runner.ts` implements `runClientFlow` with start/decision/call_flow/end steps, wired into `App.tsx`; STATUS.md previously claimed no browser runtime but it is fully implemented |
| FR-055 | Scheduled jobs | Done | Cron schedules on server flows, planned once across workers, missed-run policy; `environment_id` threaded into `job_runs` (migration 0022), scheduler resolves highest-stage environment and passes it to the queue; worker uses `ProductionStore(env.data_url)` when `environment_id` is set, falling back to `PreviewStore` |
| FR-056 | Background task queues | Done (preview) | Leases, retry with exponential backoff, dead-letter queue, per-app concurrency; at-least-once. PostgreSQL path untested |
| FR-054 | Entity event hooks | Done (preview) | before/after create/update/delete bound to server flows, atomic, bounded recursion |
| FR-057 | Flow debugger | Done | Step trace with ids and an overlay in the editor; `POST .../debug` (variable capture + dry_run); `POST .../debug/stream` (SSE); live-stepping via `POST .../debug-session` (pause/resume/abort background thread at named breakpoints) |
| FR-058 | Flow unit tests | Done | Declarative tests in the model, run in generated test runs |
| FR-122 | Dependency graph | Done | Built and stored per revision; find-usages endpoint |
| FR-120 | Model linter | Done | 21 rules, suppressions with justification, blocking errors, stored per revision |
| FR-030 | CRUD API runtime | Done | App-plane endpoints (`/apps/{id}/pages/...`, `/apps/{id}/grid/...`, `/apps/{id}/flows/...`) use `ProductionStore` when an environment with `data_url` is promoted; falls back to the preview store when no such environment exists; all CRUD operations, pagination, filtering, flow execution and aggregates work through both stores (docs/RUNTIME.md) |
| FR-032 | OpenAPI for generated apps | Done | `build_openapi`, endpoint and CLI; schemas validated against live responses and request validation in tests |
| FR-031 | Typed clients | Done (generated) | Generated clients for the data API in TypeScript, Python and C# (types, typed list parameters, permitted operations only, typed flow runs, typed errors, pagination helpers `listAll`/`pages`/`list_all`/`ListAllAsync`); compiled/executed in tests (C# with the .NET SDK); other languages via the OpenAPI document; no auth handling |
| FR-033/034/036 | UI renderer, data migrations, build export | Done | FR-033: the generated-application shell renders list, form and detail pages drawn from the page API; FR-034: `run_expand()` now copies data for renamed fields (`copy_from`) and warns when new required columns need backfill; FR-036: `GET /revisions/{id}/sdk.zip` bundles OpenAPI spec + TypeScript + Python + C# clients in one ZIP download |
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

- Flow runs and hooks work in the preview only; `after_*` hooks and flows run synchronously in the request.
- A `change` step refreshes its own variable, but copies of the same record held in lists are not refreshed.
- Flow variable scoping is checked as "defined upstream on some path"; the engine fails at run time if a
  variable is unset on the path actually taken.

- The web Studio has the flow builder, story board, preview and administration screens (apps/web/README.md); the administration screens are tested with a fake API, not against a running server; mouse dragging, zoom and canvas rendering are untested in a browser, and no screen-reader testing was done (apps/web/README.md).
- The CSRF token lives in `sessionStorage`; a new tab needs a new login.

- Request-size limiting trusts `Content-Length`; enforce a body limit at the reverse proxy too.
- The login throttle is per process; running several API replicas needs a shared store.
- No email verification for platform (Studio) accounts; registration is open unless
  `ORCHIVIO_ALLOW_REGISTRATION=false`. Password reset and TOTP MFA are supported. `POST /auth/bootstrap` creates the first user (and opens a session) when no users exist — safe to call with registration disabled.
- Members can be invited by email (`POST /organizations/{id}/invitations`); the invite token is returned in `X-Invite-Token` for the caller to deliver.
- Risky (A3+) proposals require a second approver (a different user) via a second `POST /proposals/{id}/approval` call; A0–A2 proposals still apply on the first approval.
- Database migrations are not run automatically at start-up by default (`alembic upgrade head`); set `ORCHIVIO_AUTO_MIGRATE=true` to run them on each startup (requires `alembic.ini` in the working directory).
- Expressions: no relationship traversal (`record.owner.name`), no object literals (see EXPRESSION-SPEC.md).
- Application end-user accounts are local to each app (local accounts, OIDC, TOTP MFA, single logout); no SAML.
- AI calls and test runs are synchronous inside the request; no job-queue offload yet.
- Generated tests run on SQLite only; PostgreSQL SQL paths are unverified.
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
