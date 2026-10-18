# HTTP API Specification

Base path `/api/v1`. The running service publishes its own OpenAPI document at
`/api/openapi.json` and interactive docs at `/api/docs`; that document is authoritative for
request and response shapes. This file records conventions, behaviour and the plan.

## 1. Conventions

- JSON in and out; UTF-8. Every response carries `X-Request-ID` (an inbound id is reused only if
  it matches `[A-Za-z0-9._-]{8,64}`), `X-Content-Type-Options: nosniff` and `Cache-Control: no-store`.
- **Errors** use one envelope. They never include stack traces or internals and do not echo
  submitted model contents:
  ```json
  { "error": { "code": "approval_required", "message": "…", "details": { } } }
  ```
- **Pagination**: `limit` (1–200, default 50) and `offset` (≥ 0); lists return
  `{items, limit, offset, total}`. Out-of-range values are rejected with 422.
- **IDs** are UUIDs. A malformed id is 422.
- **404 vs 403**: if the caller is not a member of the owning organization the response is
  `404 not_found` with a body identical to a genuinely missing resource, so existence is not
  revealed. Members who lack the role get `403 forbidden`.
- Request bodies over `ORCHIVIO_MAX_BODY_BYTES` (1 MiB) are rejected with 413 when the client
  declares a length; also enforce a limit at the reverse proxy.
- Unknown properties in request bodies are rejected (422).

## 2. Authentication

Two mechanisms; every `/api/v1` route except `register` and `login` requires one.

| | Session (browsers) | API token (automation) |
|---|---|---|
| Credential | `orchivio_session` cookie: `HttpOnly`, `SameSite=Lax`, `Secure` in prod | `Authorization: Bearer orv_…` |
| CSRF | Unsafe methods need `X-CSRF-Token` (value returned by login) → else `403 csrf_failed` | Not needed (no ambient credential) |
| Lifetime | `ORCHIVIO_SESSION_TTL_HOURS` (12), revocable (logout) | 1–365 days, revocable |
| Storage | Only SHA-256 hashes are stored | same |

Details: passwords are hashed with scrypt (12–128 characters required). Login failures return one
generic `401 invalid_credentials` for unknown users, wrong passwords and disabled accounts; five
failures per email or per client address within 15 minutes give `429` with `Retry-After`
(per-process counter). A bad `Authorization` header never falls back to the cookie. Disabled users
lose access immediately. `ORCHIVIO_ENV=prod` refuses to start with insecure cookies.

**API tokens** are bound to one organization and optionally one project, carry `scopes` (`["*"]`
or action names such as `project.read`, `model.apply`) and can never exceed the owner's role. They
cannot create organizations or other tokens (`403 session_required`).

## 3. Roles and actions

Platform roles: `owner > admin > editor > viewer` (`orchivio.policy`).

| Action | Minimum role | Used by |
|---|---|---|
| `project.read`, `model.read` | viewer | reads, listing members |
| `application.create`, `model.apply`, `model.rollback` | editor | create applications, revisions, rollback |
| `project.create`, `project.update`, `member.manage`, `audit.read` | admin | |
| `project.delete`, `organization.delete` | owner | project deletion needs `?confirm=<slug>` |

Only an owner may grant, change or remove the `owner` role; an organization always keeps at least
one owner (`409 last_owner`). Any member may leave an organization.

## 4. Endpoints

