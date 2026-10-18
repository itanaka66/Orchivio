"""Health, readiness and metrics (FR-045)."""

import re

from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine, text

from orchivio.api import create_app
from orchivio.api.settings import Settings
from orchivio.db import make_engine
from orchivio.db.schema_revision import SCHEMA_REVISION
from tests_support import ROOT


def test_the_expected_schema_revision_is_the_migration_head():
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    assert ScriptDirectory.from_config(cfg).get_current_head() == SCHEMA_REVISION


def test_liveness_never_touches_the_database(tmp_path):
    app = create_app(Settings(database_url=f"sqlite:///{tmp_path / 'missing.db'}", preview_database_url="sqlite:///:memory:"))
    assert TestClient(app).get("/healthz").json()["status"] == "ok"


def test_readiness_reports_ready_when_everything_is_in_place(app):
    r = TestClient(app).get("/readyz")
    assert r.status_code == 200 and r.json() == {
        "status": "ready", "checks": {"database": "ok", "migrations": "ok", "preview_database": "ok"}}


def test_readiness_notices_an_unmigrated_database(tmp_path):
    url = f"sqlite:///{tmp_path / 'empty.db'}"
    app = create_app(Settings(database_url=url, preview_database_url="sqlite:///:memory:"))
    r = TestClient(app).get("/readyz")
    assert r.status_code == 503 and r.json()["checks"]["migrations"] == "missing" and r.json()["status"] == "not_ready"


def test_readiness_notices_an_old_schema(db_url, tmp_path):
    engine = make_engine(db_url)
    with engine.begin() as conn:
        conn.execute(text("UPDATE alembic_version SET version_num = '0002'"))
    app = create_app(Settings(database_url=db_url, preview_database_url=f"sqlite:///{tmp_path / 'p.db'}"), engine=engine)
    r = TestClient(app).get("/readyz")
    assert r.status_code == 503 and r.json()["checks"]["migrations"] == "behind"


def test_readiness_does_not_leak_error_details(db_url, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'nope' / 'x.db'}")  # directory does not exist
    app = create_app(Settings(database_url=db_url, preview_database_url="sqlite:///:memory:"), engine=engine)
    r = TestClient(app).get("/readyz")
    assert r.status_code == 503 and r.json()["checks"]["database"] == "unavailable"
    assert "nope" not in r.text and "Error" not in r.text


def test_metrics_are_off_without_a_token_and_need_it_when_on(app, db_url, tmp_path):
    assert TestClient(app).get("/metrics").status_code == 404
    on = create_app(Settings(database_url=db_url, preview_database_url=f"sqlite:///{tmp_path / 'p.db'}", metrics_token=SecretStr("scrape-me")))
    c = TestClient(on)
    assert c.get("/metrics").status_code == 401
    assert c.get("/metrics", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert c.get("/metrics", headers={"Authorization": "Bearer scrape-me"}).status_code == 200


def test_metrics_count_requests_by_route_template_not_raw_path(db_url, tmp_path):
    app = create_app(Settings(database_url=db_url, preview_database_url=f"sqlite:///{tmp_path / 'p.db'}", metrics_token=SecretStr("t")))
    c = TestClient(app, raise_server_exceptions=False)
    for i in range(3):
        c.get(f"/api/v1/projects/00000000-0000-4000-8000-00000000000{i}")  # 401, three different ids
    c.get("/healthz")
    c.get("/no/such/route")
    body = c.get("/metrics", headers={"Authorization": "Bearer t"}).text
    assert 'orchivio_http_requests_total{method="GET",route="/api/v1/projects/{project_id}",status="4xx"} 3' in body
    assert 'route="/healthz",status="2xx"} 1' in body and 'route="unmatched",status="4xx"} 1' in body
    assert "00000000-0000" not in body  # ids never become labels
    assert "orchivio_database_up 1" in body and re.search(r'orchivio_build_info\{version="[^"]+"\} 1', body)
    # a valid Prometheus histogram: cumulative buckets ending in +Inf equal to _count
    inf = re.search(r'duration_seconds_bucket\{method="GET",route="/healthz",le="\+Inf"\} (\d+)', body)
    count = re.search(r'duration_seconds_count\{method="GET",route="/healthz"\} (\d+)', body)
    assert inf and count and inf.group(1) == count.group(1) == "1"
    buckets = [int(x) for x in re.findall(r'duration_seconds_bucket\{method="GET",route="/healthz",le="[^"]+"\} (\d+)', body)]
    assert buckets == sorted(buckets)
