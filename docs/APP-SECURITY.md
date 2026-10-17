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

## 4. Consistency checker

`check_consistency(model)` reports:

| Code | Severity | Meaning |
|---|---|---|
| `page-exposes-unreadable-entity` | error | A role can open a page but cannot read the page's entity |
| `unreachable-page` | warning | Page lists no roles, so nobody can open it |
| `role-without-access` | warning | Role has no access rules and no pages |
| `entity-without-readers` | warning | No role can read the entity |
| `anonymous-access` | warning | Anonymous access is on (needs A3 approval) |

## 5. Usage sketch

```python
decision = resolve_access(model, user_roles, "core.Product", "update")
pred = row_predicate(decision, SqlContext(current_user=UserRef(user_id, user_roles)))
rows = session.execute(text(f"UPDATE product SET ... WHERE id = :id AND {pred.sql}"),
                       {"id": rid, **pred.params})
```

`DENY_ALL` / `ALLOW_ALL` predicates are returned for the denied and unrestricted cases, so the
caller always has a predicate to apply.

## 6. Not yet implemented

Field-level read/write (FR-064), page and action enforcement in a running app, end-user
authentication (local, then OIDC), "view as role" preview, generated positive/negative tests
per rule (FR-063 acceptance), PostgreSQL row-level security as defence in depth
([ADR-0003](adr/0003-app-security-enforcement.md)).
