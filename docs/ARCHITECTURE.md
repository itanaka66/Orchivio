# Architecture (as built)

DESIGN.md §6 describes the target architecture. This document describes the code that exists
and the rules that keep it evolvable. Decision: [ADR-0001](adr/0001-repository-layout.md).

## Packages

One Python distribution (`orchivio`) with strictly layered sub-packages. DESIGN.md §14 proposes
a multi-package monorepo; we start with a single distribution and **enforce the same
boundaries in a test** so packages can be split later without untangling imports.

| Package | Responsibility | May import |
|---|---|---|
| `orchivio.runtime` | Tables from a model, CRUD data service with access/row rules, synthetic data, preview store | expr, model, policy, ports, security |
| `orchivio.logic` | Logic Engine: runs flows, entity hooks, flow tests | expr, model, runtime |
| `orchivio.testgen` | Test-case derivation and runner | expr, logic, model, runtime, security |
| `orchivio.ai` | AI Builder: prompts, analysis, model proposals, explanations | llm, model |
| `orchivio.auth` | Password hashing (scrypt), opaque token generation/hashing | – |
| `orchivio.expr` | Parse, type-check, evaluate, and SQL-compile expressions | – |
| `orchivio.policy` | Approval levels A0–A4, platform RBAC matrix | – |
| `orchivio.llm` | Provider-neutral LLM interface, mock, validated generation | – |
| `orchivio.ports` | Job queue, artifact store, audit sink interfaces + reference adapters | – |
| `orchivio.model` | Model schema, validation, element IDs, diff, migration | expr, policy |
| `orchivio.security` | Application-plane access resolution, consistency checker | expr, model, policy |
| `orchivio.db` | ORM models, sessions, revision and proposal repositories | model, policy |
| `orchivio.api` | FastAPI application, settings, auth, authorization, routes | everything above |

`runtime` defines the `Hooks` protocol and `logic` implements it, so the data service can call flows without importing the engine.

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
- `db` depends on `model` and the pure `policy` functions (approval levels); persistence never
  reaches into security or HTTP concerns.
- `llm` and `ports` are leaf interfaces. Nothing in the domain imports a vendor SDK.

## Web Studio

`apps/web` is a separate React/TypeScript application (Vite). It talks to the API only over HTTP (same origin in production, a dev proxy locally) and shares nothing with the Python code except generated files: `scripts/export_web_contract.py` writes the flow contract and sample data into `apps/web/src/flow/`, and a backend test fails if they are stale.

## Request flow (today)

```
HTTP ─▶ middleware (request id, size limit, security headers, access log)
     ─▶ current_principal   (bearer token OR session cookie + CSRF; disabled users rejected)
     ─▶ authorize(org, action)   404 if not a member, 403 if role/scope insufficient
     ─▶ load_*: resource → owning organization (every id is resolved through its tenant)
     ─▶ handler ─▶ db repositories / model library ─▶ audit event ─▶ commit
```

Handlers commit explicitly, together with their audit event, so an action and its audit record
are one transaction.

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
integrity only. The API layer authorizes first (`api/deps.py`), then calls the repository. Its
`plan()` dry run (ids, validation, diff) is what lets the API refuse risky changes before
anything is written.

## Not built yet (from DESIGN.md §6)

Web Studio, AI orchestrator, runtime engines (logic, process, scheduler, integration, AI),
delivery manager, isolated workers, MCP server/client, telemetry export.
