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
| T21 | **XSS in the Studio** steals the session or CSRF token | React escapes output and no raw HTML is rendered; `HttpOnly` session cookie; CSRF token only in per-tab `sessionStorage`; nginx serves `Content-Security-Policy` (no unsafe-eval, no unsafe-inline for scripts), `X-Frame-Options: DENY`, `Referrer-Policy`; API also sets `Referrer-Policy` | 🟡 token is script-readable by design; CSP not enforced in dev (no nginx) |
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

| A14 | A model or import points a flow at an internal service or sends data to an attacker's host | The model holds only logical destination names; admins configure the URLs; static and resolve-and-pin checks on every call; allowlist; imports are A3 proposals; new calls in a flow are A3 | ✅ tested |
| A15 | A malicious OpenAPI document (remote refs, cycles, huge schemas, odd names) | Local `$ref` only, depth/size/operation/property limits, names re-derived, nothing fetched | ✅ tested |
| A16 | A hostile or broken API answer corrupts data or leaks through errors | Typed conversion with required-field and type checks, 1 MiB cap, no body text in errors, path values percent-encoded | ✅ tested |

| A17 | Indirect prompt injection: a registered MCP server returns instructions that steer the Builder | Output is data in delimited blocks, text only, size-capped, control characters removed; no output ever triggers further calls; only A0 read-only tools can be enabled; every proposal still passes validation, lint, diff and human approval | ✅ structure tested · 🟡 model resistance not measured |
| A18 | A malicious tool description or schema manipulates the planner or the admin's view | Descriptions are bounded and cleaned, shown to the model only as data, new tools start disabled, schema reduced to simple facts, names validated | ✅ |
| A19 | The Builder is used to reach internal services or leak data through tool arguments | Registered URLs pass the SSRF checks and are pinned; arguments are validated against the tool's schema and limited to 2 000 bytes; refused calls are not sent; only hashes are audited | ✅ |

| A20 | A person acts on a task that is not theirs, or two people complete the same task | Visibility limited to assignee/role holders (others get 404); completion re-reads the stored task, checks the token still waits on exactly that task, and updates the instance by compare-and-set | ✅ tested |
| A21 | A process is used to escalate privilege (start rights, system tasks) | `start_roles` and `run_as_role` are model elements: widening either is A3; system tasks run with only `run_as_role`; delegation only to active users who hold the task's role | ✅ |
| A22 | A process spins forever or floods the worker | Static refusal of cycles without a waiting step, per-advance and per-instance step limits, bounded form and result sizes | ✅ |

| A26 | Forged or replayed ID tokens (alg none, HMAC confusion, wrong key/audience/issuer/nonce, expiry) | Allow-listed asymmetric algorithms, key from the issuer's JWKS, full claim checks, nonce bound to the attempt, 2048-bit RSA minimum | ✅ tested |
| A27 | Login CSRF / session fixation / state replay in the OIDC redirect flow | Single-use server-side state, browser-binding cookie, 10-minute expiry, PKCE | ✅ tested |
| A28 | Account takeover through e-mail claims or username collisions | Identity is (issuer, subject); matching local usernames are never merged; domain allowlists need verified e-mail; local accounts untouched | ✅ tested |
| A29 | Open redirect after sign-in; SSRF through provider-supplied endpoints | `next` limited to site paths; discovery endpoints checked and every fetch pinned | ✅ tested |
| A23 | Forced browsing: a user calls a page, row or action route the UI never showed them | Every page route re-checks role, entity operations, columns and the action's expressions; unauthorized pages and actions answer 404 exactly like missing ones | ✅ tested |
| A24 | A page is used to read or write fields it does not display | Declared columns bound reads, writes, filters and sorting; field-level security applies underneath | ✅ |
| A25 | Any signed-in user runs any server flow | Direct runs need a role in `run_roles` (deny by default); flows run as the caller; exposing a flow is A3 | ✅ tested |

| A30 | Telemetry leaks data (bodies, tokens, record values, URLs) to the collector or its operators | Attributes are an explicit allow-list of short scalars set at call sites; no bodies, headers, queries, SQL or URLs; exceptions by type only; metric attributes restricted per instrument | ✅ tested (secrets searched for in every span) |
| A31 | A caller forces tracing on or injects trace ids | Incoming `traceparent` ignored unless explicitly trusted; head sampling applies to new traces | ✅ tested |

| A32 | A model change bypasses organization policy through another route (AI, import, branch merge, rollback) | Packs are evaluated in the one place all routes plan models, plus the merge path; violations fail like validation errors | ✅ tested (revision, proposal, merge) |
| A33 | Audit export leaks other organizations' events or is used to hide activity | Scoped by organization, admin-only, literal filters, every export audited with its hash, CSV formula-safe | ✅ tested |

