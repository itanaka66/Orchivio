"""Generated test runs over HTTP."""

import copy
import hashlib
import json

import pytest
from test_tenancy import add_member, make_org

from orchivio.llm import MockProvider
from orchivio.runtime import service as svc


@pytest.fixture
def ctx(new_api, raw_model):
    owner = new_api("owner@example.com")
    org = make_org(owner)
    proj = owner.post(f"/organizations/{org['id']}/projects", json={"name": "Inv", "slug": "inventory"}).json()
    people = {"owner": owner}
    for role in ("editor", "viewer"):
        people[role] = new_api(f"{role}@example.com")
        add_member(owner, org, f"{role}@example.com", role)
    people["outsider"] = new_api("outsider@example.com")
    app = owner.post(f"/projects/{proj['id']}/applications", json={"key": "inventory", "name": "Inv", "model": raw_model}).json()
    return proj, app, people


def start(api, rev_id, **body):
    return api.post(f"/revisions/{rev_id}/test-runs", json=body)


def test_run_stores_a_report_and_summary(app, ctx):
    proj, a, p = ctx
    rev = a["current_revision_id"]
    assert start(p["viewer"], rev).status_code == 403
    r = start(p["editor"], rev, seed=3)
    assert r.status_code == 201
    run = r.json()
    assert run["status"] == "succeeded" and run["summary"]["counts"]["failed"] == 0 and run["summary"]["counts"]["passed"] >= 20
    assert run["summary"]["coverage"]["access_rules"] == {"covered": 3, "total": 3} and run["comparison"] is None

    detail = p["viewer"].get(f"/test-runs/{run['id']}?report=true").json()
    assert detail["report"]["ok"] is True and len(detail["report"]["results"]) >= 20
    assert "report" not in p["viewer"].get(f"/test-runs/{run['id']}").json()
    blob = app.state.artifacts.get(run["report_ref"])
    assert hashlib.sha256(blob).hexdigest() == run["summary"]["sha256"] and json.loads(blob)["seed"] == 3

    listing = p["viewer"].get(f"/revisions/{rev}/test-runs").json()
    assert listing["total"] == 1 and listing["items"][0]["id"] == run["id"]
    assert p["outsider"].get(f"/test-runs/{run['id']}").status_code == 404
    assert start(p["outsider"], rev).status_code == 404


def test_runtime_data_is_cleaned_up_and_application_data_stays_out_of_the_platform_db(app, ctx):
    from sqlalchemy import inspect

    proj, a, p = ctx
    start(p["editor"], a["current_revision_id"])
    leftover = [t for t in inspect(app.state.preview_store.engine).get_table_names() if t.startswith("app_")]
    assert leftover == []


def test_a_regression_is_reported_and_compared_with_the_previous_run(app, ctx, monkeypatch):
    proj, a, p = ctx
    rev = a["current_revision_id"]
    assert start(p["editor"], rev).json()["status"] == "succeeded"

    monkeypatch.setattr(svc, "resolve_access", lambda *args, **kw: svc.AccessDecision(True, True))
    bad = start(p["editor"], rev).json()
    assert bad["status"] == "failed" and bad["summary"]["failed"]
    assert "access:core.Product:viewer:create" in bad["comparison"]["newly_failing"]

    monkeypatch.undo()
    fixed = start(p["editor"], rev).json()
    assert fixed["status"] == "succeeded"
    assert "access:core.Product:viewer:create" in fixed["comparison"]["newly_passing"]
    events = p["owner"].get(f"/organizations/{proj['organization_id']}/audit-events?action_prefix=tests.").json()
    assert events["total"] == 3


def test_a_model_that_cannot_be_built_is_a_failed_build(app, ctx, monkeypatch):
    import orchivio.api.routes.testruns as mod

    proj, a, p = ctx
    monkeypatch.setattr(mod, "run_tests", lambda *args, **kw: (_ for _ in ()).throw(RuntimeError("cannot create table")))
    r = start(p["editor"], a["current_revision_id"])
    assert r.status_code == 201 and r.json()["status"] == "failed" and "cannot create table" in r.json()["summary"]["error"]


def test_repair_proposal_from_a_failed_run(app, ctx, raw_model, monkeypatch):
    proj, a, p = ctx
    rev = a["current_revision_id"]
    assert p["editor"].post(f"/test-runs/{start(p['editor'], rev).json()['id']}/ai/repair").json()["error"]["code"] in (
        "nothing_to_repair", "ai_not_configured")

    monkeypatch.setattr(svc, "resolve_access", lambda *args, **kw: svc.AccessDecision(True, True))
    bad = start(p["editor"], rev).json()
    monkeypatch.undo()

    app.state.llm_provider = None
    assert p["editor"].post(f"/test-runs/{bad['id']}/ai/repair").json()["error"]["code"] == "ai_not_configured"

    fixed = copy.deepcopy(raw_model)
    fixed["modules"][0]["entities"][0]["display_name"] = "Product (checked)"
    provider = MockProvider([json.dumps(fixed)])
    app.state.llm_provider = provider
    assert p["viewer"].post(f"/test-runs/{bad['id']}/ai/repair").status_code == 403
    r = p["editor"].post(f"/test-runs/{bad['id']}/ai/repair")
    assert r.status_code == 201, r.text
    prop = r.json()["proposal"]
    assert prop["status"] == "pending" and prop["source"] == "ai_builder"
    assert "access:core.Product:viewer:create" in provider.requests[0].messages[1].content  # failures reach the prompt
    assert "Root causes by category:" in provider.requests[0].messages[1].content
    rc = r.json().get("root_causes", [])
    assert rc and rc[0]["kind"] == "access" and rc[0]["count"] >= 1
    assert p["viewer"].get(f"/applications/{a['id']}").json()["current_revision_no"] == 1  # nothing applied

    # once the model moves on, an old run can no longer drive a repair
    p["editor"].post(f"/proposals/{prop['id']}/approval", json={"decision": "approve"})
    app.state.llm_provider = MockProvider([json.dumps(fixed)])
    assert p["editor"].post(f"/test-runs/{bad['id']}/ai/repair").json()["error"]["code"] == "stale_run"
