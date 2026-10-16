# Orchivio --- AI-Native Application Engineering Low/No-Code Platform

## Requirements, Architecture, Detailed Design Guidelines, and Development Roadmap

-   **Document ID:** ORC-PLAN-001
-   **Version:** 1.1
-   **Date:** 2026-10-17
-   **Status:** Revised baseline --- v1.0 plus Mendix-benchmark
    enhancements; proposed design before implementation
-   **Product:** **Orchivio**
-   **Category:** **AI-Native Application Engineering Low/No-Code
    Platform**
-   **Tagline (proposed):** **Describe It. Build It. Ship It.**

> This document defines the initial product requirements, architecture,
> core data model, API direction, security principles, quality criteria,
> and development phases for Orchivio. Features described here are
> planned targets, not claims of existing implementation.

## Revision History

| Version | Date | Summary |
|---|---|---|
| 1.0 | 2026-10-17 | Initial baseline |
| 1.1 | 2026-10-17 | Benchmarked against Mendix (public information as of Mendix 11.15, September 2026). Added logic/expression layer, two-plane application security model, UI composition and design system, integration and agent interoperability (MCP), branching/merge and environment delivery, human workflows, AI capabilities inside generated apps, model linter, and governance. MVP extended from 20 to 22 weeks; post-MVP roadmap to Release 1.0 (week 52) added. |

### Key Changes in v1.1

1.  **New Section 2.4 and Appendix A:** Mendix gap analysis and Orchivio's
    response, including where Orchivio intentionally does *not* follow Mendix.
2.  **New functional requirement groups 4.3.1 and 4.6--4.13** (FR-028/029,
    FR-050--FR-135).
3.  **Architecture:** Expression Engine, Logic/Process Engines, Integration
    Gateway, Studio MCP Server, Model Linter, Delivery Manager, and Telemetry
    added (Sections 6.1, 6.4).
4.  **Model specification:** modules, enumerations, computed fields,
    row-level access rules, flows, processes, and branch-ready revisions
    (Sections 8.2--8.9).
5.  **Roadmap:** MVP Phases 0--7 rebalanced to 22 weeks; Phases 8--12 added
    for Releases 0.2--1.0 (Section 15). Backlog, risks, documentation plan,
    and open decisions updated.

> **Benchmark note:** Mendix is a trademark of Siemens. Orchivio is not
> affiliated with Mendix or Siemens. Comparisons are based on publicly
> available documentation and release notes and must be re-verified before
> any external publication. Orchivio uses its own original terminology,
> designs, and implementation; no Mendix code, assets, documentation text,
> or proprietary formats are reused.

------------------------------------------------------------------------

# 1. Executive Summary

Orchivio is an AI-native application engineering platform that combines
traditional low-code/no-code development with AI agents capable of
assisting with application design, generation, validation, and
continuous improvement.

Orchivio is not merely a prompt-to-code generator. It maintains an
application as a machine-readable model and uses that model as the
consistent source for UI, data structures, APIs, business logic, tests,
documentation, and deployment definitions.

## 1.1 Core Value Proposition

1.  **Design applications from natural language:** Convert user requests
    into requirements, screens, data models, permissions, and workflows.
2.  **Continuous AI-assisted engineering:** Support planning,
    implementation, testing, failure analysis, and proposed
    repairs---not just initial generation.
3.  **Model as the source of truth:** Generate and maintain UI, database
    structures, APIs, logic, and tests from a unified application model.
4.  **Human-governed changes:** Show diffs, rationale, and impact;
    require approval for consequential changes.
5.  **Avoid vendor lock-in:** Prioritize self-hosting, standard
    technologies, and exportable models and code.
6.  **Support the full lifecycle:** Integrate testing, security checks,
    builds, deployments, and operational observability.

## 1.2 Product Principles

-   Never treat AI output as inherently correct. Validate schemas,
    inspect policies, run tests, and require review where appropriate.
-   Track the source model, generated artifacts, changes, and agent
    execution history.
-   Never execute arbitrary LLM output directly as application code at
    runtime.
-   Keep LLM providers replaceable and support both hosted and local
    models.
-   Keep the MVP focused: complete the workflow of design → generation →
    preview → testing for CRUD business applications.
-   As an open-source project, provide reproducible builds, clear
    licensing, and a vulnerability-handling process.

------------------------------------------------------------------------

# 2. Vision, Users, and Differentiation

## 2.1 Vision

Build an open and extensible application development platform where
users describe requirements in natural language, AI assists with design,
implementation, and validation, and humans retain control over quality
and decisions.

## 2.2 Target Users

  -----------------------------------------------------------------------
  User                    Primary Problem         Value Provided by
                                                  Orchivio
  ----------------------- ----------------------- -----------------------
  Independent developers  Design, implementation, An AI-assisted
                          and testing consume too development workflow
                          much time               for small teams or solo
                                                  developers

  Business users /        Need business           Visual modeling
  citizen developers      applications without    combined with
                          writing extensive code  natural-language
                                                  assistance

  Software engineers      Repetitive CRUD and     Model-driven generation
                          administration screens  and automated tests

  Software development    Reuse and maintenance   Templates, components,
  firms                   costs across projects   and environment
                                                  management

  Enterprise IT teams     Security, auditing,     RBAC, audit trails,
                          access control, and     policies, and
                          self-hosting            on-premises deployment

  Open-source developers  Vendor lock-in and      Extension APIs,
                          limited extensibility   plugins, and portable
                                                  artifacts
  -----------------------------------------------------------------------

## 2.3 Differentiation

-   **AI-native:** Integrate AI throughout the development lifecycle
    rather than limiting it to a chat panel.
-   **Model-first:** Store application structure as version-controlled,
    machine-readable models.
-   **Agentic but governed:** Let agents plan, execute, and validate
    within explicit permissions and approval gates.
-   **Open and portable:** Make data, models, code, and tests
    exportable.
-   **Self-hostable:** Support a progression from local development to
    organizational infrastructure.
-   **Composable:** Keep the core small and extend it through
    connectors, UI components, generators, and agents.


## 2.4 Benchmark Against Mendix (Summary)

Mendix is a mature, enterprise low-code platform (desktop IDE "Studio Pro",
proprietary runtime, Mendix Cloud / private cloud) that has added extensive
generative-AI assistance ("Maia": planning, building, explaining, MCP client
and server capabilities) and agent-building features for apps during
2025--2026. It is the reference benchmark for what enterprise users expect
from a model-driven platform.

**Positioning rule:** Orchivio does not aim for feature parity. It adopts
the capabilities that make a model-driven platform *usable for real
business applications* (logic, security, UI composition, integration,
delivery), and differentiates on openness, governed AI, and auditability.

| Area | Mendix (publicly documented) | Orchivio v1.0 | Orchivio v1.1 Response | Priority |
|---|---|---|---|---|
| Domain model | Entities, associations, enumerations, calculated attributes, indexes, event handlers, inheritance, external entities | Entities, fields, relationships | Enumerations, computed fields, indexes, modules, transient entities, event hooks, external entities (4.3.1, 4.6, 4.9) | P0/P1/P2 |
| Business logic | Visual server-side and client-side logic flows, expressions, rules | Declarative validation only; workflows P1 | Typed expression language (P0); Server/Client Flows, debugger, flow tests (P1) | P0/P1 |
| Human workflows | Workflow editor with user tasks, timers, boundary/notification events | Not covered | Process model, task inbox, SLA/escalation (4.12) | P1/P2 |
| App security | User roles, module roles, entity access with row constraints, page/flow access, SSO | Studio RBAC + simple per-entity roles | Two-plane security, row/field access, consistency checker, end-user auth/OIDC (4.7) | P0/P1 |
| UI composition | Layouts, snippets, building blocks, page templates, design system, pluggable widgets, AI theming | Model-driven CRUD pages | Layouts, fragments, templates, conditional UI, design tokens, AI theming, component SDK (4.8) | P0--P2 |
| Integration | Publish/consume REST, OData, business events, external entities, connectors | Future connectors | OpenAPI import, webhooks, outbox events, external entities, OData read (4.9) | P1/P2 |
| AI-assisted development | Maia planning/building/explaining, test-data generation (beta), log analysis, MCP client, Studio as MCP server for external agents | AI Builder, repair loop | Explain mode, test data, Studio MCP Server/Client under approval gates, AI lint fixes, log analysis (4.9, 4.13, 7.5) | P0/P1 |
| AI inside apps | Agents kit, GenAI connectors, agent builder, guardrails, agent observability | Not covered | AI flow steps, agents, knowledge bases, guardrails, AI observability (4.11) | P1/P2 |
| Version control | Git-based team server, branching, merge, history | Linear revisions; Git sync P1 | Branch-ready revision graph (P0), semantic 3-way merge, Git-native file format (4.10) | P0/P1 |
| Delivery & ops | Environments, deployment pipelines, constants, zero-downtime deploys, OpenTelemetry, LTS + update assistant | Preview; deployment P1 | Environments, signed deployment packages, expand/contract migrations, OTel, version channels, upgrade assistant (4.10, 4.13) | P1/P2 |
| Quality insight | Consistency checks, best-practice recommendations, unit testing, test automation | Schema validation, generated tests | Model linter with rule catalog (P0), dependency graph (P0), flow tests, coverage over model elements | P0/P1 |
| Agile & feedback | Epics/stories, in-app feedback, AI-generated backlog | Requirement extraction | Story board with traceability, preview feedback widget, AI clustering (4.10) | P1 |
| Ecosystem & governance | Modules, Marketplace, Control Center, policy and audit tools | Plugin catalog P2 | Self-hostable module registry, governance console, policy packs (4.13) | P2 |
| Mobile | PWA, offline-first, native mobile | Deferred | PWA (P1), offline sync (P2); native remains out of scope | P1/P2 |

**Where Orchivio deliberately differs from Mendix:**

-   **Open model and runtime:** the application model is an open, versioned
    JSON/YAML schema and the runtime is open source; no proprietary project
    file format.
-   **Web-first Studio and self-hosting first:** local Docker Compose to
    on-premises Kubernetes, without a mandatory vendor cloud.
-   **Governed AI as a core invariant:** every AI-originated change---from
    the built-in AI Builder *or* external agents via MCP---becomes a
    reviewable proposal subject to approval levels (Section 12.3).
-   **No arbitrary code in the core runtime:** extension code (the equivalent
    of custom code actions) runs only in isolated workers behind typed
    contracts.
-   **Provider-neutral AI with local models** as a first-class option for
    both development and in-app AI.

------------------------------------------------------------------------

# 3. Scope

## 3.1 MVP Scope

-   User authentication, organizations, and projects
-   Application models and revision history
-   Natural-language requirements intake and structured requirement
    extraction
-   AI-assisted proposals for data models, CRUD screens, forms, and list
    views
-   Diff review, approval, application, and rollback
-   CRUD application runtime
-   Declarative validation and business rules
-   Browser-based preview
-   Basic automated test generation and execution
-   API specification generation, including OpenAPI
-   Local startup with Docker Compose
-   LLM adapters, including a mock provider and at least one real
    provider
-   Audit events and job history
-   **(v1.1)** Typed, side-effect-free expression language for
    validation, computed fields, conditional UI, and access constraints
-   **(v1.1)** Two-plane security: platform RBAC separated from
    generated-application security, including row-level access rules
-   **(v1.1)** Module-ready, branch-ready model schema (single module and
    main branch exposed in the MVP UI)
-   **(v1.1)** Enumerations, computed fields, and indexes
-   **(v1.1)** Dependency (find-usages) graph and model linter baseline
-   **(v1.1)** AI Explain mode and constraint-aware synthetic test data

## 3.2 Explicitly Deferred from MVP

-   Fully autonomous, general-purpose multi-agent development
-   Advanced real-time collaborative editing
-   One-click deployment to every cloud provider
-   Advanced native mobile application generation
-   A large plugin marketplace
-   Full BPMN compatibility
-   Running arbitrary third-party plugin code inside the main process
-   Unapproved production deployments or destructive database migrations
-   **(v1.1)** Visual Server/Client Flow editor and debugger (Release 0.2)
-   **(v1.1)** Model branching and merge UI (Release 0.4)
-   **(v1.1)** Human workflows and AI features inside generated apps
    (Release 0.5)
-   **(v1.1)** Feature parity with any commercial low-code platform

## 3.3 Future Expansion

-   Visual workflows and event-driven logic
-   External-system connectors and MCP integration
-   Specialist multi-agent collaboration
-   Git synchronization and pull-request generation
-   Kubernetes, multi-cloud, and on-premises deployment
-   Template and plugin catalog
-   Monitoring, error analysis, and AI-generated improvement proposals
-   Enhanced mobile and offline capabilities
-   Enterprise policies, SSO, SCIM, and audit reports
-   **(v1.1)** Studio MCP Server and MCP client with governed tool access
-   **(v1.1)** Environment promotion pipeline and signed deployment packages
-   **(v1.1)** Agents, knowledge bases, and guardrails inside generated apps
-   **(v1.1)** Self-hostable module registry and governance console

------------------------------------------------------------------------

# 4. Functional Requirements

Priority levels: `P0 = required for MVP`, `P1 = important next`,
`P2 = future`.

