# Orchivio Studio (web)

React + TypeScript front end. Today it contains the **flow builder**; the rest of the Studio
(dashboard, model explorer, page builder, review screens) is planned.

## Run

```bash
npm install
npm run dev          # http://localhost:5173, proxies /api to http://127.0.0.1:8000
npm test             # vitest (jsdom)
npm run typecheck
npm run build        # type-checks, then bundles to dist/
```

Start the API separately (`uvicorn orchivio.api.main:app`, see the root README) and register a user
there. "Open the demo flow" works without a server, without live validation.

## Design

- **The flow is the source of truth.** `src/flow/ops.ts` holds pure, immutable edit functions; React
  Flow nodes and edges are derived from the flow (`convert.ts`) and React Flow's own state (measuring,
  selection) is merged back in. Undo/redo is a stack of immutable snapshots (`history.ts`); typing in
  one field collapses into a single undo step and a drag is one step.
- **One set of rules.** The editor refuses the same connections the server rejects (self-loops, wrong
  outputs, second connection from an output, cycles), using `ops.connect`, both for dragging
  (`isValidConnection`) and for the form controls.
- **The server is the authority.** Everything else is checked by `POST /models/validate-flow`
  (debounced, 400 ms) with the same validator that guards saving. Issues are attached to boxes, fields
  and the Problems list by their path (`issues.ts`).
- **No drift.** `src/flow/contract.json` (step types, outputs, limits, id patterns) and the sample
  flow/model are generated from the backend by `scripts/export_web_contract.py`; a backend test fails
  when they are stale.
- **Running.** The Run panel posts to the preview run endpoint and overlays the returned trace on the canvas (how often each
  step ran, where it failed). Runs use the *approved* model, so Run is disabled while there are unsaved edits.
- **Saving is a proposal.** The edited flow is merged into the revision's model and posted to
  `/applications/{id}/proposals`; nothing changes until it is approved (docs/AI-BUILDER.md).
- **Session.** Cookie session; the CSRF token returned at login is kept in `sessionStorage` (per tab).
  A new tab needs a fresh login.

## Accessibility

Every operation has a keyboard path: palette buttons add steps; the panel lets you choose each output's
next step from a list (so no mouse drag is needed to connect); React Flow provides focusable nodes and
edges, arrow-key moves and Delete; Ctrl+Z / Ctrl+Shift+Z undo and redo outside text fields; actions are
announced in an `aria-live` region; problems are listed as text, not only coloured; colours follow the
OS light/dark setting and respect reduced motion. Honest limits: no screen-reader testing was done, the
canvas itself is inherently visual, and the layout is a simple layered one.

## Tests

`flow.test.ts` (rules, history, layout, conversion), `FlowEditor.test.tsx` (driving the editor like a
user: palette, undo, keyboard connect, cycle refusal, validation display, save gating, typing collapse),
`client.test.ts` (CSRF, errors), `App.test.tsx`. jsdom cannot drag or measure, so mouse dragging and
canvas rendering are **not** covered by automated tests; they were not exercised in a real browser
either. The TypeScript client was verified once against a live server (login with CSRF, flow
validation, proposal creation) outside the suite.

## Limitations

- Mouse drag and drop, zoom/pan, the minimap and visual layout quality are untested in a browser.
- Expression inputs are plain text (no completion, no inline type hints); variables are not suggested
  in expressions.
- One flow per editor; no copy/paste of steps, no import, no flow-level search.
- The bundle is about 138 kB gzipped; no code splitting yet.
- A flow must exist in a module of an existing revision; creating the first module is not supported.
