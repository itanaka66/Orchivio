# HTTP API Specification

Base path `/api/v1`. The running service publishes its own OpenAPI document at
`/api/openapi.json` and interactive docs at `/api/docs`; that document is authoritative for
implemented endpoints. This file records conventions and the plan.

## 1. Conventions

- JSON requests and responses; UTF-8.
- **Errors** use one envelope and never include stack traces or internals, and do not echo submitted model contents:
  ```json
  { "error": { "code": "invalid_model", "message": "model failed schema validation",
               "details": [ { "loc": ["application", "name"], "message": "String should have at least 1 character" } ] } }
  ```
  Codes in use: `invalid_request`, `invalid_model`, `unsupported_schema_version`,
  `unknown_entity`, `unauthorized`, `payload_too_large` (413), `internal_error` (500).
- **Authentication**: `Authorization: Bearer <token>`. Today a single static token
  (`ORCHIVIO_API_TOKEN`) protects `/api/v1/*`; `ORCHIVIO_ENV=prod` refuses to start without one.
  `/healthz` is always open. Real sessions, CSRF protection and API tokens arrive in Phase 1.
- **Limits**: request bodies over `ORCHIVIO_MAX_BODY_BYTES` (1 MiB) are rejected when the client
  declares a length; also enforce a limit at the reverse proxy.
- **Pagination** (planned resources): `limit` (default 50, max 200) and `offset`; bounded
  server-side.
- **Idempotency**: destructive or job-creating POSTs accept an `Idempotency-Key` header
  (planned).
- **Authorization** is checked per resource, not per route: every id is resolved through the
  caller's organization membership before use.

## 2. Implemented endpoints

### `GET /healthz`
`{"status": "ok", "version": "0.1.0.dev0"}`

### `POST /api/v1/models/validate`
Body `{"model": {...}}` (schema 1.0 or 1.1; 1.0 is migrated). Returns
`{valid, schema_version, model_hash, issues[], security_findings[]}`. Schema violations return
422 `invalid_model`; semantic problems are returned as `issues` with `valid: false`.

### `POST /api/v1/models/diff`
Body `{"base": {...}, "new": {...}}`. Element ids are assigned (the new model inherits ids by
name) and the diff is returned as `{level, security_changed, changes[]}`, each change with
`kind`, `element_type`, `element_id`, `name`, `properties`, `risks`, `level` (A0–A4).

### `POST /api/v1/expressions/check`
Body `{model, entity?, expression, expected?}` (`expected` ∈ bool/int/decimal/string/datetime).
Returns `{valid: true, type, fields}` or `{valid: false, error: {message, position}}`.

## 3. Planned resources

DESIGN.md §10.2/10.3 lists the intended inventory. Proposed refinements, because §10.2 mixes
nested and flat paths and asks for a naming review:

| Rule | Example |
|---|---|
| Collections nest only under their parent *for create and list* | `GET/POST /projects/{pid}/applications` |
| Items are addressed flat by id, authorized through their ancestors | `GET /applications/{id}`, `GET /revisions/{id}` |
| Actions are sub-resources with a verb that is a noun or past participle where possible | `POST /proposals/{id}/approval`, `POST /branches/{id}/merge` |
| Long operations return a job | `POST /revisions/{id}/builds` → `202` + `GET /builds/{id}` |
| AI approval goes through proposals, not jobs | `POST /proposals/{id}/approval` (replaces `/ai-jobs/{id}/approve`) so that AI, MCP and human changes share one endpoint |

Phase mapping follows DESIGN.md §15 (auth/orgs/projects in Phase 1, revisions/diff in Phase 2,
proposals/AI jobs in Phase 4, builds/tests in Phase 5, …).

## 4. Compatibility

The API is versioned by path. Within a major version, fields are added but not removed or
repurposed; deprecations are announced one minor release ahead (DESIGN.md §5.1). OpenAPI changes
are checked in CI once the schema is committed (planned).