## 4.1 Workspace and Projects

  ------------------------------------------------------------------------
  ID               Requirement                   Priority Acceptance
                                                          Criteria
  ---------------- ---------------- --------------------- ----------------
  FR-001           User                                P0 Protected APIs
                   registration,                          are available
                   login, and                             only to
                   logout                                 authenticated
                                                          users

  FR-002           Create and edit                     P0 Each project
                   organizations                          belongs to an
                   and projects                           organization

  FR-003           Role-based                          P0 Owner, Admin,
                   access control                         Editor, and
                                                          Viewer roles are
                                                          enforced

  FR-004           Project activity                    P1 Important
                   history                                actions record
                                                          actor and
                                                          timestamp

  FR-005           Project export                      P0 Models can be
                   and import                             transferred in a
                                                          reproducible
                                                          format
  ------------------------------------------------------------------------

## 4.2 AI Application Engineering

  ---------------------------------------------------------------------------
  ID               Requirement                     Priority Acceptance
                                                            Criteria
  ---------------- ------------------ --------------------- -----------------
  FR-010           Accept                                P0 Requests are
                   natural-language                         persisted and
                   requirements                             associated with a
                                                            project

  FR-011           Decompose requests                    P0 Structured output
                   into requirements,                       is validated
                   entities, pages,                         against a schema
                   and permissions                          

  FR-012           Generate an                           P0 Plan includes
                   implementation                           tasks,
                   plan                                     dependencies,
                                                            risks, and
                                                            completion
                                                            criteria

  FR-013           Generate proposed                     P0 Diffs and
                   model changes                            validation
                                                            results are shown
                                                            before
                                                            application

  FR-014           Approve or reject                     P0 Unapproved
                   AI proposals                             changes do not
                                                            modify the
                                                            canonical model

  FR-015           Preserve AI                           P1 Inputs,
                   rationale and                            model/provider,
                   execution history                        tools, results,
                                                            and duration are
                                                            recorded

  FR-016           Analyze failures                      P1 The system
                   and propose                              presents likely
                   repairs                                  causes and next
                                                            actions

  FR-017           AI usage and                          P1 Limits can be
                   budget limits                            configured per
                                                            project

  FR-018           Switch between                        P1 Providers are
                   local and hosted                         accessed through
                   LLMs                                     a common adapter
  ---------------------------------------------------------------------------

## 4.3 Application Model

  ------------------------------------------------------------------------
  ID               Requirement                   Priority Acceptance
                                                          Criteria
  ---------------- ---------------- --------------------- ----------------
  FR-020           Define entities,                    P0 Types, required
                   fields, and                            fields,
                   relationships                          constraints, and
                                                          references are
                                                          validated

  FR-021           Define pages,                       P0 Pages can be
                   routes, and                            rendered from
                   screens                                their models

  FR-022           Support form,                       P0 Views can be
                   list, and detail                       connected to
                   views                                  CRUD operations

  FR-023           Support                             P0 Server-side
                   declarative                            validation is
                   validation                             always enforced

  FR-024           Define actions                      P1 Only permitted
                   and workflows                          actions can
                                                          execute

  FR-025           Version                             P0 Each change is
                   application                            stored as an
                   models                                 immutable
                                                          revision

  FR-026           Diff and roll                       P0 Earlier
                   back model                             revisions can be
                   revisions                              restored

  FR-027           Perform static                      P0 Invalid
                   model checks                           references,
                                                          cycles, and
                                                          undefined fields
                                                          are detected
  ------------------------------------------------------------------------

## 4.3.1 Additional Application Model Requirements (v1.1)

| ID | Requirement | Priority | Acceptance Criteria |
|---|---|---|---|
| FR-028 | Enumerations | P0 | Enum values are typed, referenced by fields and expressions, and value removal is flagged as a risky change |
| FR-029 | Modules (namespaces) | P0 schema / P1 UI | Every element belongs to a module; cross-module references are explicit; the MVP UI exposes a single default module |
| FR-131 | Indexes and composite unique constraints | P0 | Indexes are generated in migrations; the linter flags filtered/sorted fields without indexes |
| FR-132 | Transient (non-persistent) entities | P1 | Usable as form/view models and integration payloads; never create tables |
| FR-133 | File and image attributes | P1 | Stored in artifact storage with size/type limits, malware-scan hook, and access inherited from the owning record |
| FR-134 | Optimistic locking and soft delete | P1 | Concurrent update conflicts return a defined error; soft-deleted rows are excluded by default |
| FR-135 | Entity generalization (inheritance) | P2 | Strategy (single-table or joined) is explicit; deep hierarchies trigger linter warnings |

## 4.4 Runtime, APIs, and Generation

  ------------------------------------------------------------------------
  ID               Requirement                   Priority Acceptance
                                                          Criteria
  ---------------- ---------------- --------------------- ----------------
  FR-030           CRUD API runtime                    P0 Data operations
                                                          pass
                                                          authorization,
                                                          validation, and
                                                          auditing

  FR-031           Model-driven UI                     P0 Pages are
                   renderer                               rendered from
                                                          application
                                                          models

  FR-032           Generate OpenAPI                    P0 Specifications
                   specifications                         remain
                                                          consistent with
                                                          implemented APIs

  FR-033           Generate typed                      P1 Artifacts record
                   clients and code                       their
                   artifacts                              source-model
                                                          revision

  FR-034           Propose database                    P1 Destructive
                   migrations                             changes trigger
                                                          warnings and
                                                          approval

  FR-035           Provide an                          P0 Preview data is
                   application                            isolated from
                   preview                                production data

  FR-036           Export build                        P1 Artifacts can be
                   artifacts                              obtained in
                                                          standard formats
  ------------------------------------------------------------------------

## 4.5 Quality and Operations

  -------------------------------------------------------------------------
  ID               Requirement                   Priority Acceptance
                                                          Criteria
  ---------------- ---------------- --------------------- -----------------
  FR-040           Generate and run                    P0 Test results,
                   automated tests                        logs, and
                                                          failures are
                                                          persisted

  FR-041           Perform security                    P1 Dependencies,
                   checks                                 configuration,
                                                          and generated
                                                          models are
                                                          inspected

  FR-042           Display job                         P1 Long-running job
                   progress and                           status can be
                   support                                queried
                   cancellation                           

  FR-043           Maintain audit                      P0 Authentication,
                   logs                                   permission,
                                                          model, and export
                                                          events are
                                                          recorded

  FR-044           Provide backup                      P0 Database and
                   and restore                            artifacts can be
                   procedures                             restored

  FR-045           Provide metrics                     P1 API, database,
                   and health                             and worker status
                   checks                                 can be inspected
  -------------------------------------------------------------------------


## 4.6 Logic, Expressions, and Automation (v1.1)

| ID | Requirement | Priority | Acceptance Criteria |
|---|---|---|---|
| FR-050 | Typed expression language | P0 | Expressions are parsed, type-checked against the model, side-effect free, and bounded in execution time/depth |
| FR-051 | Computed (derived) fields | P0 | Stored or virtual; dependencies are tracked; cycles are rejected at validation time |
| FR-052 | Server Flows | P1 | Allowlisted steps (retrieve, create, change, commit, delete, decision, loop, call flow, call integration, AI step, raise error) execute transactionally under the caller's app security; elevation requires A3 approval |
| FR-053 | Client Flows | P1 | Limited to navigation, client validation, messages, and calling Server Flows; cannot bypass server authorization |
| FR-054 | Entity event hooks | P1 | Before/after create/update/delete hooks bind to Server Flows with bounded recursion |
| FR-055 | Scheduled jobs | P1 | Cron-style schedules defined in the model, enabled per environment, single execution across nodes, defined missed-run policy |
| FR-056 | Background task queues | P1 | Async execution with retry, backoff, dead-letter queue, and concurrency limits |
| FR-057 | Flow debugger | P1 | Breakpoints, stepping, and variable inspection in preview environments only |
| FR-058 | Flow unit tests | P1 | Declarative cases with fixtures and assertions run in CI and the Test Center |

## 4.7 Application Security Model (v1.1)

| ID | Requirement | Priority | Acceptance Criteria |
|---|---|---|---|
| FR-060 | Separate platform and application security planes | P0 | Studio roles never grant runtime data access in generated apps, and app roles never grant Studio access |
| FR-061 | Application roles | P0 | Roles are defined in the model; module-level role mapping is P1 |
| FR-062 | Entity CRUD access per app role | P0 | Deny by default; every data path is checked |
| FR-063 | Row-level access constraints | P0 | Expression-based (e.g., `record.owner == $currentUser`), compiled to SQL predicates applied to reads, updates, deletes, counts, and aggregates |
| FR-064 | Field-level read/write access | P1 | Hidden fields are removed from API responses, not merely hidden in the UI |
| FR-065 | Page, action, and flow access | P0 pages/actions, P1 flows | Unauthorized navigation and invocation are rejected server-side |
| FR-066 | Security consistency checker | P0 | Detects pages exposing unreadable entities, unreachable pages, roles without any access, and anonymous over-exposure |
| FR-067 | End-user authentication for generated apps | P0 local / P1 OIDC / P2 SAML | Anonymous access requires explicit opt-in and A3 approval |
| FR-068 | "View as role" simulation in preview | P1 | Preview renders and enforces the selected role's permissions |

## 4.8 UI Composition and Design System (v1.1)

| ID | Requirement | Priority | Acceptance Criteria |
|---|---|---|---|
| FR-070 | Layouts and navigation menus | P0 basic / P1 profiles | Pages declare a layout; menus respect page access |
| FR-071 | Reusable fragments with parameters | P1 | Fragments are versioned model elements with typed parameters |
| FR-072 | Page templates and building blocks | P1 | Templates are used by both humans and the AI UI Designer; AI is constrained to approved templates/components |
| FR-073 | Conditional visibility and editability | P0 | Defined by expressions; editability is also enforced server-side |
| FR-074 | Data grid | P0 basic / P1 export | Server-side filtering, sorting, paging; CSV export requires export permission and is audited |
| FR-075 | Design tokens and theming | P1 | Tokens drive CSS variables; AI can propose a theme from brand inputs, subject to contrast checks |
| FR-076 | Custom component SDK | P2 | Typed props manifest, sandboxed rendering (CSP/isolation), signed packages |
| FR-077 | Accessibility lint | P1 | Missing labels, contrast, and keyboard traps are reported per page |
| FR-078 | Application-level i18n | P1 | Translatable strings live in the model with locale fallback; AI translations are proposals requiring approval (ja/en first) |

## 4.9 Integration and Interoperability (v1.1)

| ID | Requirement | Priority | Acceptance Criteria |
|---|---|---|---|
| FR-080 | Published REST APIs | P0 CRUD / P1 custom | Versioned, generated OpenAPI; custom endpoints bind to Server Flows |
| FR-081 | Consumed REST via OpenAPI import | P1 | Imports produce typed integration definitions and mappings; destinations are allowlisted per environment |
| FR-082 | Inbound and outbound webhooks | P1 | Inbound signatures are verified; outbound delivery retries with SSRF protection |
| FR-083 | Business events | P1 outbox / P2 brokers | Transactional outbox guarantees at-least-once delivery; broker adapters added later |
| FR-084 | External entities | P2 | Read-through to external REST/OData/DB with explicit caching and security policy |
| FR-085 | OData v4 read-only publishing | P2 | For BI tools; respects row- and field-level security |
| FR-086 | Orchivio Studio MCP Server | P1 | External agents can read models, propose changes, validate, and run tests; all writes become proposals subject to approval levels and audit |
| FR-087 | MCP client for the AI Builder | P1 | Server allowlist per organization; per-tool approval level; tool output treated as untrusted data |
| FR-088 | Generated app as MCP server | P2 | Selected Server Flows exposed as tools with app-role authorization |
| FR-089 | Data import/export with mapping | P1 | CSV/Excel import validates rows, reports errors per row, and runs through app security |

## 4.10 Collaboration, Versioning, and Delivery (v1.1)

| ID | Requirement | Priority | Acceptance Criteria |
|---|---|---|---|
| FR-090 | Branch-ready revision graph | P0 | Revisions store parent revision IDs; MVP UI exposes only `main` |
| FR-091 | Branches and change sets | P1 | Users and AI jobs work on isolated branches |
| FR-092 | Semantic three-way model merge | P1 | Element-level conflict detection and resolution UI; AI may explain conflicts but resolution requires approval |
| FR-093 | Git-native canonical file format | P1 | One file per element, stable key ordering, round-trip lossless; enables PR review |
| FR-094 | Environments and promotion pipeline | P1 | Configurable stages (e.g., dev/test/acceptance/production) with promotion gates |
| FR-095 | Immutable deployment packages | P1 | Package = model revision + runtime version + migration plan + SBOM, signed and reproducible |
| FR-096 | Environment constants and secret references | P1 | Secrets are never stored in the model; only references to a secret store |
| FR-097 | Near-zero-downtime migrations | P2 | Expand/contract migration plans with rolling or blue/green switchover |
| FR-098 | Requirements and story board with traceability | P1 | Links requirement ↔ model elements ↔ tests ↔ revisions are navigable and exportable |
| FR-099 | Preview feedback widget | P1 | Captures page, element, and optional screenshot; AI clusters feedback into proposed stories |

## 4.11 AI Capabilities Inside Generated Applications (v1.1)

| ID | Requirement | Priority | Acceptance Criteria |
|---|---|---|---|
| FR-100 | AI step in Server Flows | P1 | Versioned prompt template, structured output schema, provider via adapter, per-call budget and timeout |
| FR-101 | Agent definitions | P2 | Instructions, tool allowlist bound to app flows, step limits, human-in-the-loop checkpoints via user tasks |
| FR-102 | Knowledge bases | P2 | Vector index (e.g., pgvector); retrieval respects the same row-level rules as the source data |
| FR-103 | Guardrails | P1 | PII redaction, retrieved-content injection filtering, and output schema validation before use |
| FR-104 | AI observability | P1 | Traces, tokens, cost, latency, and user feedback per AI step/agent run |
| FR-105 | AI provider policy per app | P1 | Allowed providers (including local-only) and monthly budget per environment |

