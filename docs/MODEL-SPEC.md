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

## 9. Planned

Flows, processes, scheduled jobs, integrations, AI steps, themes, translations, field-level
security, per-element canonical YAML files (DESIGN.md §8.9).
