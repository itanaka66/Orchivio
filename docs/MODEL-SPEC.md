# Application Model Specification (schema 1.1)

Authoritative definitions live in `src/orchivio/model/schema.py`; this document explains them.
Sample: [`examples/inventory-app/model.yaml`](../examples/inventory-app/model.yaml).

## 1. Structure

```yaml
schema_version: "1.1"
application: { key, name, version, default_locale, locales }
modules:
  - key: core
    enumerations: [ { name, values } ]
    entities:     [ { name, display_name, transient, fields, indexes, hooks } ]
    pages:        [ { key, path, type, entity, mode, layout, columns, actions } ]
    flows:        [ { key, kind, params, steps, edges } ]      # see FLOW-SPEC.md
security:
  roles: [ ... ]
  access: [ { entity, role, operations, row_constraint } ]
  pages: { page_key: [roles] }
  anonymous: false
```

Unknown properties are rejected (`extra="forbid"`) so typos and hallucinated keys from an LLM
fail at the schema boundary instead of being silently dropped.

## 2. Naming

| Kind | Pattern |
|---|---|
| module key, role, field name | `^[a-z][a-z0-9_]*$` |
| entity, enumeration | `^[A-Za-z][A-Za-z0-9]*$` |
| page key | `^[a-z][a-z0-9-]*$` |
| enum value | `^[a-z][a-z0-9_]*$` |
| route | starts with `/`, `[A-Za-z0-9/_:-]` only |

Entities are addressed as `module.Entity` in security rules; inside a module, `Entity` suffices.

## 3. Field types

`string` · `text` · `integer` · `decimal` · `boolean` · `date` · `datetime` · `uuid` · `json` ·
`enum:Name` · `ref:Entity` (or `ref:module.Entity`; `ref:system.User` is the built-in end user).

Constraints: `required`, `unique`, `primary_key`, `generated`, `max_length` (string/text),
`minimum`/`maximum` (integer/decimal), `default` (literal), `default_expr`, `computed`.

Expression typing: a field is *nullable* unless it is `required`, a primary key, `generated` or
computed. `json` fields are not available in expressions. `date` is treated as `datetime`.

## 4. Element identity

Every module, enumeration, entity, field, page and access rule has an `element_id` (UUID).
IDs are assigned by `assign_element_ids` and are what the diff matches on, so **renames are
property changes, not delete + create**. A client that omits ids gets the ids of same-named
elements from the previous revision; to express a rename, send the old `element_id` with the new
name.

## 5. Validation rules

`validate_model` returns issues with a code, a JSON-pointer-style path and the element id.
Error-severity issues block creating a revision.

| Code | Severity | Meaning |
|---|---|---|
| `duplicate-module/-enum/-entity/-field/-page/-route/-role/-access/-enum-value` | error | Name or key reused (access: same entity + role twice) |
| `bad-type`, `unknown-enum`, `unknown-entity` | error | Type string invalid or target missing |
| `no-primary-key`, `multiple-primary-keys` | error | Non-transient entities need exactly one |
| `ref-to-transient`, `bad-generated` | error | Reference to a transient entity; `generated` on a type other than uuid/datetime |
| `bad-constraint`, `bad-default`, `bad-computed` | error | Constraint doesn't fit the type; computed field is also a key/unique/generated/defaulted |
| `unknown-index-field`, `unknown-column` | error | Reference to a missing field |
| `missing-mode` | error | `form` page without `create`/`edit` |
| `expression` | error | Expression fails to parse or type-check (message includes position) |
| `computed-cycle` | error | Computed fields depend on each other (`a -> b -> a`) |
| `export` operation | - | An access rule may list `export` next to `read/create/update/delete`; it is not part of the generated CRUD test matrix |
| `unknown-access-field`, `duplicate-access-field`, `access-field-unused` | error | Field lists of an access rule (docs/APP-SECURITY.md §3) |
| `duplicate-flow`, `flow-*`, `edge-*`, `step-not-allowed`, `missing-branch`, `unreachable-step`, `loop-*`, `duplicate-variable` | error | Flow structure and scoping rules (FLOW-SPEC.md) |
| `unknown-role`, `unknown-page` | error | Security block references something undefined |
| `no-access-rule` | error | Non-transient entity without any access rule (deny-by-default must be explicit) |
| `hooks-not-supported` | warning | Entity hooks need Server Flows (not yet available) |

Expressions are checked in context: `default_expr` has no `record`; `computed.expr`,
`row_constraint` and page action expressions see the entity as `record`. Computed fields must
yield a non-null value of the field's type (use `??`).