## 4.12 Process Orchestration --- Human Workflows (v1.1)

| ID | Requirement | Priority | Acceptance Criteria |
|---|---|---|---|
| FR-110 | Process definitions | P1 basic / P2 advanced | User tasks, system tasks, decisions, parallel paths, timers; boundary and notification events in P2 |
| FR-111 | Task inbox | P1 | Assignment by role or expression, delegation, due dates, escalation |
| FR-112 | Process versioning and instance migration | P2 | Running instances follow an explicit migration policy |
| FR-113 | Process monitoring | P2 | Throughput, bottlenecks, and overdue tasks per process |

## 4.13 Governance, Quality Insight, and Lifecycle (v1.1)

| ID | Requirement | Priority | Acceptance Criteria |
|---|---|---|---|
| FR-120 | Model linter | P0 baseline | Rule catalog (security, performance, maintainability, accessibility) with severities and justified suppressions; ≥20 rules at MVP |
| FR-121 | AI-proposed lint fixes | P1 | Fixes are proposals with diffs and require approval |
| FR-122 | Dependency graph / find usages | P0 | Every element lists inbound/outbound references; used for diff impact analysis |
| FR-123 | Runtime observability | P1 | OpenTelemetry traces/metrics/logs, log viewer, AI-assisted log analysis |
| FR-124 | Governance console | P2 | App inventory, policy packs, AI usage, and audit export per organization |
| FR-125 | Runtime version channels and upgrade assistant | P1 | LTS and rolling channels; upgrade previews show model migrations and deprecations before applying |
| FR-126 | Module registry | P2 | Self-hostable, signed, semver-versioned modules and templates |
| FR-127 | AI Explain mode | P0 | Explains any element, page, or flow, citing element IDs; read-only (A0) |
| FR-128 | Synthetic test data generation | P0 | Data satisfies model constraints and never derives from production data |
| FR-129 | PWA and offline | P1 PWA / P2 offline | Installable responsive app; offline sync with explicit conflict rules later |

------------------------------------------------------------------------

# 5. Non-Functional Requirements

  -----------------------------------------------------------------------
  Area                                Requirement
  ----------------------------------- -----------------------------------
  Security                            Deny by default, least privilege,
                                      CSRF protection, input validation,
                                      secret protection

  Isolation                           Enforce organization and project
                                      boundaries server-side

  Reliability                         Design retries for idempotency and
                                      prevent unintended duplicate
                                      execution

  Observability                       Structured logs, correlation IDs,
                                      job state, and failure reasons

  Extensibility                       Add LLMs, UI components,
                                      generators, and connectors through
                                      defined interfaces

  Portability                         Docker-based deployment,
                                      externalized configuration,
                                      standard APIs, and data export

  Maintainability                     CI for type checks, linting, and
                                      unit/integration/E2E tests

  Performance                         Keep non-LLM CRUD paths low-latency

  Privacy                             Minimize LLM context and make
                                      transmission scope visible and
                                      configurable

  Accessibility                       Keyboard navigation, accessible
                                      labels, contrast, and semantic HTML

  Internationalization                Design for Japanese and English;
                                      separate UI strings from code

  Licensing                           Track dependency licenses and
                                      generate SBOM and third-party
                                      notices
  -----------------------------------------------------------------------

Performance values remain provisional until measured in the MVP. An
initial example target is p95 below 500 ms for ordinary read APIs
without LLM calls in a small single-node deployment.

## 5.1 Additional Non-Functional Requirements (v1.1)

| Area | Requirement |
|---|---|
| Expression safety | Expression evaluation is deterministic, sandboxed, and bounded (max depth, max collection size, timeout); no I/O |
| Security-rule performance | Row-level predicates must use indexes; the linter warns on unindexed constraint fields |
| Merge correctness | Merge of two branches must be deterministic and round-trip lossless; property-based tests required |
| Delivery reproducibility | Identical deployment package inputs must produce byte-identical artifacts |
| Upgrade compatibility | Model schema N-1 must load in runtime N; deprecations are announced at least one minor release before removal |
| Interoperability | Integrations use open standards (OpenAPI, OIDC, OpenTelemetry, MCP, CloudEvents where applicable) |
| AI-in-app cost control | Hard budget ceilings stop AI steps before overspend; failures degrade gracefully |

------------------------------------------------------------------------

# 6. System Architecture

## 6.1 Logical Architecture

``` text
┌──────────────────────────────────────────────────────────────────────┐
│                         Orchivio Web Studio                          │
│ AI Builder / Model Explorer / Page & Flow Builders / Process Builder │
│ Diff & Merge / Preview (view-as-role) / Test Center / Story Board    │
│ Deployments / Observability / Governance                             │
└──────────────┬──────────────────────────────────────┬────────────────┘
               │ HTTPS / REST / SSE                   │ MCP (Streamable HTTP)
┌──────────────▼──────────────────────────────────────▼────────────────┐
│                     API Gateway + Studio MCP Server                   │
│ Platform AuthN/AuthZ / Validation / Rate Limit / Audit / OpenAPI      │
└──────┬──────────────────┬────────────────────┬──────────────────────┘
       │                  │                    │
┌──────▼───────────┐ ┌────▼─────────────────┐ ┌▼───────────────────────┐
│ Model Service    │ │ AI Orchestrator      │ │ Delivery Manager       │
│ Schema/Revisions │ │ Planner → Tools →    │ │ Environments/Packages  │
│ Branch & Merge   │ │ Validator → Approval │ │ Migration Plans/Promote│
│ Dependency Graph │ │ MCP Client / Context │ │ Version Channels       │
│ Model Linter     │ └────┬─────────────────┘ └┬───────────────────────┘
└──────┬───────────┘      │                    │
       │           ┌──────▼────────────────────▼──────┐
       │           │ Job Queue / Isolated Workers      │
       │           │ AI / Build / Test / Extension     │
       │           └──────┬────────────────────────────┘
┌──────▼──────────────────▼────────────────────────────────────────────┐
│                     Application Runtime (generated apps)              │
│ App Security (roles, row/field rules) / CRUD & REST / UI Renderer     │
│ Expression Engine / Logic Engine (flows) / Process Engine (tasks)     │
│ Scheduler & Task Queue / Integration Gateway (REST, webhooks, outbox) │
│ AI Runtime (AI steps, agents, knowledge bases, guardrails)            │
└──────┬───────────────────────────────────────────────────────────────┘
       │
┌──────▼───────────────────────────────────────────────────────────────┐
│ PostgreSQL (+pgvector) / Object Storage / Cache / Artifacts / Secrets │
└──────┬───────────────────────────────────────────────────────────────┘
       │
┌──────▼──────────────────────┐   ┌───────────────────────────────────┐
│ LLM Provider Adapters       │   │ Telemetry (OpenTelemetry Collector)│
│ Cloud / Local / Mock        │   │ Traces / Metrics / Logs            │
└─────────────────────────────┘   └───────────────────────────────────┘
```

Components introduced in v1.1 are activated progressively according to the
roadmap (Section 15); the MVP ships the Model Service with dependency graph
and linter baseline, the Expression Engine, and App Security in the runtime.

## 6.2 Architecture Principles

-   Start with a **modular monolith plus isolated workers**. Do not
    split everything into microservices on day one.
-   Keep clear module boundaries between API, model management, runtime,
    and AI orchestration.
-   Execute builds, tests, and generated code in workers separated from
    the API process.
-   Use a job queue for long-running operations, with progress,
    cancellation, retry, and timeout support.
-   Use PostgreSQL for metadata and application data by default; use
    object storage-compatible storage for files and artifacts.
-   Make the local development environment reproducible through Docker
    Compose.
-   Split services later based on measured load, trust boundaries, and
    team needs.
-   **(v1.1)** Keep a strict boundary between the **platform plane**
    (Studio, Orchivio users) and the **application plane** (generated apps,
    their end users), including separate identities, roles, and audit
    streams.
-   **(v1.1)** All business logic in generated apps is interpreted from the
    model by the Expression, Logic, and Process Engines; extension code runs
    only in isolated workers through typed contracts.
-   **(v1.1)** External agents (via MCP) and the built-in AI Builder share
    one proposal/approval pipeline; there is no privileged side channel.
-   **(v1.1)** Use open standards for every external surface (OpenAPI, OIDC,
    OpenTelemetry, MCP) to preserve portability.

## 6.3 Proposed Initial Technology Stack

  -----------------------------------------------------------------------
  Layer                   Proposal                Rationale / Notes
  ----------------------- ----------------------- -----------------------
  Web UI                  React + TypeScript      Component ecosystem and
                                                  type safety

  UI foundation           CSS variables and       Theming and brand
                          accessible component    flexibility
                          primitives              

  API                     Python + FastAPI        AI integration, typed
                                                  APIs, OpenAPI

  Schemas                 Pydantic + JSON Schema  Validate models and LLM
                                                  output

  Metadata DB             PostgreSQL              Transactions, JSONB,
                                                  reliability

  Queue                   DB-backed jobs          Start simple; evolve
                          initially or a          with load
                          Redis-based queue       

  Cache                   Redis when needed       Rate limiting,
                                                  short-lived state,
                                                  caching

  Artifact storage        Local storage /         Exports, logs, and
                          S3-compatible storage   build artifacts

  LLM                     Common provider adapter Replaceable hosted and
                                                  local models

  Testing                 pytest, Vitest,         Unit through E2E
                          Playwright              

  Packaging               Docker Compose, later   Gradual path from local
                          Helm or equivalent      to production

  CI                      GitHub Actions or       Automated quality gates
                          equivalent              
  -----------------------------------------------------------------------

Validate the stack with a proof of concept. Record the selected option,
alternatives, rationale, and operational trade-offs in Architecture
Decision Records (ADRs).


## 6.4 Runtime Engine Design (v1.1)

| Engine | Responsibility | Key Design Decisions |
| ---|---|--- |
| Expression Engine | Parse, type-check, and evaluate expressions; compile access constraints to SQL | Candidate: CEL-compatible subset or a small custom grammar (ADR in Phase 0); pure, deterministic, bounded |
| Logic Engine | Execute Server Flows and Client Flows step by step | Interpreted state machine over a typed step graph; transactions per flow; step-level tracing for debugger and OTel |
| Process Engine | Persist long-running processes and user tasks | Event-sourced instances on PostgreSQL; timers via scheduler; idempotent step handlers |
| Scheduler & Task Queue | Scheduled jobs and background flows | Leader election or DB advisory locks for single execution; retries, backoff, DLQ |
| Integration Gateway | Outbound REST, webhooks, outbox publishing | Per-environment destination allowlist, SSRF protection, circuit breakers, secret references only |
| AI Runtime | AI steps, agents, knowledge-base retrieval | Same provider adapters as the Studio; guardrail pipeline (redact → call → validate); per-app budget |
| App Security | Authenticate end users, enforce roles and row/field rules | Constraints injected into every query by the runtime data layer; optional PostgreSQL RLS as defense in depth (ADR) |

The Logic Engine never executes generated source code; it interprets
validated model elements. This keeps the "no arbitrary LLM output at
runtime" principle intact while allowing rich business logic.

------------------------------------------------------------------------

# 7. AI Engine Design

## 7.1 Agent Responsibilities

Initially use one orchestrator with specialized task roles. Adopt
multiple independently running agents only when evidence shows that
specialization is beneficial.

  -----------------------------------------------------------------------
  Role                    Responsibility          Permitted Output
  ----------------------- ----------------------- -----------------------
  Requirements Analyst    Clarify ambiguity,      Requirements model and
                          business rules, and     questions
                          acceptance criteria     

  Solution Planner        Plan tasks,             Structured
                          dependencies, and risks implementation plan

  Data Modeler            Propose entities,       Model change proposal
                          fields, relationships,  
                          and constraints         

  UI Designer             Propose pages,          Page-model proposal
                          components, and         
                          interactions            

  Logic Designer          Propose declarative     Approved logic DSL
                          rules and workflows     

  Test Engineer           Propose test cases and  Test definitions
                          fixtures                

  Reviewer                Check requirement       Review findings
                          alignment, risky        
                          changes, and omissions  

  Repair Agent            Propose fixes based on  Diff or model change
                          test failures           proposal
  -----------------------------------------------------------------------

These roles do not require separate LLMs. Specialize only where measured
quality, cost, and latency justify it.

**Additional roles (v1.1):**

| Role | Responsibility | Permitted Output |
|---|---|---|
| Security Analyst | Derive app roles and row/field rules from requirements; review permission expansion | Security-model proposal and findings |
| Integration Designer | Map OpenAPI imports and webhooks to integration definitions | Integration proposal |
| Flow Designer | Propose Server/Client Flows and process definitions | Flow/process proposal |
| Explainer | Explain existing elements and impacts with element-ID citations | Read-only explanation (A0) |
| Test Data Generator | Generate constraint-satisfying synthetic data | Fixture sets |
| Lint Fixer | Propose fixes for linter findings | Diff proposal |
| Ops Analyst | Summarize logs/traces and correlate failures with revisions | Findings and repair proposals |

## 7.2 Standard AI Loop

