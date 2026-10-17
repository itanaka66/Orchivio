# ADR-0001: Single distribution with enforced package boundaries

Status: Provisional · Date: 2026-10-17

## Context
DESIGN.md §14 sketches a monorepo with many `packages/*` plus `apps/*`, and §6.2 says to start
as a modular monolith. Splitting into separately versioned packages now would add packaging
overhead before the boundaries are proven.

## Decision
One installable Python distribution (`orchivio`, `src/` layout) containing sub-packages
(`expr`, `model`, `security`, `policy`, `db`, `llm`, `ports`, `api`). Dependency direction is
declared in [ARCHITECTURE.md](../ARCHITECTURE.md) and **enforced by a test**
(`tests/unit/test_architecture.py`). The web Studio (React/TypeScript) will live in `apps/web`
when it starts.

## Alternatives
- Multi-package monorepo now (as in §14): clearer ownership, slower iteration and release
  mechanics for a solo/small team.
- Flat module layout without enforcement: fastest, but boundaries erode.

## Consequences
Packages can be extracted later with little untangling because imports already respect the
layering. Everything versions together until then. Revisit after the Phase 0 proof of concept.
