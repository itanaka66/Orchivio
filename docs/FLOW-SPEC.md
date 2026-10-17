# Flow Specification (Server and Client Flows)

Implementation: schema `src/orchivio/model/schema.py` (`FlowDef`), validator
`src/orchivio/model/flows.py`, editor `apps/web/src/flow/`. Requirements: DESIGN.md §4.6 (FR-052,
FR-053), §8.6.

> **Status.** Server flows can be modelled, validated, versioned, diffed, edited visually and **run** by the
> Logic Engine (§5) in the preview environment, as entity event hooks (§6), and from declarative tests
> (§7). Client flows are modelled and validated only; the browser runtime does not exist yet.

## 1. Model

A flow lives in a module (`modules[].flows[]`) and is a directed **acyclic** graph:

```yaml
flows:
  - key: ReorderLowStock          # letters, digits, underscore
    kind: server                  # server | client
    description: …
    params: [ { name: min_gap, type: integer } ]      # same type syntax as entity fields
    steps:
      - { id: find, type: retrieve, label: Find products, position: { x: 0, y: 120 },
          config: { entity: Product, where: "quantity <= reorder_level - min_gap", into: low, many: true, limit: 100 } }
      - …
    edges:
      - { id: e3, source: any, target: each, handle: "true" }
```

`position` is editor layout only: it never changes behaviour and is ignored by the diff, so moving
boxes around does not create a model change (or an approval).

## 2. Steps

| Type | Settings | Outputs | Variable it defines |
|---|---|---|---|
| `start` | – | `out` | – (exactly one per flow) |
| `retrieve` | `entity`, `where?` (boolean; record fields and variables), `into`, `many?`, `limit?` (1–1000, default 100) | `out` | `into`: a record (nullable) or, with `many`, a list of records |
| `create` | `entity`, `values` (expression per field), `into?` | `out` | `into`: the new record |
| `change` | `target` (record variable), `values` | `out` | – |
| `delete` | `target` | `out` | – |
| `decision` | `expr` (boolean) | `true`, `false` | – |
| `loop` | `over` (list variable), `item`, `max_iterations` (1–1000, **required**) | `body`, `done` | `item`: a record, visible in the body only |
| `call_flow` | `flow`, `args` (expression per parameter) | `out` | – |
| `raise_error` | `message` (1–200 characters) | none (terminal) | – |
| `end` | – | none (terminal) | – |

Client flows may use only `start`, `decision`, `call_flow` and `end`; they cannot read or write data
(FR-053). A **server** flow may call only server flows; a client flow may call either kind.

## 3. Rules

Structure (`edge-*`, `flow-*`, `missing-branch`, …):

- Step and edge ids are unique; edges connect existing steps; no self-loops; nothing connects into
  `start`; an output carries at most one edge; each handle must be valid for its step type.
- `decision` needs both `true` and `false` connected, `loop` needs `body` and `done`.
- Every step is reachable from `start`; `start` is connected.
- **No cycles.** Repetition is only the bounded `loop` step, so every flow terminates. A loop body ends
  where its path ends (it returns to the loop) and must not connect to the steps after the loop.
- A non-terminal step without a continuation ends the flow; this is a warning (`implicit-end`) except
  at the end of a loop body.
- Flows may not call each other in a cycle (`flow-call-cycle`): recursion is unbounded.

Variables and expressions (`expression`, `duplicate-variable`, `config`):

- Parameters and variables are visible as bare identifiers. A variable is visible in a step only if its
  defining step is **upstream on some path** (this is an approximation: it does not prove the variable is
  assigned on every path). Loop items are visible only inside the body.
- Names are `lower_snake_case`, unique per flow, and may not repeat a parameter.
- Every expression is type-checked with the expression language (EXPRESSION-SPEC.md): `decision.expr`
  and `retrieve.where` must be boolean; `create`/`change` values must fit the field's type, and `create`
  must supply every required field without a default; call arguments must match the callee's parameters.
  `retrieve.where` sees the entity's fields as bare names, and they shadow variables of the same name.
- A nullable record variable (single `retrieve`) needs `?.` for member access.