``` text
User Request
    ↓
Gather Context (models, specifications, history, constraints)
    ↓
Structure Requirements and Identify Missing Information
    ↓
Generate an Implementation Plan
    ↓
Generate Proposed Model Changes
    ↓
Validate Schema and Policy
    ↓
Present Diff, Impact, and Risks
    ↓
Human Approval (where required)
    ↓
Apply as a New Revision
    ↓
Generate / Build / Test
    ↓
Analyze Failures and Propose Repairs
    ↓
Record Results, Evidence, and Cost
```

## 7.3 Tool Execution Guardrails

-   LLMs cannot execute arbitrary shell commands directly. Tool names
    and arguments must be schema-defined and allowlisted.
-   Build and test generated code in isolated environments. Do not mount
    host secrets or unrelated files.
-   Deny network access by default; allow only explicitly approved
    destinations when required.
-   Limit CPU, memory, disk, process count, and execution time.
-   Treat model output as untrusted input and validate syntax, schemas,
    permissions, and consistency.
-   Do not treat prompt-injection instructions found in external
    documents, source code, or user data as trusted instructions.
-   Preserve proposals, approvers, application results, and test results
    as audit evidence.
-   Require explicit approval for production deployment, permission
    changes, data deletion, and destructive migrations.

## 7.4 Context Management

Avoid sending unlimited context to an LLM. Prioritize:

1.  Current request and acceptance criteria
2.  Target model and related models
3.  Recent diffs and validation errors
4.  Project conventions, allowed components, and permitted tools
5.  Relevant code or history only when needed

For each AI job, record input references, provider/model,
prompt-template version, output-schema version, token usage, cost,
duration, and validation results. Exclude or redact secrets before
sending context to a provider.


## 7.5 Agent Interoperability via MCP (v1.1)

Mendix exposes its IDE assistant as an MCP server so that external coding
agents can modify apps. Orchivio adopts the same interoperability goal but
binds it to its governance model.

**Studio MCP Server (FR-086)** exposes a small, versioned tool set:

| Tool | Approval Level | Behavior |
|---|---|---|
| `model.read`, `model.search`, `model.usages` | A0 | Read model elements and dependency graph within the caller's project scope |
| `model.explain` | A0 | Explanation with element-ID citations |
| `model.validate`, `model.lint` | A0 | Run validation/linter on a revision or proposal |
| `proposal.create` | A1 | Submit a model change set; never applied directly |
| `tests.run` | A1 | Run tests in an isolated preview environment |
| `proposal.apply` | A2+ | Only callable when an approver has approved the proposal in the Studio |

Rules:

-   Authentication via scoped, expiring personal or service tokens; tokens
    are bound to one organization and optionally one project.
-   Every MCP call is audited with token, tool, arguments hash, and result.
-   Rate limits and per-token budgets apply.
-   External agents cannot raise their own approval level.

**MCP Client (FR-087)** lets the AI Builder call approved external MCP
servers (documentation, ticketing, design sources). Server registration is an
organization-admin action (A3); tool results are untrusted data and are
never followed as instructions.

## 7.6 Additional AI Assistance (v1.1)

-   **Explain mode (P0):** natural-language explanation of any element,
    page, flow, or diff, with citations to element IDs.
-   **Synthetic test data (P0):** constraint-aware fixtures for preview and
    tests; never derived from production data.
-   **Security-aware generation (P0):** the AI Builder must propose app roles
    and access rules together with entities; proposals without access rules
    fail the linter.
-   **Lint fixes and theming (P1):** AI proposes fixes for linter findings and
    design-token themes from brand inputs.
-   **Log and trace analysis (P1):** correlate runtime errors with revisions
    and propose repairs.
-   **Feedback to stories (P1):** cluster preview feedback into proposed
    stories linked to model elements.

------------------------------------------------------------------------

# 8. Application Model Specification

## 8.1 Model Design Principles

-   The model is declarative, machine-readable, and version-controlled.
-   Each model format has a version and a migration strategy.
-   Generate UI, DB changes, APIs, tests, and documentation from a
    consistent source wherever practical.
-   Validate model changes before applying them and preserve the
    revision history.
-   Arbitrary embedded code is out of scope for the MVP. Any future
    extension mechanism must have explicit boundaries and permissions.

## 8.2 Conceptual Model

``` text
Organization
 ├── Membership ── User                       (platform plane)
 ├── PolicyPack                               (v1.1, P2)
 └── Project
      ├── Application
      │    ├── Branch                         (v1.1; MVP: main only)
      │    ├── AppRevision (parents[])        (v1.1: revision graph)
      │    │    ├── Module                    (v1.1)
      │    │    │    ├── EnumerationDefinition
      │    │    │    ├── EntityDefinition (fields, computed, indexes, hooks)
      │    │    │    ├── PageDefinition / LayoutDefinition / FragmentDefinition
      │    │    │    ├── FlowDefinition (server | client)          (v1.1)
      │    │    │    ├── ProcessDefinition                         (v1.1)
      │    │    │    ├── ScheduledJobDefinition                    (v1.1)
      │    │    │    ├── IntegrationDefinition (rest, webhook, event)
      │    │    │    └── AIStepDefinition / AgentDefinition        (v1.1)
      │    │    ├── SecurityModel (app roles, access rules)        (v1.1)
      │    │    ├── ThemeDefinition (design tokens)                (v1.1)
      │    │    └── TranslationSet                                 (v1.1)
      │    ├── Build / TestRun
      │    ├── DeploymentPackage                                   (v1.1)
      │    └── Deployment ── Environment (constants, secret refs)  (v1.1)
      ├── Requirement / Story / TraceLink / FeedbackItem           (v1.1)
      ├── AIConversation / AIJob / Proposal
      └── AuditEvent
```

## 8.3 Example Application Model

``` yaml
schema_version: "1.0"
application:
  key: inventory
  name: Inventory Manager
  version: "0.1.0"
  default_locale: en-US

entities:
  - name: Product
    display_name: Product
    fields:
      - name: id
        type: uuid
        primary_key: true
        generated: true
      - name: name
        type: string
        required: true
        max_length: 200
      - name: sku
        type: string
        required: true
        unique: true
      - name: quantity
        type: integer
        required: true
        minimum: 0
      - name: created_at
        type: datetime
        generated: true
    permissions:
      read: [viewer, editor, admin]
      create: [editor, admin]
      update: [editor, admin]
      delete: [admin]

pages:
  - key: product-list
    path: /products
    type: list
    entity: Product
    actions: [view, create, edit]

  - key: product-create
    path: /products/new
    type: form
    entity: Product
    mode: create
```

This is a conceptual example. The implementation's JSON Schema and
Pydantic models will be authoritative. Field types, roles, generated
fields, relationships, expressions, and actions must be defined
precisely in the implementation schema.

### 8.3.1 Extended Example (v1.1)

``` yaml
schema_version: "1.1"
application:
  key: inventory
  name: Inventory Manager
  default_locale: ja-JP
  locales: [ja-JP, en-US]

modules:
  - key: core
    enumerations:
      - name: StockStatus
        values: [in_stock, low, out_of_stock]
    entities:
      - name: Product
        fields:
          - { name: id, type: uuid, primary_key: true, generated: true }
          - { name: name, type: string, required: true, max_length: 200 }
          - { name: sku, type: string, required: true, unique: true }
          - { name: quantity, type: integer, required: true, minimum: 0 }
          - { name: reorder_level, type: integer, required: true, minimum: 0 }
          - name: status
            type: enum:StockStatus
            computed:
              mode: virtual
              expr: >
                quantity == 0 ? StockStatus.out_of_stock
                : (quantity <= reorder_level ? StockStatus.low : StockStatus.in_stock)
          - { name: owner, type: ref:system.User, required: true, default_expr: "$currentUser" }
        indexes:
          - { fields: [owner] }
        hooks:
          after_update: core.NotifyLowStock      # P1 (Server Flow)

    flows:                                       # P1
      - key: NotifyLowStock
        kind: server
        params: [{ name: product, type: ref:core.Product }]
        steps:
          - decision: { expr: "product.status == StockStatus.low" }
            then:
              - call_integration: { key: core.SlackWebhook, payload_expr: "{text: product.name + ' is low'}" }

    pages:
      - key: product-list
        path: /products
        layout: main
        type: list
        entity: Product
        columns: [name, sku, quantity, status]
        actions:
          - key: edit
            visible_expr: "$currentUser.hasRole('editor')"

security:                                         # P0
  roles: [viewer, editor, admin]
  access:
    - entity: core.Product
      role: editor
      operations: [read, create, update]
      row_constraint: "record.owner == $currentUser"
    - entity: core.Product
      role: admin
      operations: [read, create, update, delete]
  pages:
    product-list: [viewer, editor, admin]
```

## 8.4 Model Validation Rules

-   Reject duplicate entity names, field names, and page keys.
-   Verify that referenced entities and fields exist.
-   Check compatibility between types and constraints.
-   Detect duplicate routes.
-   Validate permission definitions.
-   Detect circular references and invalid dependencies.
-   Validate actions against an allowlist.
-   Detect undefined component or connector references.
-   Check model versions and available migration paths.
-   Warn about risky changes, including required-column additions, type
    changes, deletions, and permission expansion.
-   **(v1.1)** Type-check every expression against the model and reject
    side effects, unknown identifiers, and unbounded collection operations.
-   **(v1.1)** Reject computed-field dependency cycles and flow call cycles
    without explicit recursion limits.
-   **(v1.1)** Require at least one access rule per entity (deny-by-default
    is explicit, not accidental) and run the security consistency checker.
-   **(v1.1)** Verify cross-module references are declared as module
    dependencies.
-   **(v1.1)** Run the model linter; error-severity findings block approval
    unless suppressed with a recorded justification.


## 8.5 Expression Language (v1.1)

-   **Purpose:** single language for validation, computed fields,
    conditional UI, defaults, decisions in flows, assignment rules, and
    row-level access constraints.
-   **Properties:** statically typed, null-safe operators, pure (no I/O),
    deterministic, bounded (depth, collection size, evaluation time).
-   **Context variables:** `record`, `$currentUser`, `$now`, `$env.<constant>`
    (read-only), flow variables in flows only.
-   **Compilation targets:** (1) interpreter for runtime evaluation;
    (2) SQL predicate compiler for row-level constraints and list filters;
    (3) TypeScript evaluator for client-side visibility (never trusted for
    security).
-   **Decision record:** compare a CEL-compatible subset against a custom
    grammar in Phase 0 (ADR-EXPR-001), considering tooling, SQL compilation,
    and AI generation accuracy.

## 8.6 Flow and Process Specification (v1.1)

-   Flows are typed directed graphs of allowlisted step types with explicit
    inputs, outputs, and error handlers; loops require bounded iteration.
-   Server Flows run in one database transaction by default; steps that call
    external systems are flagged and must use the outbox or explicit
    compensation.
-   Client Flows can only call Server Flows, navigate, show messages, and
    validate; they never write data directly.
-   Processes are long-running and persist each transition; user tasks
    reference a page, an assignment expression, and an optional due date.
-   Every step emits trace spans with element IDs, enabling the debugger,
    OpenTelemetry, and AI failure analysis to point back to model elements.

## 8.7 Application Security Model (v1.1)

-   **Planes:** platform roles (Owner/Admin/Editor/Viewer) govern the Studio;
    app roles govern generated apps. They never inherit from each other.
-   **Evaluation order:** authenticate → resolve app roles → page/action
    access → entity operation access → row constraint (SQL predicate) →
    field-level filtering → audit.
-   **Union semantics:** when a user has several roles, access is the union
    of granted rules; constraints are OR-combined per operation.
-   **Defense in depth:** optional PostgreSQL row-level security mirrors the
    compiled predicates for direct database access paths (ADR).
-   **Testing:** each access rule generates positive and negative test cases
    automatically (Section 13).

## 8.8 Revisions, Branches, and Merge (v1.1)

-   `app_revisions` stores `parent_revision_ids` (0, 1, or 2 parents) from
    the MVP onward, so branching can be enabled without data migration.
-   Element identity is a stable `element_id` (UUID) independent of name;
    renames are tracked as property changes, not delete/create.
-   Merge is element-level three-way: non-overlapping property changes merge
    automatically; overlapping changes, deletions vs. edits, and broken
    references are conflicts requiring human resolution.
-   Merged results must pass validation and the linter before the merge
    revision is created.

## 8.9 Canonical File Format (v1.1)

-   One YAML file per element under `modules/<module>/<kind>/<name>.yaml`,
    plus `application.yaml` and `security/*.yaml`.
-   Stable key ordering and formatting guarantee minimal Git diffs and
    lossless round-trips between the database store and Git.
-   The file format is versioned together with `schema_version` and
    documented in `MODEL-SPEC.md`.

------------------------------------------------------------------------

# 9. Database Design --- Initial Logical Schema

