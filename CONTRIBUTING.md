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

## Models as files

```bash
orchivio validate model-dir/     # a canonical directory, a zip or a single YAML file
orchivio export model.yaml model-dir/ --assign-ids
orchivio diff before/ after/     # exit code 1 when they differ; prints risk and approval level
```

Commit the exported directory to Git to review model changes as ordinary pull requests.

## Web Studio

```bash
cd apps/web && npm install && npm run typecheck && npm test
```

If you change the flow schema or validator, run `python scripts/export_web_contract.py`; `tests/unit/test_web_contract.py` fails when the generated files are stale.

## Code conventions

- Package dependency rules are enforced by `tests/unit/test_architecture.py`; see
  [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
- Model output and request data are untrusted: validate with Pydantic at the boundary, never
  `eval`/`exec`, never build SQL by string-formatting values.
- Database changes need an Alembic migration; `tests/integration/test_migrations.py` must pass.
- PostgreSQL-specific behaviour is tested in `tests/integration/test_postgres.py`, which runs only when
  `ORCHIVIO_TEST_PG_URL` points at a disposable database (CI sets it). Run it before touching triggers,
  SQL compilation or runtime column types.
- Anything that decides access (roles, row constraints, validation) needs a test that fails when the
  rule is broken on purpose (see `tests/unit/test_testgen.py` for the pattern).
- New SQL-compiled expression constructs need an interpreter-vs-SQL equivalence test.
- Don't add a dependency without checking its licence (SBOM/NOTICE are release requirements).
