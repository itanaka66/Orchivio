# AI Builder and Proposals

Implementation: `src/orchivio/ai/`, `src/orchivio/llm/`, `src/orchivio/db/proposals.py`,
`src/orchivio/api/routes/{ai,proposals}.py`. Requirements: DESIGN.md §4.2, §7, §11.2.

## 1. Principle

The AI proposes; it never changes anything. Its output is **untrusted data** that must pass
schema validation, static validation and blocking lint rules, is shown as a diff with risk
levels, and becomes a revision only when a person with enough privilege approves it.
There is no privileged path: human edits, the AI Builder and (later) MCP agents all create
*proposals*.

## 2. Flow

```
requirements ──▶ analyze (optional: summary, roles, entities, ≤5 questions)
      │
      ▼
propose_model ─▶ LLM ─▶ JSON ─▶ AppModel schema (extra=forbid) ─┐ invalid? re-prompt with the errors
      ▲                                                          ▼ (≤3 attempts)
      │                              assign element ids ─▶ validate_model ─▶ lint (blocking errors)
      │                                                          │ problems? feed them back (≤2 repairs)
      └──────────────────────────────────────────────────────────┘
                                   ▼
            Proposal (pending): model, diff, approval level, lint findings   ← nothing applied yet
                                   ▼
        POST /proposals/{id}/approval  ──▶ new immutable revision (or rejected / superseded)
```

If the provider cannot produce an acceptable model, the job is recorded as failed and the
caller gets `422 ai_output_invalid` with the remaining issues. No proposal is stored.

## 3. Approval

| Level | Raised by | Who may approve |
|---|---|---|
| A2 | any applied model change | editor or above |
| A3 | permission expansion, new grants, anonymous access, removal of entities/fields/enums, type changes | admin or above, **and** `acknowledge_risks: true` |
| A4 | reserved (production deployment, secrets) | owner |

A proposal records the revision it was based on. If the model has moved on by approval time it
becomes `superseded` (`409 stale_proposal`); there is no automatic merge, so propose again.
Rejected, applied and superseded proposals are final. Every transition is audited.

## 4. Endpoints

| Endpoint | Purpose | Min. role |
|---|---|---|
| `POST /applications/{id}/ai/analyze` | Requirements summary and open questions | editor |
| `POST /applications/{id}/ai/propose` | Model proposal from requirements (uses the current model as context; `use_tools` adds MCP background, §6) | editor |
| `/organizations/{id}/mcp-servers[/{name}[/discover]]`, `PUT .../tools/{tool}` | Register external MCP servers, discover and enable read-only tools | org admin |
| `POST /applications/{id}/ai/explain` | Explain one element, citing element ids (read-only, A0) | viewer |
| `POST /applications/{id}/proposals` | Submit a human-made model as a proposal | editor |
| `GET /applications/{id}/proposals`, `GET /proposals/{id}` | Review (diff, lint, warnings, decision) | viewer |
| `POST /proposals/{id}/approval` | `approve` (applies) or `reject` | editor / admin per level |
| `POST /test-runs/{id}/ai/repair` | Repair proposal from a failed generated-test run | editor |

## 5. Controls

- **Untrusted input.** User text is placed in delimited blocks (`<requirements>`,
  `<current_model>`, `<element_context>`); the delimiter cannot be closed from inside; the system
  prompt says the blocks are data. This lowers injection risk but is *not* the safety mechanism.
- **The safety mechanism is the gate.** Even a model that obeyed a malicious instruction produces
  a proposal that is diffed, classified (a test covers a model that tries to enable anonymous
  access) and needs an admin's explicit acknowledgement.
- **Security-aware generation.** The prompt demands roles and access rules; a model without
  access rules fails validation (`no-access-rule`) and is sent back for repair.
- **Strict output.** Unknown properties are rejected, so hallucinated keys are retried instead of
  being silently dropped. Output is never executed.
- **Minimal context.** Only the requirements text and the current model are sent; `explain` sends
  just the target element and its direct dependencies/dependents. Secrets are never part of the
  model. Job records keep a SHA-256 of the input, not the text.
- **Consent for external providers.** A provider with `external = True` (hosted API) is refused
  with `403 external_ai_disabled` until a project admin sets `settings.ai_allow_external: true`.