Security consistency (separate pass, `check_consistency`) is described in
[APP-SECURITY.md](APP-SECURITY.md).

## 6. Diff and risk classification

`diff_models(base, new)` requires identified models and returns changes sorted
removed → added → modified. Each change carries risks and an approval level (A2 by default; see
DESIGN.md §12.3):

| Risk | Raised by | Level |
|---|---|---|
| `destructive` | removing an entity, field, enumeration or module | A3 |
| `type-change` | field type changed | A3 |
| `enum-value-removed` | enumeration value removed | A3 |
| `required-added` | field becomes required | A2 (flagged) |
| `rename` | name/key changed on an element with the same id | A2 (flagged) |
| `permission-grant` | new access rule | A3 |
| `permission-expansion` | rule gains operations or loses its row constraint | A3 |
| `anonymous-access` | `security.anonymous` turned on | A3 |

Changes to roles, page access or `anonymous` surface as one `security` change at A3. Removing
an access rule narrows permissions and carries no risk flag. `ModelDiff.level` is the maximum.

## 7. Hashing and storage

`model_hash` = SHA-256 over canonical JSON (sorted keys, no whitespace, `None` omitted). Equal
models hash equal regardless of YAML key order; revisions store the hash for integrity and
quick equality checks.

## 8. Migration from schema 1.0

`migrate_to_latest` wraps flat `entities`/`pages` into module `core`, turns each entity's
`permissions` into `security.access` rules (one per entity and role) and grants each page to the
roles that can read its entity. It never modifies its input. Unknown versions raise
`UnsupportedSchemaVersion`.

## 9. Canonical files and archives

The model can be written as one small YAML file per element (`orchivio export`, `GET /revisions/{id}/export`),
so Git diffs and pull requests show exactly which element changed:

```
application.yaml                          format, schema version, application info, module order
modules/<module>/module.yaml              module id and the order of its elements
modules/<module>/{enumerations,entities,pages,flows}/<Name>.yaml
security/security.yaml                    roles, page access, anonymous flag, rule order
security/access/<entity>__<role>.yaml     one file per access rule
```

Properties (checked by property tests over generated models, including arbitrary unicode):

- **Lossless.** `from_files(to_files(m)) == m`, element order and ids included; writing is idempotent.
- **Minimal and stable.** Keys follow the schema's declaration order; defaults are omitted and restored on
  load; the same model always produces the same bytes; editing one element changes one file.
- **Strict reading.** Unknown, unreferenced or missing files, bad YAML (only `safe_load` is used) and schema
  violations are errors with the offending path. Names that differ only by letter case are refused on write
  because they cannot live on case-insensitive file systems.
- **Archives.** `build_archive` writes a zip with sorted entries, fixed timestamps and no compression
  (compression output differs between zlib builds), plus `MANIFEST.json` with a SHA-256 per file and the model
  hash. `read_archive` verifies all of them and defends against hostile input: file count and size limits, a
  compression-ratio guard, regular files only, canonical paths only.
- **Governance.** Exporting needs the admin-level `project.export` permission and is audited
  (`revision.exported`). Importing (`POST /applications/{id}/import`, raw `application/zip` body) creates a
  *proposal*; an import never changes the model directly, so it is diffed, risk-classified and approved like any
  other change.

## 10. Generated OpenAPI

`orchivio openapi model.yaml` and `GET /revisions/{id}/openapi.json` produce an OpenAPI 3.1 document of the data API: `/data/{module.Entity}` (list, create) and `/data/{module.Entity}/{id}` (read, update, delete), with `Entity`, `EntityCreate`, `EntityUpdate` and `EntityPage` schemas derived from the fields (types, `maxLength`, `minimum`/`maximum`, enums, references as their key type; generated and computed fields are `readOnly` and absent from inputs; unknown properties are rejected). Operations no role may perform are omitted; `x-orchivio-roles` and `x-orchivio-row-constraints` document who may do what. In the Studio preview the paths are served under `/api/v1/applications/{id}/preview`. Decimals are returned as strings to keep precision. A contract test validates real responses and the accept/reject behaviour of request bodies against these schemas.

## 11. Branches and merging

Branches are named pointers to a head revision. `merge_branches` finds the best common ancestor (the common
ancestor with the highest revision number that is not an ancestor of another), flattens the three models into
nodes keyed by `element_id` (steps, edges, parameters and tests of a flow, lint suppressions and the
application/security records are nodes too) and merges property by property (`orchivio.model.merge`):

