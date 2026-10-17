# Threat Model (initial)

Scope: the Orchivio platform and the applications it generates. Method: list assets and trust
boundaries, then threats per boundary with the control and its current status. Review at the
start of every phase and before each release (DESIGN.md §12, Phase 6).

## 1. Assets

Customer data in generated apps · application models and history · Studio credentials and API
tokens · LLM provider keys and secrets · audit logs · build/test workers and their hosts.

## 2. Trust boundaries

1. Browser / API client ↔ API (untrusted input)
2. Platform plane ↔ application plane (separate identities and audit streams)
3. Organization ↔ organization (tenant boundary)
4. Orchivio ↔ LLM provider (data leaves the system)
5. API process ↔ build/test workers (generated code never runs in the API process)
6. Orchivio ↔ external agents via MCP, and ↔ consumed REST services (both untrusted)

## 3. Threats and controls

Status: ✅ implemented and tested · 🟡 partly / not verified end-to-end · ⬜ planned.

| # | Threat | Control | Status |
|---|---|---|---|
| T1 | **Prompt injection / hallucinated output** alters the model or escalates privilege | AI output is data: strict schema (`extra=forbid`), `generate_validated`, static validation, diff with risk levels, approval before any revision | 🟡 validation, diff and levels done; no orchestrator or approval workflow yet |
| T2 | **Unsafe execution of generated logic** | No arbitrary code in runtime; expressions are interpreted, pure and bounded; limits on depth, nodes, steps, size | ✅ for expressions; ⬜ workers/sandbox |
| T3 | **Injection via expressions/row constraints** | Values only as bind parameters; identifiers validated and quoted; non-compilable ⇒ deny; interpreter/SQL equivalence tests | ✅ SQLite · 🟡 PostgreSQL |
| T4 | **Row-level rule bypass** through a new query path | Single data-access layer must apply `row_predicate` everywhere; generated negative tests; linter | ⬜ data layer, tests, linter not built |
| T5 | **Privilege escalation via permission expansion** | Diff flags grants/expansions/anonymous as A3; consistency checker | ✅ detection · ⬜ approval enforcement |
| T6 | **Cross-tenant access** | Every query scoped through membership; ids never trusted; cross-org tests | ⬜ (schema supports it; no endpoints yet) |
| T7 | **Audit tampering** | Append-only table, no FKs, DB triggers, separate retention role | 🟡 migration written, never run on PostgreSQL |
| T8 | **Revision tampering** | Immutable revisions, `model_hash`, trigger | 🟡 as above |
| T9 | **Secret leakage** (keys in model, logs or LLM context) | Secrets only by reference; hashed tokens/passwords; redact before LLM calls; no stack traces in errors | 🟡 errors and hashing design done; redaction ⬜ |
| T10 | **Path traversal in artifact storage** | Validated keys + resolved-path check | ✅ tested |
| T11 | **Resource exhaustion (API)** | Body-size limit, bounded pagination, expression limits | 🟡 `Content-Length` only; rate limiting ⬜ |
| T12 | **Unauthenticated access** | Bearer token required in prod; startup refuses prod without it | 🟡 bootstrap token only; real auth ⬜ |
| T13 | **SSRF / data exfiltration through integrations and webhooks** | Per-environment allowlists, private-network blocking | ⬜ (Phase 9) |
| T14 | **Malicious MCP tool use / injection through tool output** | Scoped tokens, proposal-only writes, tool output treated as data | ⬜ (Phase 9) |
| T15 | **Supply-chain compromise** | Dependency and secret scanning, SBOM in CI; pinned runtime versions | ⬜ |
| T16 | **Cost exhaustion (LLM)** | Per-project budgets, usage recorded per job | ⬜ (usage fields exist) |

## 4. Assumptions

- PostgreSQL, the host OS and the TLS terminator are operated securely.
- Containers are not treated as the only isolation boundary for workers (DESIGN.md §12.2).
- Self-hosters supply their own secret management until the secret-store interface exists.

## 5. Open items

Choose worker isolation technology; decide on PostgreSQL RLS as defence in depth
([ADR-0003](adr/0003-app-security-enforcement.md)); define retention periods; add rate limiting
and CSRF protection with the session implementation.