| Method | Path | Notes |
|---|---|---|
| POST | `/auth/register` | `201` user; `409 email_taken`; `422 weak_password`; `403 registration_disabled` |
| POST | `/auth/login` | sets cookie; returns `{user, csrf_token, expires_at}` |
| POST | `/auth/logout` | revokes the session server-side |
| GET | `/me` | user and organization memberships |
| GET/POST | `/organizations` | list mine / create (creator becomes owner) |
| GET | `/organizations/{id}` | |
| GET/POST | `/organizations/{id}/members` | add by email of an existing user |
| PATCH/DELETE | `/organizations/{id}/members/{user_id}` | change role / remove |
| GET | `/organizations/{id}/audit-events` | `action_prefix`, pagination; newest first |
| GET/POST | `/organizations/{id}/projects` | |
| GET/PATCH/DELETE | `/projects/{id}` | |
| GET/POST | `/projects/{id}/applications` | `POST` may include an initial `model` |
| GET | `/applications/{id}` | includes `current_revision_no` |
| GET/POST | `/applications/{id}/revisions` | list / create; see below |
| GET | `/revisions/{id}` | includes `model_json` |
| GET/POST | `/applications/{id}/branches` | list (always includes `main`) / create `{name, from_revision_id?}`; names `[a-z0-9][a-z0-9._/-]{0,60}` |
| DELETE | `/branches/{id}` | abandon (`409 main_protected` for `main`) |
| POST | `/branches/{id}/merge` | three-way merge into `into` (default `main`); see below |
| GET | `/revisions/{id}/diff/{base_id}` | both must belong to the same application |
| POST | `/applications/{id}/rollback` | restores an earlier model as a *new* revision |
| GET/POST/DELETE | `/tokens`, `/tokens/{id}` | own tokens; the secret is returned once at creation |
| GET | `/revisions/{id}/openapi.json` | OpenAPI 3.1 document of the application's data API for that revision |
| GET | `/revisions/{id}/export` | reproducible zip of canonical files (admin; audited) |
| POST | `/applications/{id}/import` | raw `application/zip` body (max `ORCHIVIO_MAX_IMPORT_BYTES`, 8 MiB); verified, then stored as a proposal (`?title=`) |
| GET | `/revisions/{id}/lint`, `/revisions/{id}/elements/{element_id}/usages` | stored lint findings (`include_suppressed`) / find usages |
| GET/POST | `/applications/{id}/proposals`, `/proposals/{id}`, `/proposals/{id}/approval` | change proposals and their decision (docs/AI-BUILDER.md) |
| POST | `/applications/{id}/ai/analyze`, `/ai/propose`, `/ai/explain` | AI Builder (needs a configured provider) |
| POST | `/applications/{id}/preview/reset`; GET `…/preview/status`; CRUD `…/preview/data/{entity}[/{id}]` | preview runtime (docs/RUNTIME.md) |
| POST | `/applications/{id}/preview/flows/{flow}/run` | run a server flow in the preview as the simulated user; returns status, error and the step trace |
| POST/GET | `/revisions/{id}/test-runs`, `/test-runs/{id}`, POST `/test-runs/{id}/ai/repair` | generated tests |
| POST | `/models/validate`, `/models/validate-flow`, `/models/lint`, `/models/diff`, `/expressions/check` | stateless tools (any authenticated caller); `validate-flow` checks one unsaved flow in a model's context and returns issue paths ending in `steps/<id>…` |
| POST | `/mcp` | Studio MCP server (JSON-RPC 2.0, Streamable HTTP without SSE; bearer API token only); see below |
| GET | `/applications/{id}/schedules` | scheduled flows of the current revision with `next_run_at` and last run |
| GET/POST | `/applications/{id}/jobs` | list (`status`) / queue a server flow now (`flow`, `args?`, `run_as_role?`; defaults from its schedule) |
| GET | `/jobs/{id}` | run status, attempts, error, result |
| POST | `/jobs/{id}/retry`, `/jobs/{id}/cancel` | dead → queued with fresh attempts (`409 not_dead`) / queued → cancelled (`409 not_queued`) |
| GET/POST | `/applications/{id}/environments` | stages with their current deployment / create (admin) |
| PATCH/DELETE | `/environments/{id}` | policy flags / delete (only without history) |
| GET | `/environments/{id}/constants`; PUT/DELETE `/environments/{id}/constants/{KEY}` | `value` or `secret_ref` (admin to write) |
| POST | `/revisions/{id}/packages` | build the signed package (`201` new, `200` identical existing; `503 signing_not_configured`) |
| GET | `/applications/{id}/packages`, `/packages/{id}`, `/packages/{id}/verify`, `/packages/{id}/download` | list / metadata + plan + SBOM / verify / file (admin) |
| POST | `/packages/{id}/promote` | request a deployment; gates in docs/DELIVERY.md |
| POST | `/deployments/{id}/approval` | `{decision: approve or reject}` by another admin |
| GET | `/applications/{id}/deployments` | history (`environment`, `status`) |
| GET/POST | `/applications/{id}/app-users` | end users of the generated app (admin): list / create with temporary password and roles |
| GET/PATCH/DELETE | `/app-users/{id}`; POST `/app-users/{id}/reset-password` | roles, status, display name / new temporary password (ends sessions) |
| POST | `/apps/{app_id}/auth/login`, `/auth/logout`, `/auth/change-password`; GET `/auth/me` | application-plane sign-in (docs/APP-SECURITY.md §6) |
| ANY | `/apps/{app_id}/data/{entity}[/{id}]`, POST `/apps/{app_id}/flows/{flow}/run` | the generated app's data and flows as the signed-in end user |
| GET/POST | `/applications/{id}/webhooks` | outbound webhook subscriptions (admin): list / create (`url`, `secret_ref`, `events`, `fields`) |
| GET/PATCH/DELETE | `/webhooks/{id}` | read / change (including `status: active|paused`) / delete |
| POST | `/webhooks/{id}/test` | queue one `orchivio.ping` delivery |
| GET | `/webhooks/{id}/deliveries`, `/webhook-deliveries/{id}`; POST `/webhook-deliveries/{id}/retry` | delivery log (`status`), one delivery with payload, requeue a dead one |
| POST | `/apps/{app_id}/import/{entity}`; `/applications/{id}/preview/import/{entity}` | CSV import as the signed-in user / simulated role (`mode`, `dry_run`, `delimiter`, `ignore_unknown`, `map.*`); `422 import_rejected` lists row errors |
| GET | `/apps/{app_id}/export/{entity}`; `/applications/{id}/preview/export/{entity}` | CSV download; needs the `export` operation, audited |
| GET/POST | `/applications/{id}/inbound-webhooks` | signed endpoints that start a flow (admin): list / create |
| GET/PATCH/DELETE | `/inbound-webhooks/{id}`; GET `/inbound-webhooks/{id}/events` | read / change mapping, role, secret, status / delete; accepted requests |
| POST | `/hooks/{app_id}/{key}` | the public endpoint: authenticated only by `Orchivio-Signature`; `202`, `401`, `404`, `413`, `422`, `429` |
| PUT/GET/DELETE | `/applications/{id}/destinations[/{name}]` | where a logical integration destination points and how to authenticate (admin) |
| POST | `/applications/{id}/integrations/openapi-import` | OpenAPI 3.x (JSON/YAML) -> change proposal with an integration and its transient entities |
| GET/POST | `/organizations/{id}/mcp-servers` | external MCP servers for the AI Builder (org admin): list / register |
| GET/PATCH/DELETE | `/organizations/{id}/mcp-servers/{name}` | read / change URL, credential reference, status / remove |
| POST | `/organizations/{id}/mcp-servers/{name}/discover` | list the server's tools (new ones start disabled) |
| PUT | `/organizations/{id}/mcp-servers/{name}/tools/{tool}` | `{enabled, level}`; only `A0` can be enabled |
| GET / POST | `/apps/{app_id}/processes`, `/apps/{app_id}/processes/{key}/start` | processes the signed-in user may start / start one |
| GET | `/apps/{app_id}/tasks`, `/apps/{app_id}/tasks/{id}` | the task inbox (`scope=mine|available|all`, `status`) |
| POST | `/apps/{app_id}/tasks/{id}/claim`, `/release`, `/delegate`, `/complete` | act on a task (docs/PROCESS-SPEC.md §3) |
| GET | `/apps/{app_id}/process-instances/{id}` | progress of an instance you started |
| GET | `/applications/{id}/process-instances`, `/process-instances/{id}`, `/applications/{id}/user-tasks` | monitoring (viewer and up) |
| POST | `/applications/{id}/processes/{key}/start`, `/process-instances/{id}/cancel`, `/process-instances/{id}/retry` | start for testing / cancel / retry a failed step (editor) |
| PUT/GET/DELETE | `/applications/{id}/oidc`; POST `/applications/{id}/oidc/test` | the application's OIDC provider (admin); check discovery and keys |
| GET | `/apps/{app_id}/auth/oidc/login`, `/auth/oidc/callback` | start / finish an OIDC sign-in (redirects; session cookie as for local login) |
| GET | `/apps/{app_id}/auth/csrf` | a fresh CSRF token for the current session |
| GET | `/apps/{app_id}/pages`, `/apps/{app_id}/pages/{key}` | the menu and one page definition, only for pages the caller may open |
| GET | `/apps/{app_id}/pages/{key}/rows[/{id}]` | rows with only the page's columns and per-row `_actions` |
| POST / PATCH | `/apps/{app_id}/pages/{key}/rows[/{id}]` | submit a create / edit form (declared columns only; `editable_expr` enforced) |
| POST | `/apps/{app_id}/pages/{key}/actions/{action}` | invoke a page action (`{row_id}`); flow-bound actions run as the caller |
| GET | `/governance/policy-rules` | the catalogue of policy rules |
| GET/POST | `/organizations/{id}/policy-packs` | policy packs of the organization (admin): list / create |
| GET/PATCH/DELETE | `/organizations/{id}/policy-packs/{pack}` | read / change rules and status / delete |
| GET | `/organizations/{id}/governance/compliance`, `/inventory`, `/ai-usage` | governance reports (docs/GOVERNANCE.md) |
| GET | `/organizations/{id}/governance/audit-export` | audit events as JSON lines or CSV, paged, hashed, itself audited |
| GET | `/applications/{id}/logs` | application log lines (editor+): filters, newest first, `before_id` paging |
| POST | `/applications/{id}/logs/analyze` | AI analysis of a recent window of log lines; findings cite line ids |
| GET/POST | `/projects/{id}/requirements`, `/stories` | requirements and stories (docs/AGILE.md) |
| POST | `/applications/{id}/requirements/extract` | AI extracts draft requirements from a document |
| GET | `/projects/{id}/test-cases` | generated test cases that can be linked |
| GET/PATCH/DELETE | `/projects/{id}/requirements/{rid}`, `/stories/{sid}` | read / change / delete |
| GET/POST | `/projects/{id}/trace-links` | trace links; `DELETE .../{link}` |
| GET | `/projects/{id}/traceability?format=json\|csv` | coverage and links report |
| GET/POST | `/applications/{id}/feedback` | preview feedback: list (editor) / submit (member) |
| GET/PATCH | `/applications/{id}/feedback/{fid}[/screenshot]` | screenshot / dismiss |
| POST | `/applications/{id}/feedback/cluster` | AI groups new feedback into proposed stories |
| GET | `/healthz`, `/readyz` | liveness / readiness (unauthenticated; docs/OPERATIONS.md) |
| GET | `/metrics` | Prometheus text; bearer `ORCHIVIO_METRICS_TOKEN`, disabled when unset |

