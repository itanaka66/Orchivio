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
| T1 | **Prompt injection / hallucinated output** alters the model or escalates privilege | AI output is data: strict schema (`extra=forbid`), validate/lint/repair loop, diff with risk levels, role-gated approval (A3 needs an admin and acknowledgement), delimited untrusted input, output never executed | ✅ gate tested with a hostile model output; 🟡 injection resistance of real models not measured |
| T2 | **Unsafe execution of generated logic** | No arbitrary code in runtime; expressions are interpreted, pure and bounded; limits on depth, nodes, steps, size | ✅ for expressions; ⬜ workers/sandbox |
| T3 | **Injection via expressions/row constraints** | Values only as bind parameters; identifiers validated and quoted; non-compilable ⇒ deny; interpreter/SQL equivalence tests | ✅ SQLite · 🟡 PostgreSQL |
| T4 | **Row-level rule bypass** through a new query path | One data service applies the predicate to reads, counts, updates and deletes and re-checks the resulting record; non-compilable constraint means deny; generated access/row-constraint tests; mutation tests prove they fail on a broken runtime | ✅ preview runtime (SQLite); ⬜ other query paths (exports, aggregates, OData, MCP), PostgreSQL |
| T5 | **Privilege escalation via permission expansion** | Diff flags grants/expansions/anonymous as A3; consistency checker | ✅ detection · ⬜ approval enforcement |
| T6 | **Cross-tenant access** | Every resource resolved to its organization and authorized through membership; identical 404 for missing and hidden; cross-org tests | ✅ for the platform API (tests per resource); ⬜ application plane |
| T7 | **Audit tampering** | Append-only table, no FKs, DB triggers, separate retention role | 🟡 migration written, never run on PostgreSQL |
| T8 | **Revision tampering** | Immutable revisions, `model_hash`, trigger | 🟡 as above |
| T9 | **Secret leakage** (keys in model, logs or LLM context) | Secrets only by reference; hashed tokens/passwords; redact before LLM calls; no stack traces in errors | 🟡 errors and hashing design done; redaction ⬜ |
| T10 | **Path traversal in artifact storage** | Validated keys + resolved-path check | ✅ tested |
| T11 | **Resource exhaustion (API)** | Body-size limit, bounded pagination, expression limits, login throttle, password length cap | 🟡 `Content-Length` only; throttle is per process; general rate limiting ⬜ |
| T12 | **Unauthenticated access / credential attacks** | Sessions (HttpOnly, SameSite=Lax, Secure in prod, server-side revocation, hashed), CSRF token, scrypt, generic login errors with constant-time dummy verification, throttling, scoped expiring API tokens that cannot mint tokens | ✅ tested; 🟡 SSO/MFA ⬜, throttle not shared across replicas |
| T17 | **CSRF** | `X-CSRF-Token` on unsafe cookie-authenticated requests; SameSite=Lax | ✅ tested |
| T18 | **User enumeration** | Same response and timing class for unknown/wrong/disabled logins | 🟡 registration reveals `email_taken`; member invite reveals `user_not_found` to admins |
| T13 | **SSRF / data exfiltration through integrations and webhooks** | Per-environment allowlists, private-network blocking | ⬜ (Phase 9) |
| T14 | **Malicious MCP tool use / injection through tool output** | Scoped tokens, proposal-only writes, tool output treated as data | ⬜ (Phase 9) |
| T15 | **Supply-chain compromise** | Dependency and secret scanning, SBOM in CI; pinned runtime versions | ⬜ |
| T16 | **Cost exhaustion (LLM)** | Per-project monthly budget, usage recorded per job, bounded retries and repairs, input size limit | 🟡 depends on adapter-reported cost |
| T21 | **XSS in the Studio** steals the session or CSRF token | React escapes output and no raw HTML is rendered; `HttpOnly` session cookie; CSRF token only in per-tab `sessionStorage` | 🟡 no Content-Security-Policy header yet; token is script-readable by design |
| T22 | **Malicious flow content** (labels, expressions) injected into the editor | Rendered as text only; all content validated server-side; flows are not executed | ✅ today; revisit when the Logic Engine exists |
| T23 | **Runaway or abusive flows** (long loops, deep calls, hook recursion, privilege escalation) | Graph must be acyclic; loops need a maximum; step, time, call-depth and hook-depth limits; one transaction; runs as the caller, never elevated | ✅ tested; 🟡 limits are per run, not per user/time, and runs are synchronous in the request |
| T24 | **Backup theft** (hashes, models, audit log) or backup tampering | Backups are written `0600`, documented as confidential, verified by per-file hashes before restore, restore only into an empty database at the same revision, sessions excluded by default | 🟡 encryption at rest is the operator's job; the archive is not signed |
| T25 | **Metrics and readiness as information leaks** | `/metrics` disabled unless a token is set and compared in constant time; route templates not raw paths; `/readyz` returns only ok/behind/missing/unavailable | ✅ tested |
| T19 | **Data exfiltration to an external LLM** | Providers flagged `external` are refused until the project opts in; minimal context; input stored only as a hash | ✅ tested with a mock flagged external |
| T20 | **Preview used to reach real data / Studio role abuse in preview** | Separate preview database; roles come from the model, never from Studio roles; unknown roles rejected | ✅ tested |