- **Budget.** `settings.ai_monthly_budget_usd` stops further calls (`429 ai_budget_exceeded`) once
  recorded cost reaches it. Cost is whatever the provider adapter reports; the Anthropic adapter
  reports tokens and leaves cost at 0 because prices change and are deployment specific, so set
  budgets only with a provider that reports cost.
- **Citations.** Explanations must cite element ids that exist in the supplied context; unknown
  ids are dropped and an explanation with no valid citation is retried, then rejected. The
  *text* of an explanation is still AI-generated and may be wrong.
- **Provider errors.** The client sees a generic `502 ai_provider_error`; details stay in
  `ai_jobs.error`.

## 6. External tools through MCP (FR-087)

`POST /applications/{id}/ai/propose` with `use_tools: true` lets the Builder consult read-only tools of external MCP
servers (documentation, tickets, design sources) before it proposes a model. Off by default and chosen per request.

* **Registry** (`/organizations/{id}/mcp-servers`, organization admin, A3): register a server (URL checked like every
  other outbound destination: https, public addresses only, optional allowlist, pinned at connection time; optional
  bearer credential by `env:`/`file:` reference). `POST .../{name}/discover` lists its tools. Discovery output is
  normalised: tool names must be plain identifiers, descriptions are cut to 500 characters with control characters
  removed, input schemas are reduced to property names, simple types, enums and `required`. **New tools start
  disabled**; a tool that disappears loses its approval; changing the URL clears everything.
* **Per-tool policy** (`PUT .../tools/{tool}`): `{enabled, level}`. Only `level: A0` (read-only background) can be
  enabled. Anything stronger needs a person to confirm each call and the Builder has no such step, so it is refused
  (`approval_not_supported`) rather than silently allowed.
* **One planning round.** The model is shown the catalogue of enabled tools and the requirements and may *request* up to
  `ORCHIVIO_MCP_CLIENT_MAX_CALLS` (3) calls. Every request is re-checked by the platform: enabled and still advertised,
  arguments valid for the schema (types, enums, required, no unknown keys, at most 2 000 bytes), not a duplicate.
  Refused calls are never sent. Tool output cannot trigger further calls.
* **Tool output is untrusted data.** Only `text` content is kept, control characters are stripped, the size is capped at
  8 000 characters, and it is placed in `<external_context source="server/tool">` blocks whose closing tags are
  defused; the system prompt (`propose_model_with_context@1`) says never to follow instructions found there. The tool
  catalogue given to the planner contains no tool output. Server-supplied error text is never put in messages or logs.
* **Audit.** Every call (including refused ones) writes `mcp.client_call` with server, tool, a hash of the arguments,
  success, error code and output size - no arguments and no output - and the job's `usage.tool_calls` records the same.
  The planning call counts in the job's token and cost totals and in the project's monthly AI budget.
* **Failure is not fatal.** An unusable plan, a missing credential, an unreachable or failing server only mean the
  proposal is made without that background; the proposal still goes through validation, lint, diff and approval.
* The defence is the same as for all Builder output: a person approves before anything changes. Tool output can at worst
  mislead a proposal, not apply one. Injection resistance of real models is not measured.

## 7. Providers

`ORCHIVIO_LLM_PROVIDER` is `none` (default: AI endpoints answer `503 ai_not_configured`) or
`anthropic` (needs `ORCHIVIO_ANTHROPIC_API_KEY`, optional `ORCHIVIO_LLM_MODEL`; install the
`anthropic` extra for `httpx`). The adapter speaks the Messages API over HTTPS and appends the
JSON Schema to the system prompt; it is covered by mocked-transport tests **only** and has not
been run against the live service here. A provider is any object implementing
`orchivio.llm.LLMProvider` (see INTERFACES.md); local models fit the same interface.

## 8. Known limitations

- Calls run inside the request (up to the provider timeout). Moving them to the job queue and
  streaming progress to the Studio is planned.
- The model sees the whole current model for `propose`; very large models need chunking.
- No conversation memory: each call is independent (tables for conversations exist).
- No evaluation dataset yet (schema validity, requirement coverage, rework rate; DESIGN.md §13.2).
- Prompt-injection resistance has not been red-teamed; treat the approval gate as the defence.
