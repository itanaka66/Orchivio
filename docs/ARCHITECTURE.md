# Architecture (as built)

DESIGN.md §6 describes the target architecture. This document describes the code that exists
and the rules that keep it evolvable. Decision: [ADR-0001](adr/0001-repository-layout.md).

## Packages

One Python distribution (`orchivio`) with strictly layered sub-packages. DESIGN.md §14 proposes
a multi-package monorepo; we start with a single distribution and **enforce the same
boundaries in a test** so packages can be split later without untangling imports.

| Package | Responsibility | May import |
|---|---|---|
| `orchivio.expr` | Parse, type-check, evaluate, and SQL-compile expressions | – |
| `orchivio.policy` | Approval levels A0–A4, platform RBAC matrix | – |
| `orchivio.llm` | Provider-neutral LLM interface, mock, validated generation | – |
| `orchivio.ports` | Job queue, artifact store, audit sink interfaces + reference adapters | – |
| `orchivio.model` | Model schema, validation, element IDs, diff, migration | expr, policy |
| `orchivio.security` | Application-plane access resolution, consistency checker | expr, model, policy |
| `orchivio.db` | ORM models, sessions, revision repository | model |
| `orchivio.api` | FastAPI application, settings | everything above |

`tests/unit/test_architecture.py` fails if a package imports outside its row. Add a new package
to that table *and* the test in the same change.

```
            api
   ┌─────┬───┴───┬────────┬──────┐
   db  security  llm    ports   (policy)
   │      │
   └─ model ─┘
      │   └── policy
      expr
```

## Why the boundaries look like this

- `expr` knows nothing about the application model. It asks for field and enum types through the
  `SchemaTypes` protocol, so the model can depend on it and not the reverse.
- `policy` is pure functions so the same rules can guard the REST API, the future Studio MCP
  server and the AI orchestrator (DESIGN.md §6.2: one proposal/approval pipeline, no side
  channel).
- `db` depends only on `model`: persistence never reaches into security or HTTP concerns.
- `llm` and `ports` are leaf interfaces. Nothing in the domain imports a vendor SDK.

## Request flow (today)

```
HTTP ─▶ api (size limit, bearer token, error envelope)
          ├─ migrate_to_latest ─▶ AppModel.model_validate     (schema errors → 422, values not echoed)
          ├─ validate_model ─▶ expr.check for every expression
          ├─ check_consistency
          └─ diff_models (after assign_element_ids)
```

## Revision write path (library)

```
RevisionRepository.create
  lock application row
  assign_element_ids(model, previous=current revision)
  validate_model  ── errors? ─▶ ModelInvalid, nothing written
  insert AppRevision(parents=[current], hash, model_json)
  update application.current_revision_id and branch head
  insert AuditEvent
```

Authorization (`policy.is_allowed`) is the **caller's** job; the repository enforces model
integrity only. This will be wrapped by service functions in Phase 1 that take a verified
principal.

## Not built yet (from DESIGN.md §6)

Web Studio, AI orchestrator, runtime engines (logic, process, scheduler, integration, AI),
delivery manager, isolated workers, MCP server/client, telemetry export.
