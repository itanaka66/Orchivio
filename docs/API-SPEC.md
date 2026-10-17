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
| GET | `/revisions/{id}/diff/{base_id}` | both must belong to the same application |
| POST | `/applications/{id}/rollback` | restores an earlier model as a *new* revision |
| GET/POST/DELETE | `/tokens`, `/tokens/{id}` | own tokens; the secret is returned once at creation |
| GET | `/revisions/{id}/lint`, `/revisions/{id}/elements/{element_id}/usages` | stored lint findings (`include_suppressed`) / find usages |
| GET/POST | `/applications/{id}/proposals`, `/proposals/{id}`, `/proposals/{id}/approval` | change proposals and their decision (docs/AI-BUILDER.md) |
| POST | `/applications/{id}/ai/analyze`, `/ai/propose`, `/ai/explain` | AI Builder (needs a configured provider) |
| POST | `/applications/{id}/preview/reset`; GET `…/preview/status`; CRUD `…/preview/data/{entity}[/{id}]` | preview runtime (docs/RUNTIME.md) |
| POST/GET | `/revisions/{id}/test-runs`, `/test-runs/{id}`, POST `/test-runs/{id}/ai/repair` | generated tests |
| POST | `/models/validate`, `/models/validate-flow`, `/models/lint`, `/models/diff`, `/expressions/check` | stateless tools (any authenticated caller); `validate-flow` checks one unsaved flow in a model's context and returns issue paths ending in `steps/<id>…` |
| GET | `/healthz` | unauthenticated |

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

Further error codes: `approval_required` (409, also for proposals), `stale_proposal`, `not_pending`,
`no_changes`, `ai_not_configured` (503), `external_ai_disabled` (403), `ai_budget_exceeded` (429),
`ai_output_invalid` (422), `ai_provider_error` (502), `preview_not_built` / `preview_stale` (409),
`unknown_role`, `invalid_record` (422), `conflict` (409), `stale_run`, `nothing_to_repair`.

## 5. Still planned

DESIGN.md §10.3 inventory by phase: branches and merge, environments and packages, MCP endpoint,
integrations, end-user (application plane) authentication and a production runtime. Open points:
`Idempotency-Key` for job-creating POSTs; per-token rate limits; cursor pagination if lists grow.

## 6. Compatibility

Versioned by path. Within a major version fields are added, not removed or repurposed;
deprecations are announced one minor release ahead (DESIGN.md §5.1). OpenAPI diffing in CI is
planned.