### Application plane (end-user authentication, field access)

| # | Threat | Control | Status |
|---|---|---|---|
| A1 | A Studio credential is used to reach end-user data, or an end user's session is used in the Studio | Different cookies, token prefixes and endpoints; `/apps/...` rejects Studio cookies and `orv_` tokens; Studio endpoints do not know application sessions | ✅ tested both ways |
| A2 | Session theft from one application is replayed against another | Cookie path-scoped per application and the session row records its application | ✅ tested |
| A3 | Credential stuffing / user enumeration at app login | Per-user and per-IP throttle (per process), uniform error and constant-time dummy verification | ✅ · 🟡 throttle not shared across replicas |
| A4 | Stale privileges after a role change or disablement | Roles read on every request; disabling or resetting ends sessions | ✅ tested |
| A5 | Anonymous over-exposure | Opt-in flag classified A3; only the `anonymous` role applies; consistency checker warning | ✅ |
| A6 | A hidden field is inferred through filters, sorting, error messages, flows or computed fields | Hidden fields are removed server-side; filter/sort/write errors for hidden fields equal those for unknown fields; flows run with the caller's field access; consistency checker flags computed fields that leak | ✅ tested · 🟡 entity hooks see whole records by design; future exports, aggregates and search must reuse the same field filter |

| A7 | SSRF through webhook destinations (internal services, cloud metadata, DNS rebinding, redirects) | Static URL checks plus resolve-and-pin at send time; every resolved address must be public; no redirects; optional allowlist; refused destinations fail permanently | ✅ unit and socket tests · 🟡 no egress firewall: network-level controls remain the operator's job |
| A8 | Event payloads leak data to a receiver | `fields` allow-list, default ids only; URL kept out of the audit log; signing secret only by reference | ✅ |
| A9 | Forged or replayed webhook requests at the receiver | HMAC-SHA256 over timestamp + body, constant-time verification helper, replay window | ✅ |

| A10 | CSV import used to bypass validation, row/field access or hooks | Rows are created through `AppData.create` as the caller; unwritable columns are refused; size, row and column limits | ✅ tested |
| A11 | Formula injection and bulk exfiltration through CSV export | Separate `export` grant (A3 to widen), field and row filters, row cap, audit, quote-prefixing of formula cells | ✅ tested |

| A12 | Forged, replayed or probing requests to the public inbound endpoint | HMAC-SHA256 with constant-time compare and 5-minute window; uniform 401 for every authentication failure (including an unreadable secret); 404 for unknown/paused; dedup on event id; body-size and rate limits; mapping checked only after authentication and values never echoed | ✅ tested · 🟡 rate limit is per process; unauthenticated requests still cost a database lookup |
| A13 | A webhook sender escalates through the flow it triggers | The flow runs as the fixed `run_as_role` chosen by an admin (not by the sender); parameters are mapped and type-checked; application security applies to every step | ✅ |

## 4. Assumptions

- PostgreSQL, the host OS and the TLS terminator are operated securely.
- Containers are not treated as the only isolation boundary for workers (DESIGN.md §12.2).
- Self-hosters supply their own secret management until the secret-store interface exists.

## 5. Open items

Choose worker isolation technology; decide on PostgreSQL RLS as defence in depth
([ADR-0003](adr/0003-app-security-enforcement.md)); define retention periods; add general rate
limiting, a shared throttle store (multi-replica), and MFA/SSO.
