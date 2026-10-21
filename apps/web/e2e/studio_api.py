"""Real Orchivio API for the Studio E2E tests.

Seeds a SQLite database with an owner, an Acme organisation, an Inventory project and
application (with pages and a pending proposal), then serves the FastAPI backend with
uvicorn.  Run from apps/web/: ``python e2e/studio_api.py <port>`` (default 4190).
"""

import copy
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
for p in (ROOT / "src", ROOT / "tests", ROOT / "tests" / "integration"):
    sys.path.insert(0, str(p))

import uvicorn  # noqa: E402
from alembic import command  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from test_pages import paged  # noqa: E402
from test_tenancy import make_org  # noqa: E402
from tests_support import Api, alembic_config, load_yaml  # noqa: E402

from orchivio.api import create_app  # noqa: E402
from orchivio.api.settings import Settings  # noqa: E402
from orchivio.db import make_engine  # noqa: E402


def build():
    tmp = Path(tempfile.mkdtemp(prefix="orchivio-studio-e2e-"))
    db = tmp / "e2e.db"
    engine = create_engine(f"sqlite:///{db}")
    with engine.begin() as conn:
        command.upgrade(alembic_config(conn), "head")
    engine.dispose()

    url = f"sqlite:///{db}"
    preview_url = f"sqlite:///{tmp / 'preview.db'}"
    app = create_app(
        Settings(env="dev", database_url=url, preview_database_url=preview_url,
                 artifact_dir=str(tmp / "artifacts")),
        engine=make_engine(url),
        preview_engine=make_engine(preview_url),
    )

    # owner@example.com / "correct horse battery"
    api = Api(app).signup("owner@example.com", "Owner")
    org = make_org(api)  # name="Acme", slug="acme"
    proj = api.post(f"/organizations/{org['id']}/projects",
                    json={"name": "Inventory", "slug": "inventory"}).json()
    raw_model = load_yaml("model.yaml")
    model_with_pages = paged(raw_model)
    a = api.post(f"/projects/{proj['id']}/applications",
                 json={"key": "inventory", "name": "Inventory", "model": model_with_pages}).json()
    api.post(f"/applications/{a['id']}/preview/reset", json={})

    # Seed one pending proposal so the Review proposals screen is non-empty
    modified = copy.deepcopy(model_with_pages)
    modified["modules"][0]["entities"][0]["fields"].append({"name": "note", "type": "string"})
    api.post(f"/applications/{a['id']}/proposals",
             json={"model": modified, "title": "Add note field"})

    return app, tmp


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 4190
    app, tmp = build()
    print(f"Studio API on port {port}", flush=True)
    try:
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