Implement actual DDL through a migration tool. This section defines the
responsibility of each principal table.

  -----------------------------------------------------------------------
  Table                   Main Fields             Purpose
  ----------------------- ----------------------- -----------------------
  `users`                 `id`, `email`,          User accounts
                          `display_name`,         
                          `status`, `created_at`  

  `organizations`         `id`, `name`, `slug`,   Organizations
                          `created_at`            

  `memberships`           `organization_id`,      Organization membership
                          `user_id`, `role`       and roles

  `projects`              `id`,                   Projects
                          `organization_id`,      
                          `name`, `slug`,         
                          `settings`              

  `applications`          `id`, `project_id`,     Application metadata
                          `name`,                 
                          `current_revision_id`   

  `app_revisions`         `id`, `application_id`, Immutable model
                          `revision_no`,          revisions
                          `schema_version`,       
                          `model_json`,           
                          `created_by`,           
                          `created_at`            

  `ai_conversations`      `id`, `project_id`,     AI conversations
                          `created_by`, `title`,  
                          `status`                

  `ai_messages`           `id`,                   Conversation messages
                          `conversation_id`,      
                          `role`, `content`,      
                          `metadata`,             
                          `created_at`            

  `ai_jobs`               `id`, `project_id`,     AI and generation jobs
                          `type`, `status`,       
                          `input_ref`,            
                          `result_ref`,           
                          `provider`, `model`,    
                          `usage`, `started_at`,  
                          `finished_at`           

  `builds`                `id`, `revision_id`,    Build records
                          `status`,               
                          `artifact_ref`,         
                          `logs_ref`,             
                          `created_at`            

  `test_runs`             `id`, `build_id`,       Test results
                          `status`, `summary`,    
                          `report_ref`,           
                          `created_at`            

  `deployments`           `id`, `application_id`, Deployment history
                          `environment`,          
                          `status`,               
                          `artifact_ref`,         
                          `created_at`            

  `audit_events`          `id`,                   Audit trail
                          `organization_id`,      
                          `actor_id`, `action`,   
                          `target_type`,          
                          `target_id`,            
                          `metadata`,             
                          `created_at`            

  `api_tokens`            `id`, `owner_id`,       API tokens, if needed
                          `token_hash`, `scopes`, 
                          `expires_at`,           
                          `revoked_at`            
  -----------------------------------------------------------------------

## 9.1 Data Rules

-   Use UUIDs for primary keys by default.
-   Store timestamps in UTC and convert to the user's time zone for
    display.
-   Define case sensitivity for uniqueness, including email addresses.
-   Never store tokens or secrets in plaintext.
-   Enforce important foreign keys, unique constraints, and indexes in
    the database.
-   Store only schema-validated data in `model_json`.
-   Enforce organization/project boundaries in both queries and
    authorization logic.
-   Prevent ordinary application operations from altering or deleting
    audit history.
-   Make retention periods configurable for conversations, generated
    code, and logs.


## 9.2 Additional Tables (v1.1)

Changes to existing tables: `app_revisions` adds `branch_id`,
`parent_revision_ids` (array), and `model_hash`; `audit_events` adds
`plane` (`platform` | `application`) and `channel` (`studio` | `api` | `mcp`).

| Table | Main Fields | Purpose | Phase |
|---|---|---|---|
| `branches` | `id`, `application_id`, `name`, `head_revision_id`, `status` | Model branches (only `main` in MVP) | 2 |
| `proposals` | `id`, `ai_job_id`, `base_revision_id`, `change_set_json`, `approval_level`, `status`, `approved_by` | Unified proposals from AI Builder, MCP, and humans | 4 |
| `lint_findings` | `id`, `revision_id`, `rule_id`, `severity`, `element_id`, `suppressed`, `justification` | Linter results and suppressions | 3 |
| `element_refs` | `revision_id`, `from_element_id`, `to_element_id`, `ref_kind` | Dependency graph cache | 2 |
| `app_users` | `id`, `application_id`, `environment_id`, `external_subject`, `status` | Generated-app end users (application plane) | 3 |
| `app_user_roles` | `app_user_id`, `role_key` | End-user role assignment | 3 |
| `requirements`, `stories` | `id`, `project_id`, `title`, `body`, `status`, `source` | Requirements and story board | 8 |
| `trace_links` | `id`, `from_type`, `from_id`, `to_type`, `to_id` | Requirement ↔ element ↔ test traceability | 8 |
| `feedback_items` | `id`, `application_id`, `page_key`, `element_id`, `body`, `screenshot_ref`, `story_id` | Preview feedback | 8 |
| `scheduled_jobs_state` | `job_key`, `environment_id`, `last_run_at`, `next_run_at`, `lock_owner` | Scheduler state | 8 |
| `task_queue` | `id`, `queue`, `payload_ref`, `attempts`, `status`, `run_after` | Background flows | 8 |
| `outbox_events` | `id`, `type`, `payload`, `status`, `attempts`, `created_at` | Transactional outbox | 9 |
| `webhook_subscriptions` | `id`, `application_id`, `url`, `secret_ref`, `events`, `status` | Outbound webhooks | 9 |
| `mcp_tokens` | `id`, `owner_id`, `organization_id`, `project_id`, `scopes`, `token_hash`, `expires_at` | Studio MCP access | 9 |
| `mcp_servers` | `id`, `organization_id`, `url`, `auth_ref`, `tool_policies`, `status` | Allowlisted external MCP servers | 9 |
| `environments` | `id`, `application_id`, `name`, `stage_order`, `policy` | Delivery environments | 10 |
| `environment_constants` | `environment_id`, `key`, `value`, `secret_ref` | Constants and secret references | 10 |
| `deployment_packages` | `id`, `revision_id`, `runtime_version`, `migration_plan_ref`, `sbom_ref`, `signature`, `digest` | Immutable packages | 10 |
| `process_instances`, `user_tasks` | `id`, `process_key`, `version`, `state`, `assignee`, `due_at` | Human workflows | 11 |
| `ai_runs` | `id`, `application_id`, `step_or_agent_key`, `provider`, `tokens`, `cost`, `latency_ms`, `trace_id` | AI-in-app observability | 11 |
| `kb_documents`, `kb_chunks` | `id`, `kb_key`, `source_ref`, `embedding vector`, `acl_ref` | Knowledge bases | 11 |
| `policy_packs` | `id`, `organization_id`, `rules_json`, `enforcement` | Governance policies | 12 |

Application data of generated apps remains in per-application schemas (or
databases); platform metadata and application data never share tables.

------------------------------------------------------------------------

# 10. API Design

## 10.1 Common Rules

-   Base path: `/api/v1`
-   JSON is the default payload format.
-   Generate OpenAPI and check schema changes in CI.
-   Authorize access at the resource level, not just the route level.
-   Use a consistent error format; never expose internal stack traces or
    secrets.
-   Bound pagination, sorting, and filtering.
-   Run long operations as jobs and expose status through separate
    endpoints.
-   Use idempotency keys or confirmation steps for destructive
    operations.

## 10.2 Initial API Inventory

  ----------------------------------------------------------------------------------------------------
  Method                  Path                                                 Purpose
  ----------------------- ---------------------------------------------------- -----------------------
  POST                    `/auth/login`                                        Log in

  POST                    `/auth/logout`                                       Log out

  GET                     `/me`                                                Current user

  GET/POST                `/organizations`                                     List/create
                                                                               organizations

  GET/POST                `/projects`                                          List/create projects

  GET/POST                `/projects/{project_id}/applications`                List/create
                                                                               applications

  GET                     `/applications/{app_id}/revisions`                   List revisions

  POST                    `/applications/{app_id}/revisions`                   Create a revision

  GET                     `/revisions/{revision_id}/diff/{base_revision_id}`   Compare model revisions

  POST                    `/applications/{app_id}/ai-jobs`                     Start an AI job

  GET                     `/ai-jobs/{job_id}`                                  Get job state and
                                                                               result

  POST                    `/ai-jobs/{job_id}/approve`                          Approve a proposal

  POST                    `/ai-jobs/{job_id}/reject`                           Reject a proposal

  POST                    `/revisions/{revision_id}/validate`                  Validate a model

  POST                    `/revisions/{revision_id}/builds`                    Start a build

  GET                     `/builds/{build_id}`                                 Get build status

  POST                    `/builds/{build_id}/tests`                           Start tests

  GET                     `/test-runs/{test_run_id}`                           Get test results

  GET                     `/applications/{app_id}/preview`                     Preview information

  GET                     `/audit-events`                                      Query audit events
  ----------------------------------------------------------------------------------------------------

This inventory is provisional. Review resource hierarchy, authorization,
and naming consistency before implementation.


## 10.3 Additional API Inventory (v1.1)

| Method | Path | Purpose | Phase |
|---|---|---|---|
| GET | `/revisions/{revision_id}/elements/{element_id}/usages` | Find usages / impact | 2 |
| POST | `/revisions/{revision_id}/lint` | Run linter | 3 |
| POST | `/expressions/check` | Type-check an expression in a revision context | 2 |
| POST | `/applications/{app_id}/explain` | Explain element, page, flow, or diff (A0) | 4 |
| POST | `/applications/{app_id}/test-data` | Generate synthetic fixtures | 5 |
| GET/POST | `/proposals`, `/proposals/{id}/approve` | Unified proposal handling | 4 |
| GET/POST | `/applications/{app_id}/branches` | Branches | 10 |
| POST | `/branches/{id}/merge` | Three-way merge (returns conflicts) | 10 |
| POST | `/flows/{flow_key}/debug-sessions` | Preview-only flow debugging | 8 |
| GET/POST | `/projects/{id}/requirements`, `/stories`, `/trace-links` | Story board and traceability | 8 |
| POST | `/applications/{app_id}/feedback` | Preview feedback | 8 |
| POST | `/applications/{app_id}/integrations/openapi-import` | Consumed REST import | 9 |
| ANY | `/mcp` | Studio MCP Server endpoint | 9 |
| GET/POST | `/organizations/{id}/mcp-servers` | MCP client allowlist | 9 |
| GET/POST | `/applications/{app_id}/environments` | Environments | 10 |
| POST | `/revisions/{revision_id}/packages` | Build deployment package | 10 |
| POST | `/packages/{id}/promote` | Promote to next environment | 10 |
| GET | `/applications/{app_id}/tasks` | Task inbox (application plane) | 11 |
| GET | `/applications/{app_id}/ai-runs` | AI-in-app observability | 11 |
| GET/POST | `/organizations/{id}/policy-packs` | Governance policies | 12 |

------------------------------------------------------------------------

# 11. Web Studio UX Design

## 11.1 Primary Screens

1.  **Dashboard:** Recent projects, work status, AI jobs, and build
    status
2.  **Project Workspace:** Applications, models, history, environments,
    and settings
3.  **AI Builder:** Natural-language requirements, questions, plans,
    generation, and validation results
4.  **Model Explorer:** Entities, fields, relationships, and permissions
5.  **Page Builder:** Page tree, components, properties, and preview
6.  **Logic Builder:** Declarative actions, conditions, and events (P1)
7.  **Diff & Review:** Before/after changes, impact, warnings,
    approve/reject
8.  **Preview:** Execute the model-driven app with test data
9.  **Test Center:** Test cases, logs, failure causes, and repair
    proposals
10. **Deployments:** Environments, artifacts, and deployment history
    (P1)
11. **(v1.1) Security Editor:** app roles, access matrix, row
    constraints, consistency findings, "view as role" (P0 basic)
12. **(v1.1) Usages & Lint Panel:** dependency graph and linter findings
    beside every element (P0)
13. **(v1.1) Flow Builder & Debugger:** visual Server/Client Flows (P1)
14. **(v1.1) Process Builder & Task Inbox preview** (P1)
15. **(v1.1) Integration Center:** OpenAPI import, webhooks, events, MCP
    servers (P1)
16. **(v1.1) Branches & Merge:** branch list, merge conflicts, resolution
    (P1)
17. **(v1.1) Story Board:** requirements, stories, feedback, traceability
    (P1)
18. **(v1.1) Observability & AI Runs:** traces, logs, AI cost (P1)
19. **(v1.1) Governance Console:** inventory, policy packs, audit export
    (P2)

## 11.2 AI Builder Workflow

-   The user describes the application in natural language.
-   AI summarizes its understanding, open questions, user roles, and
    data fields.
-   AI asks a small number of concrete questions only when critical
    ambiguity remains.
-   AI proposes an implementation plan.
-   AI generates model changes and presents diffs, warnings, and
    validation results.
-   Approval creates a new model revision.
-   The system runs preview and tests, then proposes repairs if
    necessary.

## 11.3 UX Principles

-   Explain what changed, why it changed, and what was verified.
-   Visually distinguish destructive, permission-expanding, and
    external-data-transmission actions.
-   Make job status, cancellation, and retry easy to understand.
-   Distinguish AI-generated claims from facts verified by tools or
    tests.
-   Support responsive layouts and keyboard navigation.


## 11.4 Studio Experience Additions (v1.1)

-   **Explain everywhere:** every element, diff, and lint finding offers an
    "Explain" action with citations.
-   **Impact before approval:** Diff & Review shows the dependency-graph
    impact, security-rule changes, and linter deltas for each proposal.
-   **Templates first:** the Page Builder and the AI UI Designer start from
    approved templates and building blocks, reducing random layouts.
-   **Japanese-first experience:** Studio UI, AI Builder prompts, and
    generated apps support ja-JP and en-US from the MVP.

------------------------------------------------------------------------

# 12. Security Design

## 12.1 Threat Model

Principal threats include cross-tenant data exposure, prompt injection,
unsafe generated-code execution, privilege escalation, secret leakage,
compromised dependencies, worker escape, and audit-log tampering.

## 12.2 Required Controls

-   Enforce authentication and server-side RBAC on all APIs.
-   Never trust an organization or project ID supplied by the client
    without checking membership.
-   For cookie-based authentication, implement CSRF protection and
    secure session settings, including `Secure`, `HttpOnly`, and
    appropriate `SameSite` attributes.
-   Hash API tokens and manage scopes, expiration, and revocation.
-   Minimize context sent to LLM providers and remove secrets.
-   Never execute generated code inside the API process.
-   Run build/test workers as non-root with restricted filesystems and
    network access.
