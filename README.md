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
- **HTTP API scaffold**: health check and stateless `validate` / `diff` / `expression check`.

## Quick start

Requires Python 3.11+.

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev,server]"
pytest                                               # unit + integration tests (SQLite)
uvicorn orchivio.api.main:app --reload               # http://127.0.0.1:8000/api/docs
```

Try the validator against the sample model:

```bash
python - <<'EOF'
import yaml, json, urllib.request
model = yaml.safe_load(open("examples/inventory-app/model.yaml"))
req = urllib.request.Request("http://127.0.0.1:8000/api/v1/models/validate",
    data=json.dumps({"model": model}).encode(), headers={"Content-Type": "application/json"})
print(json.load(urllib.request.urlopen(req))["valid"])
EOF
```

With Docker (untested in CI so far): `cp .env.example .env`, set `POSTGRES_PASSWORD`, then
`docker compose up --build`.

## Repository layout

```
src/orchivio/   expr, model, security, policy, db, llm, ports, api   (see docs/ARCHITECTURE.md)
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
| [docs/DATABASE.md](docs/DATABASE.md) | Platform database schema |
| [docs/INTERFACES.md](docs/INTERFACES.md) | Ports and extension points |
| [docs/API-SPEC.md](docs/API-SPEC.md) | HTTP API conventions and endpoints |
| [docs/THREAT-MODEL.md](docs/THREAT-MODEL.md) | Initial threat model |
| [docs/adr/](docs/adr/README.md) | Architecture decision records |
| [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md) | Contribution and security policy |

## License

The repository currently contains the GNU GPL v3 text in [LICENSE](LICENSE). The final licence is
an open decision ([ADR-0006](docs/adr/0006-licence.md)); do not assume it is settled.
