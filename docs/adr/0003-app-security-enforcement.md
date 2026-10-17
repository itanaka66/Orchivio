# ADR-0003: Row-level security enforcement (ADR-SEC-002)

Status: Proposed · Date: 2026-10-17 · DESIGN.md §22 item 12

## Context
Generated apps must enforce row constraints on every data path (reads, updates, deletes,
counts, aggregates, exports, later OData/MCP/knowledge bases). Two mechanisms are available:
predicates injected by the runtime data layer, and PostgreSQL row-level security (RLS).

## Proposal
1. **Runtime predicates are the primary mechanism** (implemented: `row_predicate`). One data-access
   layer applies them; there is no way to query application data around it.
2. **PostgreSQL RLS as optional defence in depth**, generated from the same compiled predicates,
   for direct-database access paths. Not before the data layer exists and is measured.
3. Constraints that cannot be compiled are **denied**, never skipped.

## Rationale
Runtime predicates work on any supported database, are testable against the interpreter, and
keep the authorization logic in one place. RLS adds a second line of defence but couples
behaviour to session variables and connection pooling, and complicates migrations and
debugging.

## Consequences
The linter must flag unindexed or nullable constraint fields (performance, §5.1). Negative
tests are generated per rule. Needs a decision before Phase 3 completes; if RLS is adopted,
add equivalence tests (interpreter vs SQL vs RLS).