-   Do not treat containers as the only security boundary; continuously
    evaluate execution isolation and permissions.
-   Integrate dependency scanning, secret scanning, and SBOM generation
    into CI.
-   Record critical actions in audit logs and control log access and
    retention.
-   Separate permissions for data deletion, export, and backup
    restoration.
-   Publish a security reporting channel and vulnerability-handling
    process.

## 12.3 Approval Levels

  -----------------------------------------------------------------------
  Level                   Examples                Initial Policy
  ----------------------- ----------------------- -----------------------
  A0                      Read, explain,          May run automatically
                          summarize               

  A1                      Non-destructive model   Generation allowed;
                          proposals and test      application requires
                          proposals               approval

  A2                      Apply model changes,    Explicit approval
                          add dependencies,       
                          create migrations       

  A3                      Send data externally,   Strong confirmation and
                          change permissions,     audit
                          delete data             

  A4                      Production deployment,  Explicit approval plus
                          secret changes,         environment policy
                          destructive operations  
  -----------------------------------------------------------------------


## 12.4 Application-Plane Security (v1.1)

-   Generated apps have their own identity store or OIDC integration,
    separate from Orchivio Studio accounts.
-   Row-level constraints are enforced by the runtime data layer on every
    query, including counts, aggregates, exports, OData, MCP tools, and
    knowledge-base retrieval.
-   Anonymous access, permission expansion, and elevated (system-context)
    flows require A3 approval and are highlighted in diffs.
-   Client-side expressions are presentation-only; the server re-evaluates
    every authorization decision.

## 12.5 Additional Threats and Controls (v1.1)

| Threat | Control |
|---|---|
| Row-level rule bypass through new query paths | Single data-access layer; generated negative tests per rule; linter checks for unguarded paths |
| MCP tool abuse by external agents | Scoped expiring tokens, proposal-only writes, rate limits, full audit |
| Prompt injection through MCP/tool results or knowledge bases | Results treated as data; guardrail filtering; no tool output can change approval state |
| SSRF via consumed REST and webhooks | Per-environment allowlists, private-network blocking, DNS rebinding protection |
| Secret leakage via environment constants | Secret references only; secrets resolved inside the runtime/worker, never returned by APIs |
| Malicious modules/components | Signed packages, review policy, sandboxed rendering and isolated execution |
| AI-in-app cost exhaustion | Hard budgets and per-user rate limits |

**Approval levels (additions):** MCP `proposal.apply`, merges into `main`,
and environment promotion are A2; anonymous access, MCP server
registration, and system-context flows are A3; production promotion and
secret changes remain A4.

------------------------------------------------------------------------

# 13. Testing and Quality Assurance

## 13.1 Test Layers

-   **Unit:** Schema validation, RBAC, model diffs, migration planning,
    prompt construction
-   **Integration:** API, database, queue, LLM adapters, artifact
    storage
-   **Contract:** API schemas, LLM output schemas, plugin interfaces
-   **E2E:** Natural-language request through model generation,
    approval, preview, CRUD, and tests
-   **Security:** Tenant boundaries, privilege escalation, prompt
    injection, secrets, worker isolation
-   **AI evaluation:** Accuracy, requirement coverage, repair success,
    unsafe-change rate, cost, and latency
-   **Performance:** CRUD APIs, model validation, queue processing,
    concurrent jobs
-   **Migration:** Upgrade old model schemas to newer versions
-   **(v1.1) Expression:** grammar fuzzing, type-checker coverage, SQL
    compilation equivalence (interpreter vs. SQL results)
-   **(v1.1) App security:** auto-generated positive/negative tests per
    access rule; "view as role" E2E
-   **(v1.1) Flow & process:** flow unit tests, timer and retry tests,
    idempotency of process steps
-   **(v1.1) Merge:** property-based tests for deterministic, lossless merge
    and canonical-file round-trips
-   **(v1.1) MCP:** contract tests for Studio MCP tools and approval
    enforcement
-   **(v1.1) Model coverage:** share of entities, pages, flows, and access
    rules exercised by tests

## 13.2 AI Quality Metrics

  -----------------------------------------------------------------------
  Metric                              Definition
  ----------------------------------- -----------------------------------
  Requirement Coverage                Share of requirements covered by
                                      generated models and tests

  Schema Validity                     Rate of generated models that pass
                                      schema validation

  First-Pass Build Success            Share of initial generations that
                                      build successfully

  Test Pass Rate                      Share of generated tests that pass

  Human Rework Rate                   Share of proposals requiring
                                      substantial human changes

  Unsafe Proposal Rate                Share of proposals rejected by
                                      policy

  Cost per Successful Build           AI cost per successful build

  Median / p95 Latency                Median and p95 AI-job duration
  -----------------------------------------------------------------------

Keep evaluation conditions and datasets versioned. Run regression
evaluations when models or prompts change. Do not use AI evaluation
scores alone as release criteria; security and functional tests remain
mandatory.

## 13.3 MVP Exit Criteria

-   The project starts from a clean environment using documented
    instructions.
-   A sample inventory app can be generated from natural language.
-   The generated model passes schema validation.
-   Users can review diffs and apply changes only after approval.
-   Basic list, create, edit, and detail CRUD flows work.
-   Server-side validation and authorization are enforced.
-   Generated tests run and results appear in the UI.
-   Job history and audit events are inspectable.
-   Cross-organization access tests pass.
-   CI passes linting, type checks, unit tests, and integration tests.
-   Setup, backup, known limitations, and threat model are documented.
-   **(v1.1)** Row-level access rules are enforced and their generated
    negative tests pass.
-   **(v1.1)** Expressions drive computed fields and conditional UI in the
    sample app.
-   **(v1.1)** The linter baseline (≥20 rules) and find-usages work in the
    Studio.
-   **(v1.1)** Explain mode and synthetic test data work for the sample app.
-   **(v1.1)** Revisions store parent IDs (branch-ready) and pass migration
    tests.

------------------------------------------------------------------------

# 14. Proposed Repository Structure

``` text
orchivio/
├── apps/
│   ├── web/                     # React + TypeScript Studio
│   ├── api/                     # FastAPI entry point, routes, Studio MCP server
│   ├── runtime/                 # Generated application runtime
│   └── worker/                  # AI/build/test/extension workers
├── packages/
│   ├── app-model/               # Model schemas, validation, migrations, canonical files
│   ├── expression-engine/       # (v1.1) Parser, type checker, interpreter, SQL compiler
│   ├── app-security/            # (v1.1) App roles, row/field rules, consistency checker
│   ├── model-linter/            # (v1.1) Rule catalog and dependency graph
│   ├── logic-engine/            # (v1.1, R0.2) Server/Client Flow interpreter
│   ├── process-engine/          # (v1.1, R0.5) Long-running processes and user tasks
│   ├── integration-gateway/     # (v1.1, R0.3) REST import, webhooks, outbox
│   ├── merge/                   # (v1.1, R0.4) Three-way semantic merge
│   ├── delivery/                # (v1.1, R0.4) Environments, packages, migration plans
│   ├── ai-orchestrator/         # Planning, tool policy, agent loop, MCP client
│   ├── ai-runtime/              # (v1.1, R0.5) AI steps, agents, guardrails, KB
│   ├── llm-adapters/            # Hosted/local/mock providers
│   ├── ui-renderer/             # Model-driven page renderer, design tokens
│   ├── codegen/                 # OpenAPI, clients, and artifact generators
│   ├── policy-engine/           # Approval levels, platform authorization, policy packs
│   └── shared/                  # Shared types and utilities
├── migrations/
├── templates/
│   ├── starter-crud/
│   └── building-blocks/         # (v1.1) Approved page templates
├── examples/
│   └── inventory-app/
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── e2e/
│   ├── security/
│   ├── merge/                   # (v1.1) Property-based merge tests
│   └── evals/
├── docs/
│   ├── architecture/
│   ├── adr/
│   ├── security/
│   ├── benchmark/               # (v1.1) Competitive gap analysis (internal)
│   ├── user-guide/
│   └── developer-guide/
├── scripts/
├── docker/
├── .github/workflows/
├── docker-compose.yml
├── .env.example
├── LICENSE
├── CONTRIBUTING.md
├── SECURITY.md
├── CODE_OF_CONDUCT.md
└── README.md
```

Use a monorepo initially. Confirm the language and package-management
strategy after a proof of concept, and enforce package dependency
boundaries in CI.

------------------------------------------------------------------------

# 15. Development Roadmap

## 15.1 Planning Assumptions

-   **MVP (Release 0.1):** approximately **22 weeks** (v1.0: 20 weeks). The
    two additional weeks cover the expression language, the application
    security model, module/branch-ready schema, dependency graph, and linter
    baseline---capabilities that are expensive to retrofit later.
-   **Post-MVP to Release 1.0:** Phases 8--12, approximately 30 weeks
    (weeks 23--52), each ending in a usable release.
-   **Suggested team:** one tech lead, one backend engineer, one frontend
    engineer, one AI engineer, and QA/platform support either full-time or
    part-time
-   **For a solo developer:** retain the phase order and scope gates, extend
    the schedule, and complete P0 before any P1 work; use AI coding agents
    under the same review rules (Section 20)
-   **Sprint cadence:** two weeks
-   **Planning note:** estimates are provisional and should be revised
    after proof-of-concept results and evaluation; re-run the benchmark
    review at each release boundary

## 15.2 Phases

| Phase | Weeks | Main Deliverables | Exit Criteria |
|---|---|---|---|
| 0. Product definition and technical validation | 1--2 | Requirements, ADRs (incl. expression language, app security, tenancy), UI prototype, LLM comparison, benchmark validation | Key risks evaluated and MVP scope frozen |
| 1. Foundation | 3--4 | Repository, CI, platform authentication, organizations, projects | Startup, authentication, basic CRUD, and CI work |
| 2. Application model | 5--7 | Model schemas incl. modules, enums, computed fields, indexes; expression engine v1; revisions with parent IDs; diffs; dependency graph | Models can be saved, validated, type-checked, diffed, and rolled back |
| 3. CRUD runtime and app security | 8--10 | Entity APIs, UI renderer, layouts, conditional UI, preview, app roles, row-level rules, consistency checker, linter baseline | Sample app CRUD works with enforced row-level security |
| 4. AI Builder | 11--13 | Requirements analysis, plans, security-aware proposals, unified proposals, approval flow, Explain mode | Natural language produces reviewable, lint-clean proposals |
| 5. Test and repair loop | 14--16 | Test generation incl. access-rule tests, synthetic test data, execution, failure analysis, repair proposals, model coverage | Test results feed an improvement loop |
| 6. Security and operations | 17--19 | Isolated workers, platform/app-plane audit, backups, tenant and row-level security tests | Security criteria and restore tests pass |
| 7. MVP release preparation | 20--22 | Documentation, examples, ja/en tutorial, release candidate | E2E, quality gates, and reproducible distribution pass |
| 8. Logic and automation (R0.2) | 23--28 | Server/Client Flows, flow builder, debugger, flow tests, event hooks, scheduled jobs, task queue, story board and feedback widget | A low-stock notification flow runs, is debuggable, and is covered by tests |
| 9. Integration and agent interop (R0.3) | 29--34 | OpenAPI import, webhooks, outbox, CSV/Excel import, Studio MCP Server, MCP client allowlist | An external agent can propose and test a change via MCP; it applies only after approval |
| 10. Collaboration and delivery (R0.4) | 35--40 | Branches, three-way merge, canonical Git files, environments, signed packages, promotion, OIDC for apps, OTel, version channels | Two branches merge with conflict resolution; a package is promoted dev→test→prod with approvals |
| 11. Processes and AI in apps (R0.5) | 41--46 | Process engine, task inbox, AI steps, guardrails, AI observability, per-app AI policy, PWA | An approval process with an AI classification step runs end-to-end with cost tracking |
| 12. Ecosystem, governance, hardening (R1.0) | 47--52 | Module registry, component SDK (preview), governance console, policy packs, upgrade assistant, field-level security, accessibility lint, external entities (preview) | Release 1.0 quality gates, security review, and upgrade from 0.1 pass |

## 15.3 Detailed Tasks

### Phase 0 --- Product Definition and Technical Validation (Weeks 1--2)

-   [ ] Finalize user stories and MVP acceptance criteria
-   [ ] Decide the boundary between model-driven runtime and code
    generation
-   [ ] Build a proof of concept for LLM provider abstraction
-   [ ] Compare structured-output schema conformance
-   [ ] Prototype key React Studio screens
-   [ ] Draft JSON Schema/Pydantic models
-   [ ] Record ADRs for language, database, queue, LLM, and license
-   [ ] Create a risk register and security threat model
-   [ ] **(v1.1)** ADR-EXPR-001: expression language (CEL subset vs. custom);
    prototype SQL predicate compilation
-   [ ] **(v1.1)** ADR-SEC-002: two-plane security and row-level enforcement
    (runtime predicates ± PostgreSQL RLS)
-   [ ] **(v1.1)** ADR-TEN-003: application data isolation (schema per app
    vs. database per app)
-   [ ] **(v1.1)** Validate the Mendix gap analysis (Appendix A) against
    target-user interviews; confirm P0/P1 cut

### Phase 1 --- Foundation (Weeks 3--4)

-   [ ] Create the monorepo, development environment, and CI
-   [ ] Add configuration management, structured logs, and correlation
    IDs
