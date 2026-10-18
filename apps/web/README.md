# Orchivio Studio (web)

React + TypeScript front end. It contains the **flow builder**, the **story board** and **preview** (docs/AGILE.md), and the
**administration screens** below. A model explorer, page builder and proposal review screens are still planned (proposals are reviewed
through the API).

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

## Administration screens (`src/admin`)

"Open administration" (after choosing an organization, project and application) opens a workspace with a side navigation. What a person
sees depends on their organization role; the server enforces the same rules again on every call, and its messages are shown as they are.

| Screen | Role | What it does (API) |
|---|---|---|
| Branches | editor | list, create from a revision, abandon (two-step), merge into main with preview, conflict resolution choices and risk acknowledgement |
| Environments & deployment | editor (admin for environments/constants) | environments and gates, constants (secrets only as references), build/verify/download packages, promote with destructive-step acknowledgement, approve/reject |
| Jobs & schedules | editor | schedules with next/last run, queue a server flow as a role, filter, retry, cancel |
| Processes & tasks | editor | instances (filter, details with tasks/events/variables, cancel, retry failed step), task monitor with overdue/escalated, start a test instance |
| Logs | editor | filter, search, page older lines; AI analysis whose findings highlight the cited lines |
| Application users | admin | list, create, edit roles, enable/disable, reset password, delete (two-step) |
| Single sign-on (OIDC) | admin | configure provider, role mapping, domains, auto-provisioning warning, connection test showing the redirect URI |
| Webhooks | admin | create, pause/resume with the reason, test, delivery log with payloads and resend |
| Destinations & API import | admin | destinations and credentials (missing-credential warning), OpenAPI import as a proposal |
| Module registry | editor (admin publishes) | installed modules and updates, search, install with version range / module key and the resolved steps, upgrade, remove, templates, publish, withdraw, other servers' registries |
| Git sync | admin | connect, push (optionally a pull request), pull as a proposal |
| MCP servers | admin (organization) | register, discover tools, enable read-only tools |
| Governance | admin (organization) | policy packs (JSON rules), compliance, inventory with risk flags, AI usage, audit export links |

Design notes: one small kit (`admin/kit.tsx`: `useApi`, `useAction`, two-step `Danger` buttons, pager); screens talk to the API through
`client.request` and are tested against a route-table fake (`admin/mock.ts`). Secrets are never typed in: fields take references such as
`env:NAME`. The browser view stays mounted (hidden) while a screen is open, so going back keeps the chosen organization, project and application.
