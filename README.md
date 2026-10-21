# Orchivio

**AI-native application engineering platform** — describe an application in natural language, let the AI propose the data model, pages, logic, and tests, review and approve every change, then run the generated application.

> **Pre-alpha.** Not ready for production use. APIs and data formats will change without notice.

---

## What it is

Orchivio maintains your application as a structured model (entities, pages, security rules, flows, processes) and treats that model as the single source of truth. The AI proposes changes; humans approve them. Nothing is applied automatically.

Key properties:

- **Model-first.** Generate UI, database migrations, access rules, and tests from one JSON model.
- **Human-governed.** Every change goes through a diff-and-approve proposal flow before touching your data.
- **Self-hostable.** A single Docker Compose file runs the full stack. No SaaS required.
- **AI-optional.** All Studio features work without an LLM key; AI features are additive.

## Status

| Area | State |
|---|---|
| Platform auth, orgs, projects | ✅ implemented |
| Application model (entities, fields, enumerations, modules) | ✅ implemented |
| Module management UI (add, rename, delete via proposals) | ✅ implemented |
| Proposal flow (diff, lint, approval levels) | ✅ implemented |
| CRUD preview runtime | ✅ implemented |
| End-user auth (login, logout, CSRF, OIDC, password change) | ✅ implemented |
| Application security (roles, row rules, field-level access) | ✅ implemented |
| AI Builder (analyze → propose) | ✅ implemented |
| Test generation and repair loop | ✅ implemented |
| Page designer (Studio) | ✅ implemented |
| Process designer | ✅ implemented |
| Server Flows (logic engine) | ✅ implemented |
| Client flow browser runtime | ✅ implemented |
| Flow debugger | ✅ implemented |
| Delivery: environments, signed packages, promotion | ✅ implemented |
| Near-zero-downtime migrations (expand/contract) | ✅ implemented |
| Studio MCP server | ✅ implemented |
| PWA (installable Studio, offline shell) | ✅ implemented |
| Typed client generation (TypeScript, Python, C#) | ✅ implemented |
| Requirements & story editor | ✅ implemented |
| Audit log (org-scoped event stream) | ✅ implemented |
| Three-way model merge | 🔲 planned (R0.4) |
| External MCP client (AI in apps) | 🔲 planned (R0.5) |
| Module registry | 🔲 planned (R1.0) |

Full roadmap: [DESIGN.md §15](DESIGN.md).

## Quick start

**Requires:** Docker and Docker Compose.

```bash
git clone https://github.com/itanaka66/Orchivio.git
cd Orchivio
cp .env.example .env          # set POSTGRES_PASSWORD
docker compose up --build
```

Then open **http://localhost** in your browser. The Studio is served by nginx; the API is also reachable directly at http://localhost:8000.

To enable the AI Builder, add your Anthropic key to `.env`:

```
ORCHIVIO_LLM_PROVIDER=anthropic
ORCHIVIO_ANTHROPIC_API_KEY=sk-ant-…
```

## Running without Docker

Requires Python 3.11+ and Node 18+.

```bash
# Backend
pip install -e ".[postgres,server]"
alembic upgrade head
uvicorn orchivio.api.main:app --reload

# Frontend (separate terminal)
cd apps/web
npm install
npm run dev       # dev server with /api proxy to :8000
```

## Repository layout

```
src/orchivio/     Python package (API, model, runtime, security, flows, …)
apps/web/         React + TypeScript Studio (Vite)
migrations/       Alembic migration scripts
tests/
  integration/    PostgreSQL integration tests (set ORCHIVIO_TEST_PG_URL)
  live/           Live-API tests against real Anthropic models (set ORCHIVIO_LIVE_LLM=1)
  evals/          AI quality eval harness with rubric fixtures
docs/             Architecture, specs, ADRs
k8s/              Kubernetes manifests (namespace, postgres, api, web, ingress)
docker/           initdb scripts
```

Full layout: [DESIGN.md §14](DESIGN.md).

## Running the tests

```bash
# Unit tests (SQLite, no external services)
pytest

# PostgreSQL integration tests
ORCHIVIO_TEST_PG_URL=postgresql://user:pass@localhost/orchivio_test pytest -m postgres

# Live Anthropic adapter tests
ORCHIVIO_LIVE_LLM=1 ORCHIVIO_ANTHROPIC_API_KEY=sk-ant-… pytest -m live_api

# AI quality evals
ORCHIVIO_LIVE_LLM=1 ORCHIVIO_ANTHROPIC_API_KEY=sk-ant-… pytest -m eval -v
```

The `live_api` and `eval` tests are skipped automatically when the environment variables are absent.

## Documentation

| Document | Description |
|---|---|
| [DESIGN.md](DESIGN.md) | Requirements, architecture, roadmap |
| [SECURITY.md](SECURITY.md) | Vulnerability reporting |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Development setup and PR guidelines |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Components and data flows |
| [docs/MODEL-SPEC.md](docs/MODEL-SPEC.md) | Application model schema |
| [docs/API-SPEC.md](docs/API-SPEC.md) | REST API conventions |
| [docs/APP-SECURITY.md](docs/APP-SECURITY.md) | Two-plane security model |
| [docs/FLOW-SPEC.md](docs/FLOW-SPEC.md) | Server and client flow specification |
| [docs/MCP.md](docs/MCP.md) | Studio MCP server (agent integration) |
| [docs/DELIVERY.md](docs/DELIVERY.md) | Environments, packages, promotion |
| [docs/THREAT-MODEL.md](docs/THREAT-MODEL.md) | Threats and mitigations |
| [docs/STATUS.md](docs/STATUS.md) | Known gaps and limitations |
| [docs/TUTORIAL.md](docs/TUTORIAL.md) | Studio quick-start tutorial (English and Japanese) |
| [docs/RELEASE.md](docs/RELEASE.md) | Release-candidate and release process |
| [NOTICE](NOTICE) | Third-party software notices |
| [docs/adr/](docs/adr/) | Architecture decision records |

## License

GNU General Public License v3.0 (GPL-3.0-or-later) — Copyright (C) 2026 株式会社エージーネディア / agNedia Inc.

See [LICENSE](LICENSE) for the full text.