| A34 | Telemetry or logs leak data to the collector or to the AI | Attribute allow-list, fixed log messages with allow-listed fields, exception types only, no prompts in AI spans; log analysis sends only those stored lines | ✅ tested |
| A35 | Trace context lets a caller force sampling, or tells a third party about us | Incoming context ignored by default; outbound `traceparent` opt-in and only for webhooks and consumed APIs | ✅ tested |
| A36 | The AI invents evidence in a log analysis | Findings must cite line ids that were sent; others are dropped and counted | ✅ tested |

| A37 | Feedback text steers the AI (prompt injection) or floods the board | Feedback is wrapped as untrusted data; AI output only creates `proposed` stories and cites validated ids; per-user rate limit | ✅ tested |
| A38 | A screenshot upload carries active content | Only PNG/JPEG by magic bytes, 512 KiB, served with the stored type, `nosniff`, CSP `default-src 'none'; sandbox`, editors only | ✅ tested |
| A39 | Trace links point at another project's data | Every link target is resolved inside the same project (stories, revisions, elements of its applications) | ✅ tested |
| A40 | Requirement text executes in a spreadsheet | Cells starting with `= + - @` are prefixed in CSV export | ✅ tested |

| A41 | A document steers the requirement extraction or floods the project | Document is wrapped as untrusted data; output is draft requirements only, titles de-duplicated, at most 20 per call | ✅ tested |
| A42 | The widget captures more than the person intends | Screen capture goes through the browser's share prompt and takes one frame on a click; element picking swallows exactly one click; nothing is sent until Send | ✅ tested (picking, validation); capture needs a manual check |

| A43 | The remote URL is used for SSRF or local file access (internal hosts, `file://`, `ext::`, ssh) | https only, operator allow-list, public-address check, `GIT_ALLOW_PROTOCOL=https`, no redirects; disabled unless hosts are configured | ✅ tested (DNS pinning not possible: residual risk documented) |
| A44 | A malicious repository attacks the server (hooks, filters, symlinks, submodules, option injection) | No checkout, plumbing only, hooks off, clean config, `--` before untrusted arguments, validated branch/path names, regular files only | ✅ tested |
| A45 | The Git token leaks (logs, errors, audit, argv) | Reference only; passed as an environment header; scrubbed; git output never returned | ✅ tested |
| A46 | Git becomes a way around approval | Pull only creates a proposal; push only writes a non-base branch | ✅ tested |
| A47 | Orchivio overwrites people's files in the repository | Only canonical-shaped files under the path are replaced; the base branch is never written | ✅ tested |

| A48 | A malicious or tampered package alters an application | Signature over a manifest that pins the model hash; trusted keys only; re-verified from storage on every install; the result is a proposal with the normal validation, policy packs and approval | ✅ tested |
| A49 | A module smuggles extra permissions or roles | Roles and access rules it brings appear in the diff as security changes (A3); policy packs apply; nothing is applied without approval | ✅ tested |
| A50 | A published version is silently replaced | Versions are immutable (unique name+version); yanking hides, never deletes | ✅ tested |
| A51 | Private packages leak to other organizations | `org` visibility readable only by members; others get 404 | ✅ tested |
| A52 | An upgrade overwrites local work or another module | Upgrades only replace an unmodified module that came from the same package; otherwise refused unless explicitly told | ✅ tested |

| A53 | A remote registry feeds malicious packages or abuses the connection (SSRF, huge downloads, error-text injection) | Pinned transport with the webhook destination policy, size limit, only signed packages from trusted keys are stored, the remote's text is never returned | ✅ tested |
| A54 | The remote token leaks | Reference only (`env:`/`file:`), sent only as a bearer header to the configured server, never stored or returned | ✅ tested |
| A55 | A dependency pulls in code the installer did not expect | Dependencies are resolved into the same proposal and listed as steps; each is verified and policy-checked like the module itself; conflicts and cycles refuse | ✅ tested |
| A56 | A rename hides a changed meaning | The result is validated as part of the proposal; a test requires the renamed module to pass the same generated tests as the original | ✅ tested |
| A57 | Uninstall breaks or silently deletes things | A proposal only; refused while other modules require it or something else uses it; roles stay; data tables are not dropped by a revision | ✅ tested |

## 4. Assumptions

- PostgreSQL, the host OS and the TLS terminator are operated securely.
- Containers are not treated as the only isolation boundary for workers (DESIGN.md §12.2).
- Self-hosters supply their own secret management until the secret-store interface exists.

## 5. Open items

Choose worker isolation technology; decide on PostgreSQL RLS as defence in depth
([ADR-0003](adr/0003-app-security-enforcement.md)); define retention periods; add general rate
limiting, a shared throttle store (multi-replica), and MFA/SSO.