-   [ ] Add PostgreSQL and migrations
-   [ ] Implement user, organization, and project APIs
-   [ ] Implement a common RBAC policy layer (platform plane)
-   [ ] Implement login, dashboard, and project workspace
-   [ ] Standardize error formats, pagination, and OpenAPI
-   [ ] Create test data and development seed scripts
-   [ ] **(v1.1)** Separate audit streams for platform and application planes

### Phase 2 --- Application Model (Weeks 5--7)

-   [ ] Define Entity/Field/Relation schemas
-   [ ] Define Page/Component/Route schemas
-   [ ] Define Permission/Validation schemas
-   [ ] Validate with JSON Schema and Pydantic
-   [ ] Implement immutable revisions and revision numbers
-   [ ] Implement diffs using JSON Pointer or an equivalent approach
-   [ ] Implement apply, reject, and rollback operations
-   [ ] Define model migration and compatibility tests
-   [ ] Display model validation errors in the UI
-   [ ] **(v1.1)** Add modules, enumerations, computed fields, and indexes
-   [ ] **(v1.1)** Implement expression parser, type checker, and
    interpreter (v1)
-   [ ] **(v1.1)** Store `parent_revision_ids` and stable `element_id`s
-   [ ] **(v1.1)** Build the dependency graph and find-usages API

### Phase 3 --- CRUD Runtime and App Security (Weeks 8--10)

-   [ ] Resolve API routes and CRUD operations from models
-   [ ] Implement data access through authorization, validation, and
    auditing
-   [ ] Render model-driven forms, lists, and detail pages
-   [ ] Implement page routing and navigation
-   [ ] Isolate preview/test data
-   [ ] Create the sample inventory app
-   [ ] Add API contract tests and Playwright E2E tests
-   [ ] Implement initial data export/import
-   [ ] **(v1.1)** Implement app roles, page/action access, and row-level
    constraints compiled to SQL
-   [ ] **(v1.1)** Implement local end-user authentication for generated apps
-   [ ] **(v1.1)** Implement layouts, conditional visibility/editability, and
    basic data grid
-   [ ] **(v1.1)** Implement security consistency checker and linter baseline
    (≥20 rules)

### Phase 4 --- AI Builder (Weeks 11--13)

-   [ ] Define the LLM adapter interface
-   [ ] Implement a mock provider and one real provider
-   [ ] Version prompt templates
-   [ ] Implement requirements analysis and missing-information
    detection
-   [ ] Generate structured implementation plans
-   [ ] Generate proposed model changes
-   [ ] Validate output schemas and retry invalid outputs safely
-   [ ] Display diffs, impact, and warnings
-   [ ] Apply model changes only after approval
-   [ ] Record token usage, cost, and latency
-   [ ] Implement timeouts, rate limits, and cancellation
-   [ ] **(v1.1)** Generate app roles and access rules together with entities
-   [ ] **(v1.1)** Implement unified `proposals` handling (shared later by MCP)
-   [ ] **(v1.1)** Implement Explain mode with element-ID citations
-   [ ] **(v1.1)** Show dependency impact, security changes, and lint deltas in
    Diff & Review

### Phase 5 --- Test and Repair Loop (Weeks 14--16)

-   [ ] Propose test cases from requirements
-   [ ] Generate CRUD API tests
-   [ ] Implement isolated build/test workers
-   [ ] Store job status, logs, and artifacts
-   [ ] Classify failures and identify likely causes
-   [ ] Generate repair proposals linked to failures
-   [ ] Compare test results before and after repairs
-   [ ] Build a versioned AI evaluation dataset and regression suite
-   [ ] Create an initial AI quality dashboard
-   [ ] **(v1.1)** Generate positive/negative tests per access rule
-   [ ] **(v1.1)** Implement constraint-aware synthetic test data
-   [ ] **(v1.1)** Generate stable test IDs from element keys; report model
    coverage

### Phase 6 --- Security and Operations (Weeks 17--19)

-   [ ] Run workers as non-root with resource limits
-   [ ] Restrict network and filesystem access
-   [ ] Add secret scanning, dependency scanning, and SBOM generation
-   [ ] Protect and query audit logs
-   [ ] Automate backup/restore tests
-   [ ] Test tenant isolation and privilege escalation
-   [ ] Define retention, deletion, and export policies
-   [ ] Conduct a threat-model-based security review
-   [ ] **(v1.1)** Test row-level bypass attempts across all query paths
-   [ ] **(v1.1)** Fuzz the expression engine and verify interpreter/SQL
    equivalence

### Phase 7 --- MVP Release Preparation (Weeks 20--22)

-   [ ] Validate installation from a clean environment
-   [ ] Run the sample app E2E scenario
-   [ ] Document APIs and the application model format
-   [ ] Document administrator configuration and backup procedures
-   [ ] Write the user tutorial (ja/en)
-   [ ] Review OSS licenses, third-party notices, and SBOM
-   [ ] Fix bugs, measure performance, and publish known limitations
-   [ ] Tag a release candidate and verify reproducible builds
-   [ ] **(v1.1)** Publish `EXPRESSION-SPEC.md` and `APP-SECURITY.md`

### Phase 8 --- Logic and Automation, Release 0.2 (Weeks 23--28)

-   [ ] Define the Flow schema and allowlisted step types
-   [ ] Implement the Logic Engine (Server Flows, transactions, error
    handlers)
-   [ ] Implement Client Flows and the visual Flow Builder
-   [ ] Implement preview-only flow debugger with trace spans
-   [ ] Implement entity event hooks, scheduled jobs, and task queue
-   [ ] Implement declarative flow unit tests
-   [ ] AI Flow Designer role and flow-aware repair proposals
-   [ ] Implement story board, traceability links, and preview feedback
    widget
-   [ ] Add linter rules for flows (unbounded loops, commits in loops,
    missing error handling)

### Phase 9 --- Integration and Agent Interop, Release 0.3 (Weeks 29--34)

-   [ ] Implement OpenAPI import and typed integration definitions
-   [ ] Implement inbound/outbound webhooks with signing and SSRF protection
-   [ ] Implement transactional outbox for business events
-   [ ] Implement CSV/Excel import with mapping and per-row errors
-   [ ] Implement the Studio MCP Server tool set and scoped tokens
-   [ ] Implement MCP client allowlist and per-tool approval levels
-   [ ] Add MCP contract tests and prompt-injection tests for tool results
-   [ ] Publish `MCP.md` with Claude Code and other MCP client setup guides

### Phase 10 --- Collaboration and Delivery, Release 0.4 (Weeks 35--40)

-   [ ] Enable branches and change sets in the Studio
-   [ ] Implement three-way semantic merge and conflict UI
-   [ ] Implement the canonical file format and Git synchronization/PRs
-   [ ] Implement environments, constants, and secret references
-   [ ] Build signed, reproducible deployment packages with migration plans
-   [ ] Implement promotion pipeline with approval gates
-   [ ] Implement OIDC SSO for generated apps
-   [ ] Add OpenTelemetry instrumentation and log viewer
-   [ ] Define runtime version channels (LTS/rolling)

### Phase 11 --- Processes and AI in Apps, Release 0.5 (Weeks 41--46)

-   [ ] Implement the Process Engine, user tasks, timers, and task inbox
-   [ ] Implement the Process Builder
-   [ ] Implement AI steps in flows with structured output
-   [ ] Implement guardrail pipeline and per-app AI provider/budget policy
-   [ ] Implement AI observability (`ai_runs`, traces, cost)
-   [ ] Implement PWA packaging
-   [ ] AI-assisted log/trace analysis linked to revisions

### Phase 12 --- Ecosystem, Governance, and Hardening, Release 1.0 (Weeks 47--52)

-   [ ] Implement the self-hostable module registry with signing
-   [ ] Publish component SDK preview with sandboxed rendering
-   [ ] Implement governance console and policy packs
-   [ ] Implement upgrade assistant (model migrations and deprecations)
-   [ ] Implement field-level security, accessibility lint, design tokens
    and AI theming
-   [ ] Implement fragments, page templates/building blocks, multi-module
    UI, transient entities, file attributes, and optimistic locking
-   [ ] External entities and agents (preview), knowledge bases (preview)
-   [ ] Third-party security review; upgrade test from Release 0.1
-   [ ] Tag Release 1.0

## 15.4 Milestones

-   **M1 (Week 2):** Technical decisions and MVP scope finalized
-   **M2 (Week 4):** Login, projects, and CI operational
-   **M3 (Week 7):** Model editing, expressions, validation, revisions, and
    usages operational
-   **M4 (Week 10):** CRUD app runs from a manually defined model with
    row-level security
-   **M5 (Week 13):** Natural language generates an approvable,
    security-aware, lint-clean proposal
-   **M6 (Week 16):** Test execution and repair-proposal loop operational
-   **M7 (Week 19):** Security and backup criteria met
-   **M8 (Week 22):** MVP Release 0.1 candidate
-   **M9 (Week 28):** Release 0.2 --- logic and automation
-   **M10 (Week 34):** Release 0.3 --- integrations and MCP interop
-   **M11 (Week 40):** Release 0.4 --- branching, merge, and delivery
-   **M12 (Week 46):** Release 0.5 --- processes and AI in apps
-   **M13 (Week 52):** Release 1.0

------------------------------------------------------------------------

# 16. Backlog and Priorities

## P0 --- MVP Required

-   Authentication, organizations, projects, and RBAC
-   Application model, schema validation, and revisions
-   Basic Entity and Page models
-   CRUD runtime
-   AI requirements analysis and model proposals
-   Diff, approval, rejection, and rollback
-   Preview
-   Basic test generation and execution
-   Audit logs
-   Docker Compose, README, and sample app
-   **(v1.1)** Expression language; computed fields; conditional UI
-   **(v1.1)** Modules, enumerations, indexes; branch-ready revisions
-   **(v1.1)** Two-plane security; app roles; row-level constraints; local
    end-user auth; consistency checker
-   **(v1.1)** Dependency graph; linter baseline; Explain mode; synthetic
    test data; access-rule tests

## P1 --- Early Post-MVP

-   Visual page builder, fragments, templates, design tokens
-   Server/Client Flows, debugger, event hooks, scheduled jobs, task queue
-   Basic human workflows and task inbox
-   Git integration, branches, semantic merge, canonical files
-   Expanded code export and generators
-   Better UX for multiple hosted and local LLM providers
-   AI budget controls and usage reports
-   OpenAPI import, webhooks, outbox, CSV/Excel import
-   Studio MCP Server and MCP client allowlist
-   Environments, signed packages, promotion, OIDC for generated apps
-   OpenTelemetry, AI log analysis, version channels, upgrade assistant
-   AI steps in flows, guardrails, AI observability
-   Story board, traceability, preview feedback widget
-   Field-level security, accessibility lint, app-level i18n, PWA
-   Team collaboration

## P2 --- Long-Term Direction

-   Multi-agent collaborative engineering
-   Module registry, plugin/template catalog, component SDK
-   Advanced workflow and event processing (boundary/notification events,
    instance migration, monitoring)
-   Agents, knowledge bases, and generated apps as MCP servers
-   External entities, OData publishing, broker adapters
-   Near-zero-downtime (expand/contract) deployments
-   Enterprise SSO (SAML) and SCIM; governance console and policy packs
-   Kubernetes and multi-cloud operations
-   Advanced monitoring and operations-informed improvements
-   Offline-first applications; entity inheritance
-   Marketplace and developer ecosystem

------------------------------------------------------------------------

# 17. Risks and Mitigations

  -----------------------------------------------------------------------
  Risk                    Impact                  Mitigation
  ----------------------- ----------------------- -----------------------
  Inaccurate AI           Rework and defects      Structured output,
  generation                                      schema validation,
                                                  tests, diff approval

  Excessive scope         Delayed release         Restrict MVP to CRUD
                                                  and model generation

  LLM vendor dependency   Cost and migration      Provider adapters,
                          difficulty              evaluation benchmarks,
                                                  local-model support

  Unsafe generated-code   Host compromise         Isolated workers,
  execution                                       non-root execution,
                                                  resource limits,
                                                  approval gates

  Frequent model-schema   Compatibility issues    `schema_version`,
  changes                                         migration tools,
                                                  compatibility tests

  Drift between UI and    Inconsistency           Treat model as source
  model                                           of truth and track
                                                  generated artifacts

  Cross-tenant leakage    Serious data incident   Server-side
                                                  authorization, boundary
                                                  tests, auditing

  OSS maintenance burden  Reduced sustainability  Small core, clear
                                                  contribution process,
                                                  CI

  Poor performance        Degraded UX             Async LLM jobs,
                                                  measurement, caching,
                                                  load tests

  Trademark or brand      Costly rebranding       Trademark clearance and
  conflict                                        expert review before
                                                  public launch

  License incompatibility Distribution            Dependency license
                          constraints             review, SBOM, NOTICE,
                                                  legal review
  -----------------------------------------------------------------------


**Additional risks (v1.1):**

| Risk | Impact | Mitigation |
|---|---|---|
| Parity chasing against mature commercial platforms | Scope explosion, late MVP | Positioning rule (Section 2.4); benchmark items enter only through P0/P1 review |
| Expression language complexity | Delays, AI generation errors | Small grammar, CEL-compatible option, AI eval set for expressions |
| Row-level rules degrade performance | Slow lists and reports | Index linting, query plans in CI, caching of role resolution |
| Merge semantics bugs | Lost or corrupted models | Stable element IDs, property-based tests, validation before merge commit |
| MCP becomes an unaudited write path | Governance failure | Proposal-only writes, approval enforcement in the Model Service, audit |
| AI-in-app cost and reliability | Customer cost overruns | Budgets, local-model option, graceful degradation |
| Comparative claims become outdated or inaccurate | Credibility/legal exposure | Re-verify benchmark each release; keep comparisons internal unless reviewed |

