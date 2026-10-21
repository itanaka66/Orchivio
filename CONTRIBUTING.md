# Contributing to Orchivio

Thanks for your interest. This is a pre-alpha project; the design and APIs are still moving. Contributions are welcome, but please open an issue to discuss non-trivial changes before submitting a pull request.

## Before you start

- Read [DESIGN.md](DESIGN.md) for requirements and architecture.
- Check [docs/STATUS.md](docs/STATUS.md) for known gaps before filing a new issue.
- Security issues: see [SECURITY.md](SECURITY.md) — do not open a public issue.

## Development setup

**Requirements:** Python 3.11+, Node 18+, Docker (for integration tests).

### Backend

```bash
pip install -e ".[postgres,server,dev]"
```

Run the API against a local Postgres (or the Compose stack):

```bash
cp .env.example .env          # set POSTGRES_PASSWORD
docker compose up db -d       # start Postgres only
alembic upgrade head
uvicorn orchivio.api.main:app --reload
```

### Frontend

```bash
cd apps/web
npm install
npm run dev       # proxies /api to localhost:8000
```

### Full stack

```bash
docker compose up --build     # API + Postgres + nginx-served Studio on :80
```

## Running checks

### Python

```bash
pytest                        # unit + integration tests
ruff check src tests          # lint
ruff format --check src tests # format check (CI enforces this)
```

All tests use a real database; mocking the database is not acceptable (see ADR-004).

### TypeScript / React

```bash
cd apps/web
npm run typecheck             # tsc --noEmit (strict mode; all errors must be zero)
npm run test                  # Vitest unit tests
npm run build                 # full production build
```

The TypeScript config uses `strict`, `noUncheckedIndexedAccess`, `noUnusedLocals`, and `noUnusedParameters`. `array[i]` returns `T | undefined`; guard or assert `!` with intent.

## Code conventions

- **Proposals, not direct writes.** All model mutations go through `client.createProposal(appId, model, title)`. No route may write a model directly without creating a proposal and approval.
- **Deny by default.** Every new API route must call `authorize(principal, action)` before touching data. Omitting it is a security bug.
- **No arbitrary LLM execution.** LLM output is validated against the model schema before any use. Raw strings from an LLM are never executed as code.
- **Comments.** Write no comments unless the *why* is non-obvious — a hidden constraint, a specific bug workaround, or a subtle invariant. Do not explain what the code does.
- **Backend style:** ruff enforces `E`, `F`, `I`, `B`, `UP`; lines up to 100 characters.
- **Frontend style:** no formatter (intentional — see pyproject.toml for rationale). Keep vanilla CSS; no Tailwind or component libraries.

## Pull request guidelines

1. **Branch from `main`**, use a short descriptive name: `feat/...`, `fix/...`, `docs/...`.
2. **One concern per PR.** Separate a refactor from a feature.
3. **Tests are required.** New API routes need at least one happy-path and one error test. New model features need validation and lint tests.
4. **Update docs.** If your change affects a spec (`docs/*.md`), update it in the same PR.
5. **Migrations.** Add an Alembic migration for every schema change; never edit existing migrations.
6. **Special review.** Changes to `src/orchivio/policy/`, `src/orchivio/security/`, `migrations/`, and worker execution boundaries get extra scrutiny and may require an ADR.
7. **PR description:** state the motivation, what changed, and how to test it. Reference the relevant DESIGN.md section or FR number.

## Definition of done

A feature is complete only when (DESIGN.md §20.2):

- All acceptance criteria from the relevant FR are met.
- Type checks, linting, and tests pass in CI.
- Required integration tests are added.
- Permissions, errors, and edge cases are tested.
- Relevant `docs/*.md` files are updated.
- Any new ADRs, migrations, and license records are present.

## License

By contributing, you agree that your contributions will be licensed under [GPL-3.0-or-later](LICENSE).
