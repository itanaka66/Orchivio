# Orchivio

AI-native application engineering low/no-code platform. Orchivio keeps an application as a
machine-readable, version-controlled **model** and uses it as the single source for UI, data
structures, APIs, business logic and tests. AI proposes changes; humans approve them.

> **Status: pre-alpha.** This repository contains the design ([DESIGN.md](DESIGN.md)) and the
> first implemented foundations. Most features in the design are *planned*, not built.
> [docs/STATUS.md](docs/STATUS.md) is the authoritative list of what exists.

## What works today

- **Application model** (schema 1.1): Pydantic schema, static validation, stable element IDs,
  element-level diff with risk/approval classification, 1.0 → 1.1 migration.
- **Expression language**: parser, null-safe type checker, bounded interpreter, and a compiler
  from boolean expressions to parameterised SQL predicates (for row-level security).
- **Application security (library level)**: role resolution with union semantics, row-constraint
  SQL, consistency checker.
- **Persistence**: PostgreSQL-first schema (SQLAlchemy models + Alembic migration), immutable,
  hash-checked, branch-ready revisions with audit events.
- **Interfaces** with reference adapters: LLM provider (mock + validated/retrying structured
  output), job queue, artifact store, audit sink.
- **Accounts and tenancy**: registration, login (cookie sessions with CSRF, or scoped API tokens),
  organizations with owner/admin/editor/viewer roles, members, projects, audit log.
- **Applications and revisions over HTTP**: create, validate, diff, roll back; changes that
  expand permissions or remove data need explicit acknowledgement.
- **Model tools**: authenticated `validate` / `lint` / `diff` / `expression check` endpoints.
- **Linter and find-usages**: 21 rules (security, performance, maintainability, accessibility),
  recorded suppressions, dependency graph per revision.
- **Proposals and AI Builder**: every change, human or AI, is a proposal with a diff and an approval
  level; analyze / propose / explain on a provider-neutral LLM layer (mock + Anthropic adapter), with
  consent and budget guards. See [docs/AI-BUILDER.md](docs/AI-BUILDER.md).
- **Preview runtime**: tables and a CRUD service generated from the model, enforcing roles, row-level
  rules and validation; synthetic data; view-as-role. See [docs/RUNTIME.md](docs/RUNTIME.md).
- **Flow builder** (`apps/web`): a React Flow editor for Server/Client Flows with a palette, inspector,
  undo/redo, keyboard-operable connections and live server validation; saves as a proposal. Flows are
  validated, **executed** by the Logic Engine in the preview (transactional, under the caller's security),
  traced, bound to entity events as hooks, and covered by declarative tests (docs/FLOW-SPEC.md).
- **Generated tests**: access, row-constraint, CRUD and validation cases derived from the model, with
  coverage and run-to-run comparison.

## Quick start

Requires Python 3.11+.

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev,server]"
pytest                                               # unit + integration tests (SQLite)

alembic upgrade head                                 # creates ./orchivio.db (SQLite) by default
uvicorn orchivio.api.main:app --reload               # http://127.0.0.1:8000/api/docs
```

Create an account and an application from the sample model (cookie-based session, as a browser
would use it):

```bash
B=http://127.0.0.1:8000/api/v1
curl -c jar -H 'Content-Type: application/json' -d '{"email":"me@example.com","password":"a long passphrase","display_name":"Me"}' $B/auth/register
CSRF=$(curl -sc jar -H 'Content-Type: application/json' -d '{"email":"me@example.com","password":"a long passphrase"}' $B/auth/login | python -c "import sys,json;print(json.load(sys.stdin)['csrf_token'])")
curl -b jar -H "X-CSRF-Token: $CSRF" -H 'Content-Type: application/json' -d '{"name":"Acme","slug":"acme"}' $B/organizations
```

Automation should use an API token (`POST /tokens`, then `Authorization: Bearer orv_…`);
see [docs/API-SPEC.md](docs/API-SPEC.md) for every endpoint.

With Docker (untested in CI so far): `cp .env.example .env`, set `POSTGRES_PASSWORD`, then
`docker compose up --build`.

### Web Studio

```bash
cd apps/web
npm install
npm run dev      # http://localhost:5173, proxies /api to 127.0.0.1:8000
npm test
```

## Repository layout

```
src/orchivio/   expr, model, security, policy, auth, db, llm, ai, runtime, testgen, ports, api   (see docs/ARCHITECTURE.md)
migrations/     Alembic migrations (PostgreSQL is the target; SQLite is used for tests)
examples/       sample application models
tests/          unit/ and integration/
docs/           specifications, ADRs, threat model
```

## Documentation

| Document | Purpose |
|---|---|
| [DESIGN.md](DESIGN.md) | Product requirements, architecture, roadmap (the plan) |
| [docs/STATUS.md](docs/STATUS.md) | Implemented vs. planned, known gaps |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Code structure and dependency rules |
| [docs/MODEL-SPEC.md](docs/MODEL-SPEC.md) | Application model format and validation rules |
| [docs/EXPRESSION-SPEC.md](docs/EXPRESSION-SPEC.md) | Expression language |
| [docs/APP-SECURITY.md](docs/APP-SECURITY.md) | Generated-app security model |
| [docs/AI-BUILDER.md](docs/AI-BUILDER.md) | AI Builder, proposals, approval |
| [docs/RUNTIME.md](docs/RUNTIME.md) | Preview runtime, synthetic data, generated tests |
| [docs/LINT-RULES.md](docs/LINT-RULES.md) | Linter rule catalog (generated) |
| [docs/FLOW-SPEC.md](docs/FLOW-SPEC.md) | Server/Client Flow model and validation rules |
| [apps/web/README.md](apps/web/README.md) | Web Studio and flow builder |
| [docs/DATABASE.md](docs/DATABASE.md) | Platform database schema |
| [docs/INTERFACES.md](docs/INTERFACES.md) | Ports and extension points |
| [docs/API-SPEC.md](docs/API-SPEC.md) | HTTP API conventions and endpoints |
| [docs/THREAT-MODEL.md](docs/THREAT-MODEL.md) | Initial threat model |
| [docs/adr/](docs/adr/README.md) | Architecture decision records |
| [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md) | Contribution and security policy |

## License

GNU General Public License v3.0 (GPL-3.0-or-later) — Copyright (C) 2026 株式会社エージーネディア / agNedia Inc.

The full text is in [LICENSE](LICENSE); see [ADR-0006](docs/adr/0006-licence.md) for the decision
and what it does and does not cover.