------------------------------------------------------------------------

# 18. Open Source, Licensing, and Brand

## 18.1 Open-Source Release Principles

-   Choose a license based on adoption goals and the intended conditions
    for derivative use.
-   Compare permissive licenses such as MIT/Apache-2.0 with copyleft
    alternatives before deciding.
-   Review licenses separately for the core, plugins, generated
    artifacts, and examples.
-   Maintain `README`, `CONTRIBUTING`, `CODE_OF_CONDUCT`, `SECURITY`,
    `NOTICE`, and an SBOM.
-   Do not copy third-party logos, images, text, or UI assets without
    permission.
-   Even when learning from existing product categories, create original
    code, copy, branding, and design assets.

## 18.2 Orchivio Brand

This document uses **Orchivio** as the product name. This choice does
not guarantee trademark registrability, domain availability, or
non-infringement of third-party rights. Before public launch, commercial
use, or filing an application, search the Japan Platform for Patent
Information (J-PlatPat), WIPO Global Brand Database, relevant national
trademark databases, company/product names, domains, and software
distributions. Seek professional advice where appropriate.

------------------------------------------------------------------------

# 19. Documentation Plan

  ---------------------------------------------------------------------------
  Document                    Content                 Timing
  --------------------------- ----------------------- -----------------------
  `README.md`                 Overview, setup, demo,  Start in Phase 1
                              feature status          

  `PRODUCT-REQUIREMENTS.md`   Requirements, user      Phase 0
                              stories, acceptance     
                              criteria                

  `ARCHITECTURE.md`           Components, boundaries, Phase 0--1
                              and data flows          

  `MODEL-SPEC.md`             Application model       Phase 2
                              schema and examples     

  `API-SPEC.md`               API conventions and     From Phase 1
                              OpenAPI                 

  `SECURITY.md`               Vulnerability reporting Phase 1
                              and security policy     

  `THREAT-MODEL.md`           Threats, trust          Phase 0; update in
                              boundaries, mitigations Phase 6

  `docs/adr/`                 Architecture decision   For each significant
                              records                 decision

  `CONTRIBUTING.md`           Development             Phase 1
                              environment, PRs, and   
                              tests                   

  `USER-GUIDE.md`             Studio, AI Builder, and Phase 7
                              preview workflows       

  `RELEASE.md`                Releases,               Before MVP
                              compatibility, and      
                              changelog               
  ---------------------------------------------------------------------------


**Additional documents (v1.1):**

| Document | Content | Timing |
|---|---|---|
| `EXPRESSION-SPEC.md` | Grammar, types, context variables, limits | Phase 2 |
| `APP-SECURITY.md` | Two-plane model, access rules, evaluation order | Phase 3 |
| `LINT-RULES.md` | Rule catalog, severities, suppression policy | Phase 3 |
| `FLOW-SPEC.md` | Flow and process specification | Phase 8 |
| `MCP.md` | Studio MCP tools, tokens, client setup | Phase 9 |
| `MERGE.md` | Branch/merge semantics and canonical file format | Phase 10 |
| `DELIVERY.md` | Environments, packages, promotion, version channels | Phase 10 |
| `docs/benchmark/` | Internal competitive gap analysis (re-verified per release) | Each release |

------------------------------------------------------------------------

# 20. Development Governance

## 20.1 Development Rules

-   Every issue must include purpose, acceptance criteria, test
    strategy, and impact scope.
-   Update requirements or ADRs before implementing specification
    changes.
-   AI-generated code follows the same code review, testing, and license
    checks as human-written code.
-   Avoid direct pushes to the main branch; require pull requests and
    CI.
-   Give special review to changes involving migrations, authorization,
    secrets, and worker execution boundaries.
-   Define compatibility policies for model formats, APIs, and plugin
    interfaces.
-   Clearly distinguish implemented, experimental, and planned features.

## 20.2 Definition of Done

A feature is complete only when all applicable conditions are met:

1.  Requirements and acceptance criteria are satisfied.
2.  Type checks, linting, and unit tests pass.
3.  Required integration and E2E tests are added.
4.  Permissions, errors, and boundary conditions are tested.
5.  Logging, auditing, and operational concerns are addressed.
6.  User and developer documentation is updated.
7.  Required ADRs, migrations, and license records are updated.
8.  Known limitations and remaining work are documented.

------------------------------------------------------------------------

# 21. First Ten Working Days

## Days 1--2

-   [ ] Create the Git repository, branch protection, and issue
    templates
-   [ ] Draft `README.md`, a candidate `LICENSE`, and `CONTRIBUTING.md`
-   [ ] Validate Python/TypeScript toolchains and CI
-   [ ] Create a minimal Docker Compose setup

## Days 3--4

-   [ ] Define the minimum application-model JSON Schema
-   [ ] Implement validation with Pydantic
-   [ ] Implement model persistence and versioning
-   [ ] Create the inventory-app model example
-   [ ] **(v1.1)** Include `parent_revision_ids`, `element_id`, modules, and
    enumerations in the minimum schema
-   [ ] **(v1.1)** Spike the expression parser/type checker on validation and
    computed-field examples

## Days 5--6

-   [ ] Implement the LLM provider interface and a mock provider
-   [ ] Prototype structured JSON generation from natural-language
    requirements
-   [ ] Test invalid JSON, missing fields, and timeouts
-   [ ] Prototype diff presentation without modifying the canonical
    model

## Days 7--8

-   [ ] Implement minimal Entity CRUD APIs
-   [ ] Render model-driven list and form pages
-   [ ] Enforce server-side validation and authorization
-   [ ] **(v1.1)** Prototype one row-level constraint compiled to a SQL
    predicate, with a negative test
-   [ ] Add API/UI integration tests

## Days 9--10

-   [ ] Implement approval-based model application and revision creation
-   [ ] Add basic Playwright E2E tests
-   [ ] Review proof-of-concept results, technical debt, and open
    decisions
-   [ ] Update estimates for Phases 1--2

**Ten-day objective:** not a finished product, but a vertical proof of
concept where a model is defined, AI proposes changes, a human approves
them, and a CRUD application can be previewed.

------------------------------------------------------------------------

# 22. Open Decisions

Resolve the following during Phase 0 and record each decision in an ADR:

1.  Initial LLM providers and scope of local-model support
2.  Boundary between model-driven runtime and code generation
3.  Authentication approach and timing for OIDC support
4.  Initial job-queue implementation and replacement strategy
5.  Tenant data-isolation approach, such as shared database schemas or
    row-level controls
6.  Open-source license
7.  Compatibility policy for generated code and model versions
8.  Plugin execution model, signing, and review policy
9.  Opt-in/opt-out policy for telemetry
10. Trademark, domain, and brand clearance
11. **(v1.1)** Expression language: CEL-compatible subset vs. custom grammar
12. **(v1.1)** Row-level enforcement: runtime predicates only, or plus
    PostgreSQL RLS
13. **(v1.1)** Application data isolation: schema per app vs. database per
    app
14. **(v1.1)** Flow representation and editor library for the Flow Builder
15. **(v1.1)** Merge algorithm and conflict granularity (element vs.
    property)
16. **(v1.1)** Studio MCP transport, token model, and tool versioning
17. **(v1.1)** Process engine: in-house vs. embedding an existing open-source
    workflow engine
18. **(v1.1)** Whether OData publishing is needed or OpenAPI suffices for BI
19. **(v1.1)** Runtime version channel cadence (LTS length)

------------------------------------------------------------------------

# 23. Final Recommendation

Orchivio's competitive advantage should not be the appearance of its AI
chat interface. It should be the closed loop that transforms
requirements into models, applies validated changes, runs applications,
and improves them based on test evidence.

For the first release, complete this vertical workflow before expanding
the scope:

1.  Request an inventory application in natural language.
2.  Have AI propose requirements, data models, and page structures.
3.  Validate schemas and review diffs.
4.  Approve the proposal and create a revision.
5.  Preview the CRUD application.
6.  Run tests and display the results.
7.  If tests fail, propose repairs based on evidence.

After this workflow is reliable, expand into workflows, external
integrations, Git, deployment, and multi-agent collaboration. This
approach positions Orchivio as an auditable and portable AI-native
application engineering platform---not merely an AI code generator.

**v1.1 addendum:** the Mendix benchmark shows that enterprise users expect
more than CRUD generation: a logic layer, a real application security model,
reusable UI composition, integrations, branching, and governed delivery.
Orchivio should therefore lock in the *foundations* for these in the MVP
(expression language, two-plane security with row-level rules, module- and
branch-ready schema, dependency graph, linter) and deliver the rest in
Releases 0.2--1.0. Its differentiation remains openness, self-hosting,
provider-neutral AI, and a single governed proposal pipeline shared by the
built-in AI Builder and external agents connected through MCP.

------------------------------------------------------------------------

# Appendix A. Mendix Gap Analysis Detail (v1.1)

Source basis: Mendix public documentation, release notes, and roadmap pages
through Mendix 11.15 (released 2026-09-30). Items marked beta in Mendix may
change. Re-verify before external use.

| # | Capability | Mendix | Orchivio v1.0 | Gap | Orchivio v1.1 Decision | FR | Phase |
|---|---|---|---|---|---|---|---|
| 1 | Domain modeling depth | Rich (enums, calculated attributes, indexes, inheritance, event handlers) | Basic | Large | Adopt core items in MVP; inheritance P2 | FR-028, FR-051, FR-131--135 | 2, 8, 12 |
| 2 | Visual logic (server/client) | Mature | None | Critical | Expression language P0; flows P1 | FR-050--058 | 2, 8 |
| 3 | Human workflows | Mature, timers and events added through 2026 | None | Large | Process engine P1/P2 | FR-110--113 | 11, 12 |
| 4 | App security model | Roles, module roles, row constraints, page/logic access | Studio RBAC + simple entity roles | Critical | Two-plane model with row rules in MVP | FR-060--068 | 3 |
| 5 | UI composition | Layouts, snippets, building blocks, templates, design system, custom widgets | CRUD pages | Large | Layouts and conditional UI P0; rest P1/P2 | FR-070--078 | 3, 8, 12 |
| 6 | Integration | REST/OData publish/consume, business events, external entities | Future | Large | OpenAPI import, webhooks, outbox P1 | FR-080--089 | 9, 12 |
| 7 | AI-assisted development | Planning, building, explaining, MCP client, IDE as MCP server | AI Builder + repair | Medium | Explain/test data P0; MCP P1 with governance | FR-086, 087, 121, 127, 128 | 4, 5, 9 |
| 8 | AI inside apps | Agents kit, connectors, guardrails (beta), observability (beta) | None | Large | AI steps/guardrails P1; agents/KB P2 | FR-100--105 | 11, 12 |
| 9 | Version control | Git, branching, merge | Linear revisions | Large | Branch-ready P0; merge P1 | FR-090--093 | 2, 10 |
| 10 | Delivery and operations | Environments, pipelines, zero-downtime, OTel, LTS, update assistant | Preview only | Large | Environments/packages P1; ZDT P2 | FR-094--097, FR-123, FR-125 | 10, 12 |
| 11 | Quality insight | Consistency checks, best-practice recommender, unit/automated tests | Schema checks, tests | Medium | Linter + usages P0 | FR-120--122 | 2, 3 |
| 12 | Agile and feedback | Epics, feedback, AI backlog | Requirement extraction | Medium | Story board + feedback P1 | FR-098, FR-099 | 8 |
| 13 | Ecosystem and governance | Marketplace, control center, audit tools | Plugin catalog P2 | Large | Registry/governance P2 | FR-124, FR-126 | 12 |
| 14 | Mobile/offline | PWA, offline, native | Deferred | Large | PWA P1, offline P2, native out of scope | FR-129 | 11 |
| 15 | Openness and portability | Proprietary runtime and project format | Open by design | Orchivio advantage | Preserve; canonical open file format | FR-093 | 10 |
| 16 | AI governance of changes | Undo/review in IDE | Approval levels, audit | Orchivio advantage | Extend to MCP and merges | FR-086, 12.5 | 9, 10 |

# Appendix B. Requirement-to-Phase Traceability (v1.1 additions)

| Phase | Requirements |
|---|---|
| 2 | FR-028, FR-029 (schema), FR-050, FR-051, FR-090, FR-122, FR-131 |
| 3 | FR-060--063, FR-065 (pages/actions), FR-066, FR-067 (local), FR-070 (basic), FR-073, FR-074 (basic), FR-080 (CRUD), FR-120 |
| 4 | FR-127, unified proposals |
| 5 | FR-128, access-rule tests, model coverage |
| 8 | FR-052--058, FR-065 (flows), FR-098, FR-099 |
| 9 | FR-074 (export), FR-080 (custom), FR-081--083 (outbox), FR-086, FR-087, FR-089 |
| 10 | FR-091--096, FR-067 (OIDC), FR-123, FR-125 |
| 11 | FR-100, FR-103--105, FR-110, FR-111, FR-129 (PWA), FR-121 |
| 12 | FR-029 (multi-module UI), FR-064, FR-071, FR-072, FR-075--078, FR-084, FR-101, FR-102, FR-124, FR-126, FR-132--134 |
| Later (P2) | FR-085, FR-088, FR-097, FR-112, FR-113, FR-135, FR-067 (SAML), FR-129 (offline) |
