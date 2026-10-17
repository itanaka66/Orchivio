# ADR-0007: Flow editor built on React Flow

Status: Provisional · Date: 2026-10-17 · Resolves DESIGN.md §22 item 14 (provisionally)

## Context
The Flow Builder (and later the Process Builder) needs a node-and-edge editor in the React/TypeScript
Studio. Candidates: React Flow (`@xyflow/react`), Rete.js, LiteGraph.js, Drawflow; Flowise is an
application, not a library. Requirements: TypeScript and React fit, custom typed nodes, a flow model owned
by the backend (no second execution engine in the browser), keyboard accessibility, a debugger overlay
later, and a licence compatible with GPL-3.0-or-later.

## Decision
Use **React Flow** (MIT), version 12. The editor is a *view* of the model's `FlowDef`: the flow is the
single source of truth, React Flow holds only presentation state, and the backend validator is the
authority. Layout is a small in-house layered algorithm until an engine such as elkjs is justified.

## Why
- It is React/TypeScript native, and custom node components made typed step boxes with issue badges and
  per-step output handles straightforward.
- It has no execution semantics, so it cannot compete with the Logic Engine (DESIGN.md §6.4), unlike
  Rete.js's built-in engine.
- Canvas-based (LiteGraph) and unmaintained-looking (Drawflow) options are weaker on accessibility,
  TypeScript and integration.

## What the spike showed
- Controlled mode works well when edits are pure functions over an immutable flow and nodes are
  re-derived with React Flow's measurement state carried over.
- **Keyboard-only operation needs help:** React Flow has no keyboard way to create an edge, so choosing
  "what happens next" is a form control in the inspector that uses the same connection rules.
- Testing in jsdom needs stubs (`ResizeObserver`, `DOMMatrixReadOnly`), unmeasured nodes are
  `visibility:hidden` (so role+name queries fail; use label queries), and `user-event`'s mouse sequence
  trips d3-drag (use plain click events). Real-browser tests (Playwright) are still needed for drag,
  zoom and layout.
- Bundle impact: the whole app is about 138 kB gzipped.

## Consequences
We depend on `@xyflow/react` and its d3 packages (check licences in the SBOM). Anything React Flow's paid
"Pro" examples offer is out of scope. Revisit if the Process Builder needs swimlanes/BPMN notation, or if
accessibility testing with assistive technology shows the canvas model is inadequate.
