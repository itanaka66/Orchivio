# Contributing

Orchivio is pre-alpha. Read [DESIGN.md](DESIGN.md) for the plan and
[docs/STATUS.md](docs/STATUS.md) for what exists before starting work.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,server]"
pytest
ruff check .
```

## Rules (DESIGN.md §20)

- Every issue states purpose, acceptance criteria, test strategy and impact.
- Update the requirement, spec or ADR **before** changing behaviour it describes.
- No direct pushes to `main`; open a pull request and pass CI.
- AI-generated code gets the same review, tests and licence checks as any other code.
- Extra scrutiny for migrations, authorization, secrets and worker boundaries.
- Keep implemented, experimental and planned features clearly separated in docs
  ([docs/STATUS.md](docs/STATUS.md)).

## Definition of done

1. Acceptance criteria met.
2. Type checks, lint and unit tests pass.
3. Integration/E2E tests added where relevant.
4. Permissions, errors and boundary conditions tested (deny paths too).
5. Logging, auditing and operations considered.
6. User and developer docs updated.
7. ADRs, migrations and licence records updated.
8. Known limitations written down.

## Code conventions

- Package dependency rules are enforced by `tests/unit/test_architecture.py`; see
  [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
- Model output and request data are untrusted: validate with Pydantic at the boundary, never
  `eval`/`exec`, never build SQL by string-formatting values.
- Database changes need an Alembic migration; `tests/integration/test_migrations.py` must pass.
- New SQL-compiled expression constructs need an interpreter-vs-SQL equivalence test.
- Don't add a dependency without checking its licence (SBOM/NOTICE are release requirements).
