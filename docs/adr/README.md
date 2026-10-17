# Architecture Decision Records

Format: context, decision, alternatives, consequences. Statuses: **Accepted** (in use),
**Provisional** (in use, to be confirmed by the Phase 0 proof of concept), **Proposed**
(recommendation awaiting a decision), **Open** (needs the project owner).

| ADR | Title | Status |
|---|---|---|
| [0001](0001-repository-layout.md) | Single distribution with enforced package boundaries | Provisional |
| [0002](0002-expression-language.md) | Custom CEL-inspired expression language (ADR-EXPR-001) | Provisional |
| [0003](0003-app-security-enforcement.md) | Row-level security: runtime predicates first, PostgreSQL RLS later (ADR-SEC-002) | Proposed |
| [0004](0004-database-and-tenancy.md) | PostgreSQL-first, SQLite for tests, schema-per-app data (ADR-TEN-003) | Accepted / Proposed |
| [0005](0005-job-queue.md) | DB-backed job queue behind a port | Proposed |
| [0006](0006-licence.md) | Open-source licence | **Open** |

DESIGN.md §22 lists the remaining open decisions (LLM providers, OIDC timing, plugin model,
telemetry policy, brand clearance, …).
