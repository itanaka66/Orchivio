# Application Security Model

Security of **generated applications** (the application plane), as opposed to access to the
Orchivio Studio (the platform plane). Requirements: DESIGN.md §4.7 and §8.7. Implementation:
`src/orchivio/security/` and the `security` block of the model.

## 1. Two planes

| | Platform plane | Application plane |
|---|---|---|
| Who | Studio users (`users`, `memberships`) | End users of a generated app (`app_users`, planned) |
| Roles | owner, admin, editor, viewer | `security.roles` in the model |
| Enforced by | `orchivio.policy.is_allowed` | `orchivio.security` + runtime data layer |
| Audit stream | `audit_events.plane = 'platform'` | `plane = 'application'` |

Roles never inherit across planes: a Studio owner has no data access in a generated app unless
they are also an end user with an app role.

## 2. Evaluation order

authenticate → resolve app roles → page/action access → **entity operation access** → **row
constraint (SQL predicate)** → field filtering (planned) → audit.

Bold steps are implemented in `resolve_access` and `row_predicate`.

## 3. Rules

- **Deny by default.** No matching rule ⇒ denied. Unknown roles, entities and operations are
  denied. Every non-transient entity must have at least one access rule (validation error
  `no-access-rule`) so that "nobody" is a deliberate choice.
- **Union semantics.** A user with several roles gets the union of granted rules.
- **Row constraints.** An optional expression per rule, e.g. `record.owner == $currentUser`.
  Constraints from all granting rules are OR-combined. A granting rule *without* a constraint
  makes the operation unrestricted.
- **Compile or deny.** Constraints compile to a parameterised SQL predicate that the data layer
  must AND into every read, update, delete, count and aggregate. If a constraint cannot be
  compiled the caller must deny (`NotCompilable`), never skip the filter.
- **Single-record checks.** `record_allowed` evaluates the same constraints in the interpreter
  (for example before an update of a row already loaded). Tests assert interpreter and SQL
  agree.
- **Anonymous access** (`security.anonymous: true`) is an explicit opt-in that is classified A3
  by the diff and flagged by the consistency checker.

### Field-level access (FR-064)

An access rule may carry `fields: {read: [...], write: [...]}`. Omitted (or `null`) means every field; a list is
an **allow-list**, so a field added to the entity later stays hidden from restricted rules until it is listed.

* **Hidden means absent.** Fields a caller may not read are removed from list, get, create and update responses
  (no key at all, not `null`). The primary key is always returned so records stay addressable. A role that can
  create but not read gets only the key back.
* **No probing.** Filtering (`f.<field>`) or sorting by a field the caller cannot read fails exactly like an
  unknown field, and a write to a field the caller can neither read nor write is reported as `unknown field`.
  A readable field outside `write` is reported as `read-only for your roles`.
* **Union across roles.** A user's readable (writable) fields are the union of the allow-lists of the rules
  granting that operation; one rule without a list makes everything visible. Lists are combined regardless of row
  constraints. Row constraints themselves may mention hidden fields; they are evaluated on the server.
* **Flows act as the caller.** `retrieve` returns hidden fields as empty, and a `where` that mentions a field the
  caller cannot read stops the flow. Writes from `create`/`change` steps are checked like API writes. Entity hook
  flows are model logic and see the whole record.
* **Computed fields** are values like any other: list them in `read` only if you accept what they reveal. The
  checker warns (`computed-field-leak`) when a readable computed field derives from fields the same rule hides.
* Widening a list (or removing it) is a permission expansion and needs A3 approval; narrowing is an ordinary change.
* The generated OpenAPI lists restricted roles in `x-orchivio-field-access` and marks as `required` only fields that
  every role that can read the entity receives.

## 4. Consistency checker

`check_consistency(model)` reports:

| Code | Severity | Meaning |
|---|---|---|
| `page-exposes-unreadable-entity` | error | A role can open a page but cannot read the page's entity |
| `unreachable-page` | warning | Page lists no roles, so nobody can open it |
| `role-without-access` | warning | Role has no access rules and no pages |
| `entity-without-readers` | warning | No role can read the entity |
| `anonymous-access` | warning | Anonymous access is on (needs A3 approval) |
| `computed-field-leak` | warning | A readable computed field is derived from fields the same rule hides |
| `required-field-not-writable` | warning | A role may create the entity but cannot write a required field that has no default |

## 5. Usage sketch

```python
decision = resolve_access(model, user_roles, "core.Product", "update")
pred = row_predicate(decision, SqlContext(current_user=UserRef(user_id, user_roles)))
rows = session.execute(text(f"UPDATE product SET ... WHERE id = :id AND {pred.sql}"),
                       {"id": rid, **pred.params})
```

`DENY_ALL` / `ALLOW_ALL` predicates are returned for the denied and unrestricted cases, so the
caller always has a predicate to apply.

## 6. End-user authentication (local)

Generated applications have their own identity store (`app_users`, `app_user_roles`, `app_sessions`),
separate from Studio accounts (FR-067).

* **Management** (Studio, admin-only `appuser.manage`): `/applications/{id}/app-users`, `/app-users/{id}`,
  `.../reset-password`. Roles must be defined by the model. The admin sets a temporary password; the user must
  change it at first sign-in (`password_change_required` until then). Disabling a user or resetting the password
  ends all their sessions.
* **Sign-in** (`/apps/{app_id}/auth/login|logout|me|change-password`): same password hashing, throttle and
  uniform error as the Studio. The session cookie is `<session cookie>_app`, `HttpOnly`, scoped by path to
  `/api/v1/apps/{app_id}`, with a per-session CSRF token required on unsafe requests. A session is bound to its
  application, so a stolen cookie is useless for another one.
* **Plane separation (FR-060).** The `/apps/...` endpoints accept only application sessions; Studio cookies and
  `orv_` tokens get `401`. Application sessions and `oas_` tokens are unknown to every Studio endpoint.
* **Authorization.** The caller's roles are read from the database on each request, intersected with the roles of
  the current model, and passed to the same data service as in the preview (`/apps/{id}/data/{entity}`,
  `/apps/{id}/flows/{flow}/run`). A role change applies immediately; a user with no roles can do nothing.
* **Anonymous access** is off unless `security.anonymous` is true (an A3 change). Anonymous callers get only the
  role named `anonymous`, if the model defines it, and can never sign out or change a password.
* **Audit.** Sign-ins, failures, logouts and password changes are application-plane events
  (`appauth.*`, `plane = application`); management is platform-plane (`appuser.*`). Passwords are never logged.
* **Data lives in the preview runtime** until a production data plane exists, so these endpoints serve preview data.

## 7. Not yet implemented

Page and action enforcement in a running app, OIDC and SAML sign-in, self-service
registration and password reset by email, MFA, a shared (multi-replica) login throttle, "view as role" preview, generated positive/negative tests
per rule (FR-063 acceptance), PostgreSQL row-level security as defence in depth
([ADR-0003](adr/0003-app-security-enforcement.md)).
