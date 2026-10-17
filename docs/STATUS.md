# Implementation Status

Last updated: 2026-10-17 (initial implementation). Statuses: **Done** (implemented and
tested), **Library** (implemented as a tested library but not yet wired into a running
service), **Schema** (data structures only), **Planned**.

## Requirements

| ID | Requirement | Status | Notes |
|---|---|---|---|
| FR-001/002 | Auth, organizations, projects | Schema | Tables exist; no endpoints, no password handling yet |
| FR-003 | Platform RBAC | Library | `orchivio.policy.is_allowed`; not enforced by any endpoint yet |
| FR-005 | Project export/import | Planned | Canonical JSON hash exists; no export format/endpoint |
| FR-010..015 | AI intake, plans, proposals, approval | Schema | `proposals`, `ai_jobs`, `ai_*` tables; no orchestrator |
| FR-018 | Provider adapters | Library | Interface + mock; no real provider adapter |
| FR-020 | Entities, fields, relationships | Done | Schema + validation (no runtime) |
| FR-021/022 | Pages, forms, lists | Schema | Validated; no renderer |
| FR-023 | Declarative validation | Schema | Constraints modelled; no runtime enforcement |
| FR-025 | Immutable revisions | Done | `RevisionRepository`; PostgreSQL trigger **not yet run against PostgreSQL** |
| FR-026 | Diff and rollback | Done | Library + repository; rollback creates a new revision |
| FR-027 | Static model checks | Done | See MODEL-SPEC.md rule table |
| FR-028 | Enumerations | Done | Removal of a value is flagged risky |
| FR-029 | Modules | Schema | Multiple modules validate; cross-module enum use in expressions is limited |
| FR-043 | Audit logs | Partial | Revision events recorded; append-only trigger is PostgreSQL-only and untested |
| FR-050 | Typed expression language | Done | Parser, checker, interpreter, limits |
| FR-051 | Computed fields | Partial | Type-checked, cycle detection; no stored/virtual runtime |
| FR-060..063 | Two-plane security, row constraints | Library | Access resolution + SQL compiler; no runtime data layer yet |
| FR-066 | Consistency checker | Done | 5 checks |
| FR-090 | Branch-ready revision graph | Done | `parent_revision_ids`, `branches` |
| FR-122 | Dependency graph | Schema | `element_refs` table only |
| FR-120 | Model linter | Planned | Not started (target ≥ 20 rules) |
| FR-030..036 | CRUD runtime, UI renderer, OpenAPI generation, preview | Planned | |
| FR-040 | Test generation and execution | Planned | |
| FR-127/128 | Explain mode, synthetic data | Planned | |

Everything not listed is **Planned**.

## What has not been verified

- **No test has run against PostgreSQL.** Migrations and repositories are tested on SQLite. The
  PostgreSQL-specific parts (circular foreign keys, immutability triggers, JSONB/UUID[] types,
  `IS DISTINCT FROM` predicates) are exercised only by the `postgres-migrations` CI job, which
  has not run yet.
- `Dockerfile`, `docker-compose.yml` and `.github/workflows/ci.yml` have not been run.
- `ruff` was not available when the code was written; lint cleanliness is unchecked.
- The SQL predicate compiler is checked against SQLite only.

## Known limitations

- Request-size limiting trusts `Content-Length`; enforce a body limit at the reverse proxy too.
- Stateless API endpoints accept a single static bearer token; real authentication is Phase 1.
- Expressions: no relationship traversal (`record.owner.name`), no empty list literals, no
  object literals; `/` and `%` do not compile to SQL (see EXPRESSION-SPEC.md).
- Enumerations from another module cannot be named inside expressions.
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
