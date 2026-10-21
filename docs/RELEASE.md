# Release Process

This document describes how to tag a release candidate, verify reproducible builds, and
measure performance before promoting to a stable release.

## Version scheme

Orchivio uses [Semantic Versioning](https://semver.org/): `MAJOR.MINOR.PATCH`.  
Pre-release suffixes: `-rc.N` for release candidates, `.devN` for development snapshots.

Current development version: `0.1.0.dev0` (see `pyproject.toml`).

## Release checklist

### 1. Verify the CI pipeline

- All jobs in `.github/workflows/ci.yml` must be green on the release branch.
- The `postgres-migrations` job (runs against real PostgreSQL) must pass.
- No open security advisories on Python or npm dependencies:

  ```bash
  pip-audit                       # Python
  npm audit --audit-level=high    # Node
  ```

### 2. Update the version

Edit `pyproject.toml`:

```toml
version = "0.1.0rc1"   # or "0.1.0" for a stable release
```

Edit `apps/web/package.json`:

```json
"version": "0.1.0-rc.1"
```

Commit both files:

```bash
git add pyproject.toml apps/web/package.json
git commit -m "chore: bump version to 0.1.0rc1"
```

### 3. Tag the release candidate

```bash
git tag -s v0.1.0rc1 -m "Release candidate 0.1.0rc1"
git push origin v0.1.0rc1
```

Use a GPG-signed tag (`-s`) when the signing key is available; otherwise use `-a` (annotated).

### 4. Build the Docker image

```bash
docker build --no-cache -t orchivio:v0.1.0rc1 .
```

Record the image digest:

```bash
docker inspect --format='{{.Id}}' orchivio:v0.1.0rc1
```

### 5. Verify reproducible builds

Run the build twice from a clean checkout and compare digests.

```bash
git clone https://github.com/itanaka66/Orchivio.git /tmp/build-a
cd /tmp/build-a && git checkout v0.1.0rc1
docker build --no-cache -t orchivio:rc1-a .

git clone https://github.com/itanaka66/Orchivio.git /tmp/build-b
cd /tmp/build-b && git checkout v0.1.0rc1
docker build --no-cache -t orchivio:rc1-b .

docker inspect --format='{{.RootFS.Layers}}' orchivio:rc1-a
docker inspect --format='{{.RootFS.Layers}}' orchivio:rc1-b
```

Layers must be identical.  Known non-reproducible sources and mitigations:

| Source | Mitigation |
|---|---|
| `pip install` ordering | Use `pip install --require-hashes` with a locked requirements file |
| Build timestamps in `.pyc` | Set `SOURCE_DATE_EPOCH` in the Dockerfile |
| npm caching | Pin `package-lock.json` and use `npm ci` |
| OS package versions | Pin the base image to a digest, e.g. `python:3.11-slim@sha256:…` |

### 6. Generate the SBOM

The deployment-package endpoint generates a direct-dependency SBOM automatically
(see `FR-095` in `docs/STATUS.md`). For a standalone SBOM:

```bash
# Python
pip install cyclonedx-bom
cyclonedx-py environment > sbom-python.json

# Node
npm install -g @cyclonedx/cyclonedx-npm
cyclonedx-npm --output-file sbom-npm.json
```

Attach both files to the GitHub release.

### 7. Smoke-test the release image

```bash
docker compose up --build
# In a second terminal:
curl -s http://localhost/healthz
curl -s http://localhost:8000/healthz
```

Both must return `{"status": "ok"}`.

Run the sample app E2E scenario:

```bash
cd apps/web
npm run e2e:studio   # requires Node + Python in PATH
```

### 8. Performance benchmarks

Run the benchmark suite and record the results in `docs/BENCHMARKS.md`.

```bash
# From the repo root, with the API server running on port 8000:
python scripts/benchmark.py --url http://localhost:8000 --out docs/BENCHMARKS.md
```

If `scripts/benchmark.py` does not exist yet, run the integration test suite and note timing:

```bash
pytest tests/integration/ -q --tb=no --co -q   # count tests
time pytest tests/integration/ -q --tb=no       # wall time
```

Acceptable baselines for a development workstation (SQLite):

| Scenario | Target |
|---|---|
| `POST /auth/login` | < 200 ms (p99) |
| `GET /applications/{id}/revisions` | < 100 ms (p99) |
| Full integration test suite | < 120 s |

### 9. Promote to stable

After the RC has been in use for at least one week without critical bugs:

1. Repeat steps 2–8 with version `0.1.0` (no `-rc` suffix).
2. Tag `v0.1.0` and push.
3. Publish the GitHub release with the SBOM, Docker digest, and release notes.
4. Update the `latest` Docker tag:

   ```bash
   docker tag orchivio:v0.1.0 orchivio:latest
   ```

## Rollback

If a regression is found after the stable tag:

1. Open a hotfix branch from the release tag: `git checkout -b hotfix/v0.1.1 v0.1.0`
2. Apply the fix, bump to `0.1.1`, and follow this checklist from step 1.
3. Do not force-push or re-use a tag that has been pushed.