### Creating a revision

`POST /applications/{id}/revisions` with `{model, message?, acknowledge_risks?}`:

1. The model (schema 1.0 or 1.1) is migrated and validated: structural problems → `422
   invalid_model` (locations only); semantic errors → `422 model_invalid` with `details` = issues.
2. Identical to the current revision → `409 no_changes`.
3. The diff against the current revision is classified (docs/MODEL-SPEC.md §6). A change at **A3 or
   above** (destructive removal, type change, permission expansion, anonymous access…) is refused
   with `409 approval_required` and the risky changes in `details` unless the request repeats
   with `acknowledge_risks: true`. Rollback applies the same rule to the diff it would cause.
4. Otherwise a new immutable revision is stored with the previous one as parent, and an audit
   event is written.

`acknowledge_risks` is the human confirmation step. Approval by a second person and the proposal
workflow for AI and MCP changes (`/proposals`) are planned.

### MCP server

`POST /api/v1/mcp` implements `initialize`, `ping`, `tools/list` and `tools/call` (protocol versions 2025-06-18 and
2025-03-26; notifications get `202`; batches and `GET` streams are not supported). Sessions are refused: only
`Authorization: Bearer orv_…` tokens, so a token's organization, optional project and **scopes** bound what an agent
can do. Tools run the same code and permission checks as the REST routes:

| Tool | Level | Scope needed |
|---|---|---|
| `application.list`, `model.read`, `model.search`, `model.usages`, `model.lint`, `proposal.get`, `proposal.list` | A0 | `project.read` / `model.read` |
| `model.validate` | A0 | any authenticated token |
| `proposal.create` | A1 | `model.propose` (stored as a pending proposal with `source: "mcp"`) |
| `tests.run` | A1 | `model.propose` |

There is deliberately no tool that applies a proposal, creates a revision or approves anything: a person decides in
the Studio, so an agent cannot raise its own approval level. Tool failures are results with `isError: true` and an
`error` object (`code`, `status`, `details`); malformed JSON-RPC gets protocol errors. Each `tools/call` is audited
(`mcp.tool_call`: tool, SHA-256 of the arguments, outcome — never the arguments themselves) and limited per token
(`ORCHIVIO_MCP_RATE_PER_MINUTE`, default 120; `429`). Not implemented: `model.explain`, the MCP *client* (FR-087),
resources/prompts, SSE.

### Branches and merging

`POST /applications/{id}/revisions` takes an optional `branch` (default `main`). A revision on another branch
extends that branch only; `current_revision_id` follows `main`. Risk gating (A3) is applied where a change
reaches `main`: on a direct revision, or when a branch is merged.

