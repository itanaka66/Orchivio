"""A real Orchivio server for the browser tests of the generated-application shell (apps/web/e2e/appshell.e2e.ts).

Creates a throwaway SQLite database, seeds an organization, a project, an application whose model has pages, and two end users, then serves the real API
with uvicorn. Run from the repository root: ``python apps/web/e2e/app_server.py <port>``. Test support code is reused from ``tests/``.
"""

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
from test_app_auth import NEW_PW, ready_client  # noqa: E402
from test_pages import paged  # noqa: E402
from test_tenancy import make_org  # noqa: E402

from orchivio.api import create_app  # noqa: E402
from orchivio.api.settings import Settings  # noqa: E402
from orchivio.db import make_engine  # noqa: E402
from tests_support import Api, alembic_config, load_yaml  # noqa: E402


def build():
    tmp = Path(tempfile.mkdtemp(prefix="orchivio-e2e-"))
    db = tmp / "e2e.db"
    engine = create_engine(f"sqlite:///{db}")
    with engine.begin() as conn:
        command.upgrade(alembic_config(conn), "head")
    engine.dispose()
    url = f"sqlite:///{db}"
    preview_url = f"sqlite:///{tmp / 'preview.db'}"
    app = create_app(Settings(env="dev", database_url=url, preview_database_url=preview_url, artifact_dir=str(tmp / "artifacts")),
                     engine=make_engine(url), preview_engine=make_engine(preview_url))
    owner = Api(app).signup("owner@example.com")
    org = make_org(owner)
    proj = owner.post(f"/organizations/{org['id']}/projects", json={"name": "Inv", "slug": "inventory"}).json()
    a = owner.post(f"/projects/{proj['id']}/applications", json={"key": "inventory", "name": "Inventory", "model": paged(load_yaml("model.yaml"))}).json()
    owner.post(f"/applications/{a['id']}/preview/reset", json={})
    admin = ready_client(app, owner, a, "ada", ["admin"])
    me = admin.get("/auth/me").json()["id"]
    for sku, qty in (("bolt", 40), ("nut", 3)):
        assert admin.post("/data/core.Product", json={"name": sku.title(), "sku": sku, "quantity": qty, "reorder_level": 5, "owner": me}).status_code == 201
    ready_client(app, owner, a, "vera", ["viewer"])
    return app, a["id"], tmp


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 4180
    app, app_id, tmp = build()
    (Path(__file__).parent / ".app-id").write_text(app_id)
    print(f"app {app_id}; password {NEW_PW}", flush=True)
    try:
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
