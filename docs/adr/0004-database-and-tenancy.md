# ADR-0004: Database and tenancy (ADR-TEN-003)

Status: Part 1 Accepted, part 2 Proposed · Date: 2026-10-17 · DESIGN.md §22 items 4 and 13

## Part 1 — Platform database (Accepted)
PostgreSQL is the target for platform metadata. SQLAlchemy 2 models and Alembic migrations are
the schema source. SQLite is used **only for tests** through portable types (JSONB↔JSON,
UUID[]↔JSON); PostgreSQL-only features (triggers, circular FKs) are behind dialect checks and
verified by a dedicated CI job. Platform metadata and generated-application data never share
tables.

Consequence: anything SQLite cannot do needs a conditional branch in the migration and a
PostgreSQL CI check; tests on SQLite alone are not evidence that PostgreSQL behaviour is right.

## Part 2 — Generated-application data (Proposed)
**Schema per application** within one PostgreSQL database for the MVP, with a database role per
application schema. Database-per-application becomes an option for customers needing stronger
isolation or independent backup/restore.

Rationale: schema-per-app gives strong logical separation, simple preview/production isolation
(FR-035), and keeps operations manageable for a single-node deployment. It makes cross-app
reporting deliberate rather than accidental.

Open: migration tooling for generated schemas (expand/contract later), connection management
for many schemas, backup granularity. Decide before Phase 3 implements the CRUD runtime.