`POST /branches/{id}/merge` with `{into?, resolutions?, dry_run?, message?, acknowledge_risks?}` merges
element by element (docs/MODEL-SPEC.md §11) against the best common ancestor:

| Result | Response |
|---|---|
| nothing new on the branch, or the merge changes nothing | `200 {status: "up_to_date"}` |
| `dry_run` and no conflicts | `200 {status: "clean", changes}`; nothing is written |
| conflicts without a resolution | `409 merge_conflicts`, `details` = conflicts (`id`, `kind`, `element_id`, `property`, `base`, `ours`, `theirs`) |
| merged model invalid (validation or blocking lint) | `422 merge_invalid` |
| merge into `main` at A3 or above | `409 approval_required` unless `acknowledge_risks` |
| merged | `201 {status: "merged", revision_id, revision_no, conflicts_resolved, level, changes}` |

`resolutions` maps a conflict id to `ours`, `theirs` (or `keep`/`drop` for orphaned elements); any other value
leaves the conflict unresolved. The merge revision has two parents (target head, then source head) and the
source branch becomes `merged`. Further codes: `branch_exists`, `invalid_branch_name`, `branch_closed`,
`same_branch`, `no_common_ancestor`.

Further error codes: `approval_required` (409, also for proposals), `stale_proposal`, `not_pending`,
`no_changes`, `ai_not_configured` (503), `external_ai_disabled` (403), `ai_budget_exceeded` (429),
`ai_output_invalid` (422), `ai_provider_error` (502), `preview_not_built` / `preview_stale` (409),
`unknown_role`, `invalid_record` (422), `conflict` (409), `stale_run`, `nothing_to_repair`, `invalid_archive` (422), `unknown_flow`, `not_runnable`, `flow_error`, `hook_failed` (409).

## 5. Still planned

DESIGN.md §10.3 inventory by phase: environments and packages, MCP endpoint,
integrations, end-user (application plane) authentication and a production runtime. Open points:
`Idempotency-Key` for job-creating POSTs; per-token rate limits; cursor pagination if lists grow.

## 6. Compatibility

Versioned by path. Within a major version fields are added, not removed or repurposed;
deprecations are announced one minor release ahead (DESIGN.md §5.1). OpenAPI diffing in CI is
planned.