`validate_model` reports these with paths like `/modules/0/flows/Key/steps/<id>/config/<field>` or
`…/edges/<id>`, which the editor maps back to boxes and fields. `validate_flow` checks one unsaved
flow in the context of a model for live feedback.

## 4. Versioning, diff and dependencies

A flow is one model element (`element_id`). Editing any step changes the flow's `steps`/`edges` in the
diff (A2); layout changes do not. The dependency graph records `flow-entity` (entities used by steps)
and `flow-call` edges, so find-usages and the linter's unused-entity rule see flows. Per-step diff,
field-level references from step expressions, and flow-specific lint rules (commit inside a loop,
missing error handling) are not implemented.

## 5. Execution (Logic Engine)

`orchivio.logic.FlowRunner` interprets the validated graph; it never runs generated code.

- **One transaction.** A run is a single database transaction. `raise_error`, any failed step, a failed
  hook or a limit rolls back everything the flow wrote, including writes made by flows it called, and the
  audit events of those writes are discarded (events are written only after commit).
- **The caller's security.** Every data step goes through the same data service as the API: roles, row
  constraints, validation, uniqueness, computed fields and entity hooks all apply, as the simulated user.
  A flow cannot do more than the user who runs it (elevated, system-context flows are not implemented).
- **Steps.** `retrieve` compiles its `where` into a parameterised query (flow variables become bind
  parameters; computed fields cannot be filtered on) and applies the user's row constraints; `create` /
  `change` / `delete` call the data service; `change` refreshes the variable with the stored record;
  `loop` fails (rather than truncating) if the list is longer than `max_iterations`; `call_flow` passes
  coerced arguments and gives the callee its own variable scope; `end` finishes the whole flow, including
  from inside a loop body.
- **Limits.** 10 000 steps, 30 seconds, call depth 10, hook nesting depth 3 (configurable `Limits`).
- **Trace.** Every executed step yields a span `{flow, step, type, status, ms, iteration, detail}` with the
  step ids of the model, so the editor can overlay the run and a debugger can be built on it. Single-step
  debugging and breakpoints are not implemented.
- **API.** `POST /applications/{id}/preview/flows/{flow}/run` with `{args}` and the `X-Preview-User` /
  `X-Preview-Roles` headers runs the *approved* flow in the preview. Hook flows and client flows cannot be run
  directly.

## 6. Entity event hooks

`entities[].hooks` maps an event (`before_create`, `after_create`, `before_update`, `after_update`,
`before_delete`, `after_delete`) to a flow key. A hook flow must be a **server flow without parameters**,
serves exactly one entity, and receives two implicit variables: `record` (the new or changed record) and
`previous` (the stored record before an update or delete; it may be null, so use `?.`). `before_*` hooks run
before the write and can veto it with `raise_error`; `after_*` hooks run after the write, in the same
transaction. Hooks that write can trigger further hooks up to a depth of 3. A failing hook makes the
operation fail with `409 hook_failed` and nothing is changed. Hooks run as the same user, so a role that may
not create the records a hook creates cannot trigger it successfully.

## 7. Flow tests

`flows[].tests` holds declarative tests (FR-058):

```yaml
tests:
  - name: fails and changes nothing when no product is low
    args: { min_gap: 5 }
    user: { id: u1, roles: [admin] }            # optional; default: every role the model defines
    fixtures: { Product: [ { name: Fine, sku: F-1, quantity: 50, reorder_level: 5, owner: u1 } ] }
    expect:
      status: failed
      error_contains: reorder level
      visited: [none]
      not_visited: [bump]
      records: [ { entity: Product, where: "reorder_level == 5", count: 1 } ]
```

Each test runs in its own throw-away runtime (fixtures are inserted directly, the flow runs under the test
user's security). Tests are validated statically (arguments, fixture fields, step ids, expressions) and run as
`flow` cases in every generated test run, with a flows coverage figure. A mutation test breaks the engine on
purpose to prove they can fail.

## 8. Not specified yet

Client-side actions (navigation, messages) and a client runtime, `commit` and savepoints, AI steps, calls
to an integration, error-handling branches, scheduled jobs and the task queue, elevated (system-context)
flows, per-step breakpoints.
