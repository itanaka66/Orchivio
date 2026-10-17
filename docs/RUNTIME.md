# Runtime, Preview and Generated Tests

Implementation: `src/orchivio/runtime/`, `src/orchivio/testgen/`,
`src/orchivio/api/routes/{preview,testruns}.py`. Requirements: DESIGN.md §4.4, §4.5, §8.7.

## 1. What the runtime is (and is not)

The runtime **interprets the model**; it does not generate or execute code. From an identified
`AppModel` it builds SQL tables, a CRUD data service and the expression/security machinery that
guards them. Today it runs only as the **preview environment**: a copy of the application with
synthetic data that Studio users can try out. A production runtime with its own end-user
authentication is the next step (see Limitations).

## 2. Tables (`runtime/tables.py`)

- One table per persistent (non-transient) entity, named `app_<12 hex of application id>__<module>__<Entity>`
  (hashed if longer than 60 characters). Platform tables and application tables never share a
  table, and the preview lives in its own database (`ORCHIVIO_PREVIEW_DATABASE_URL`, which `prod`
  requires to differ from `ORCHIVIO_DATABASE_URL`).
- Columns follow the model: types map to string/text/bigint/numeric(28,8)/boolean/date/timestamptz/
  uuid/JSON; enums are strings with a `CHECK`; references are foreign keys to the target's primary
  key (`ref:system.User` is a plain string id); `unique` and model indexes are created.
  *Virtual* computed fields have no column (evaluated on read); *stored* ones do (evaluated on write).
- Differs from ADR-0004's proposal (schema per application) in using a table prefix, which also
  works on SQLite; moving to PostgreSQL schemas is a localised change.

## 3. Data service (`runtime/service.py`)

Every operation follows the same order:

1. Resolve the entity (`404 unknown_entity` for unknown or transient entities).
2. `resolve_access(roles, entity, operation)` → deny by default (`403 forbidden`).
3. Compile the row constraints to a parameterised SQL predicate and AND it into the query (reads,
   counts, updates, deletes). **If a constraint cannot be compiled the operation is denied.**
4. Validate server-side: unknown fields, system-managed fields (generated, computed, primary key on
   update), types, ranges, `max_length`, enums, required, defaults (`default`/`default_expr`),
   computed fields. All problems are reported together (`422 invalid_record`).
5. For creates and updates, evaluate the row constraint on the **resulting** record, so a user cannot
   create a row they could not see or hand a row to someone else (`403`).
6. Check references and uniqueness (`409 conflict`; database constraints are the backstop).
7. Write and emit an application-plane audit event (action, entity, id, acting user and roles; never
   record contents).

Rows hidden by a row constraint behave exactly like missing rows (`404`). Reference checks reveal
whether a referenced id exists regardless of the caller's read access to the target entity.

## 4. Preview environment

| Endpoint | Min. Studio role | Purpose |
|---|---|---|
| `POST /applications/{id}/preview/reset` | editor | Drop and rebuild the tables from the current revision; optionally seed synthetic data |
| `GET  /applications/{id}/preview/status` | viewer | built / stale |
| `GET/POST /applications/{id}/preview/data/{entity}` | viewer (read) / editor (write) | list (filters `f.<field>=…`, `order_by`, `desc`, `limit`≤200, `offset`) / create |
| `GET/PATCH/DELETE …/data/{entity}/{id}` | viewer / editor | |

**View as role.** Requests carry `X-Preview-User` (the end-user id to act as) and `X-Preview-Roles`
(comma-separated roles from the model). The Studio role decides only whether the caller may use the
preview; **it grants nothing inside the simulated application**. With no roles, or a role the model
does not define (`422 unknown_role`), nothing is allowed. This keeps the two security planes apart
(APP-SECURITY.md).

The preview records the model hash it was built from. After a new revision, data calls answer
`409 preview_stale` until the preview is reset. Reset discards preview data; there is no data
migration between model versions.

## 5. Synthetic data (`runtime/synth.py`)

Deterministic for a given seed (`random.Random(f"{seed}:{entity}")`), derived only from the model:
entities are filled in dependency order; references use ids of rows already generated;
`ref:system.User` fields use the supplied preview users; values respect type, `max_length`,
`minimum`/`maximum`, enums and uniqueness. Rows go through the same validation as real writes and are
skipped (and counted) if they cannot satisfy the model. Never derived from production data.

## 6. Generated tests (`testgen/`)

`POST /revisions/{id}/test-runs` derives cases from the model, builds a throw-away runtime in the preview
database, seeds it, runs the cases and drops it. Ids are built from element keys
(e.g. `access:core.Product:viewer:create`), so runs can be compared.

| Kind | What it checks |
|---|---|
| `access` | every entity × role × operation: allowed/denied exactly as `resolve_access` says |
| `row-constraint` | for each constrained rule: SQL filtering equals the interpreter on all rows for several users (read); hidden rows cannot be updated/deleted; creates/updates are refused or accepted according to the constraint evaluated on the resulting record |
| `crud` | create → read → update → delete one record |
| `validation` | required, `max_length`, `minimum`/`maximum`, unique, enum, unknown field |

Results are `passed`, `failed` or `skipped` with a reason (for example no role may create the entity, or
synthetic data cannot satisfy a constraint). **Skipped is not passed**; the report shows coverage of
entities, access rules and row constraints that were actually exercised. The JSON report is stored in the
artifact store (`report_ref`, SHA-256 in the summary), the run is recorded in `builds`/`test_runs`, and
the response compares the run with the previous one for the application (`newly_failing`,
`newly_passing`). `POST /test-runs/{id}/ai/repair` turns failures into a repair *proposal*.

`tests/unit/test_testgen.py` proves the suite can fail: it breaks the runtime on purpose (ignoring roles,
dropping the row filter, skipping validation, letting rows escape a constraint) and requires the
matching cases to fail.

## 7. Limitations

- **Preview only.** No end-user accounts: application users (`app_users`), local authentication and OIDC,
  password flows, and a production deployment path are not built. The preview identity is simulated.
- No schema migration of existing data between revisions (FR-034), no soft delete or optimistic locking,
  no file attributes, no field-level security (FR-064), no relationship traversal in queries.
- Filtering is equality only; sorting is on one stored column. Page and action access (FR-065) is not
  enforced by the data API because there is no UI renderer yet.
- Tests run synchronously inside the request (job-queue offload planned) and only on SQLite so far; the
  PostgreSQL dialect paths (null-safe comparisons, `strpos`, `right`) are unverified.
- Generated tests cannot satisfy arbitrary constraints with synthetic data; such cases are skipped.
