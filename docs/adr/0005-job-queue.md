# ADR-0005: Job queue

Status: Proposed · Date: 2026-10-17 · DESIGN.md §22 item 4

## Context
AI calls, builds and tests are long-running and must support progress, cancellation, retry,
timeouts and idempotency (FR-042). Workers run apart from the API process (§6.2).

## Proposal
Define the queue as a port (`orchivio.ports.JobQueue`, implemented) and build the first
production adapter on PostgreSQL using the existing job tables with `SELECT … FOR UPDATE SKIP
LOCKED` claiming. Add Redis only when measured load requires it.

## Rationale
Fewer moving parts for self-hosting (one datastore, transactional enqueue with the state change
that triggers it), and the port keeps the choice reversible.

## Consequences
Throughput is bounded by PostgreSQL; polling latency must be tuned (or use `LISTEN/NOTIFY`).
Handlers are allowlisted by job type, which also serves the "no arbitrary tool execution"
guardrail (§7.3). The in-memory adapter remains the reference for tests.