- One side changed, or both changed identically → that value. Layout (step positions) never conflicts.
- Set-like properties (enum values, access operations, roles, locales) and dictionaries (hooks, page
  permissions, step config) are merged by member, so only a member changed differently on both sides conflicts.
- Conflict kinds: `property` (both changed the same property differently), `delete-edit` (one side deleted an
  element the other changed), `orphan` (an element whose parent was deleted on the other side), `order` (both
  moved an element to different places) and `invalid` (the combination violates the schema; nothing can be
  chosen, someone has to edit the result).
- Ordered lists keep our order and insert their additions after their predecessor.
- The merged model is validated and linted like any other revision; an invalid merge writes nothing.

Merge properties (idempotent, commutative when both sides agree, identity with an unchanged side) are checked
with Hypothesis in `tests/unit/test_merge.py`.

## 12. Schedules

A server flow may have `schedule: {cron, run_as_role, args, enabled, missed_runs, max_attempts, backoff_seconds}`.
`cron` is five fields in UTC (`orchivio.model.cron`: lists, ranges, steps, `*`; Sunday is 0 or 7; day-of-month
and day-of-week are OR-ed when both are restricted; no names or `L`/`W`/`#`). Rules: `schedule-not-server`,
`schedule-hook`, `bad-cron` (also for expressions that never fire, such as 30 Feb), `unknown-role`, and
`schedule-args` (each parameter needs a literal value that fits its type; no unknown names). Execution semantics
are in docs/OPERATIONS.md §4. Adding a schedule is an ordinary property change of the flow in diffs and merges.

## 13. Integrations (consumed REST APIs)

`modules[].integrations[]` (omitted when empty, so models without any keep their hash) describe external REST APIs
a flow may call (FR-081):

```yaml
integrations:
  - key: billing
    destination: billing-api          # a logical name; the real base URL and credential are configured per application
    operations:
      - id: get_rate
        method: GET
        path: /rates/{currency_code}   # placeholders name path parameters
        params: [ { name: currency_code, location: path, type: string } ]   # type: string|integer|decimal|boolean; json_name = wire name if different
        response: Rate                 # a transient entity of the module; response_kind: object | list
        response_map: { total_amount: totalAmount }   # entity field -> JSON property, where different
      - { id: create_invoice, method: POST, path: /invoices, body: InvoiceRequest, response: InvoiceReceipt, response_kind: object }
```

Bodies and responses are typed by **transient entities** (no table), so expressions use them like records
(`rate.rate > 100`). Validation: `duplicate-integration`, `duplicate-operation`, `duplicate-param`, `integration-path`
(placeholders and declared path parameters must match; path parameters are required), `integration-body` (no body on
GET/DELETE), `integration-response`, `integration-entity` (must be a transient entity of the model), `integration-map`;
and for flows the `call_integration` step checks (known integration and operation, typed arguments, all required body
fields, `into` exactly when there is a response). Flow tests give canned answers with `mocks:
{"<integration>.<operation>": {status, json}}`; a call without a mock fails the test. Adding an integration, or a flow
step that calls an operation the flow did not call before, is classified A3 (`external-integration`, `external-call`).

## 14. Processes

`modules[].processes[]` (omitted when empty) define long-running business processes with user tasks, system tasks,
decisions, parallel paths and timers; see docs/PROCESS-SPEC.md. Rules: `duplicate-process`, `process-start`,
`process-end`, `duplicate-step`, `duplicate-edge`, `edge-unknown-step`, `process-edges`, `process-join`,
`process-config`, `process-assignee`, `process-escalation`, `process-variable`, `process-run-as`,
`process-sync-cycle`, `process-dead-end`, `unreachable-step`, plus `unknown-role` and `expression`. Widening
`start_roles` or changing `run_as_role` is classified A3.

## 15. Page actions and direct flow access

`pages[].actions[]` entries are a key or `{key, visible_expr, editable_expr, flow, args}`; `flow` names a server flow of
the module (not an entity hook) and `args` gives an expression per flow parameter over the row (`page-action-flow`).
`flows[].run_roles` lists the roles that may run the flow directly through the application API (`unknown-role`,
`run-roles-not-runnable` for client and hook flows); omitted means nobody. See docs/APP-SECURITY.md §8.

## 16. Planned

Flows, processes, scheduled jobs, integrations, AI steps, themes, translations, field-level
security, per-element canonical YAML files (DESIGN.md §8.9).
