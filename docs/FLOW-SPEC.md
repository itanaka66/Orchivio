# Flow Specification (Server and Client Flows)

Implementation: schema `src/orchivio/model/schema.py` (`FlowDef`), validator
`src/orchivio/model/flows.py`, editor `apps/web/src/flow/`. Requirements: DESIGN.md §4.6 (FR-052,
FR-053), §8.6.

> **Status.** Flows can be modelled, validated, versioned, diffed and edited visually. **They are not
> executed yet**: the Logic Engine that interprets them is a later phase (DESIGN.md Phase 8). Today a
> flow is a checked specification, not behaviour.

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

## 5. Not specified yet

Client-side actions (navigation, messages), `commit`, AI steps, call to an integration, transactions and
the error-handling model, event hooks binding flows to entities, scheduled jobs, flow unit tests, the
debugger. Entity `hooks` still produce a warning.
