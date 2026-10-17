"""Running flows and hooks through the preview API."""

import copy

import pytest
from test_tenancy import add_member, make_org


@pytest.fixture
def ctx(new_api, raw_model):
    owner = new_api("owner@example.com")
    org = make_org(owner)
    proj = owner.post(f"/organizations/{org['id']}/projects", json={"name": "Inv", "slug": "inventory"}).json()
    people = {"owner": owner}
    for role in ("editor", "viewer"):
        people[role] = new_api(f"{role}@example.com")
        add_member(owner, org, f"{role}@example.com", role)
    app = owner.post(f"/projects/{proj['id']}/applications", json={"key": "inventory", "name": "Inv", "model": raw_model}).json()
    return proj, app, people


def acting(user, *roles):
    return {"X-Preview-User": user, "X-Preview-Roles": ",".join(roles)}


def base(app):
    return f"/applications/{app['id']}/preview"


PRODUCT = "core.Product"


def seed(p, app, quantity, reorder, sku):
    r = p["editor"].post(f"{base(app)}/data/{PRODUCT}", headers=acting("a", "admin"),
                         json={"name": sku, "sku": sku, "quantity": quantity, "reorder_level": reorder})
    assert r.status_code == 201, r.text


def test_a_flow_runs_in_the_preview_and_returns_its_trace(ctx):
    _, app, p = ctx
    p["editor"].post(f"{base(app)}/reset", json={})
    seed(p, app, 1, 5, "low")
    seed(p, app, 50, 5, "fine")
    r = p["editor"].post(f"{base(app)}/flows/ReorderLowStock/run", json={"args": {"min_gap": 0}}, headers=acting("a", "admin"))
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["status"] == "succeeded" and out["flow"] == "core.ReorderLowStock"
    assert [s["step"] for s in out["trace"]][:3] == ["start", "find", "any"] and out["steps_executed"] == len(out["trace"])
    assert next(s for s in out["trace"] if s["step"] == "find")["detail"] == "1 record(s)"
    rows = p["viewer"].get(f"{base(app)}/data/{PRODUCT}?order_by=sku", headers=acting("v", "viewer")).json()["items"]
    assert {r["sku"]: r["reorder_level"] for r in rows} == {"fine": 5, "low": 10}


def test_failures_are_reported_and_rolled_back(ctx):
    _, app, p = ctx
    p["editor"].post(f"{base(app)}/reset", json={})
    seed(p, app, 50, 5, "fine")
    out = p["editor"].post(f"{base(app)}/flows/ReorderLowStock/run", json={"args": {"min_gap": 0}}, headers=acting("a", "admin")).json()
    assert out["status"] == "failed" and out["error_step"] == "none" and "reorder level" in out["error"]


def test_the_simulated_users_security_applies(ctx):
    _, app, p = ctx
    p["editor"].post(f"{base(app)}/reset", json={})
    seed(p, app, 1, 5, "low")
    out = p["editor"].post(f"{base(app)}/flows/ReorderLowStock/run", json={"args": {"min_gap": 0}}, headers=acting("v", "viewer")).json()
    assert out["status"] == "failed" and "may not update" in out["error"]
    none = p["owner"].post(f"{base(app)}/flows/ReorderLowStock/run", json={"args": {"min_gap": 0}}, headers=acting("o")).json()
    assert none["status"] == "failed"  # a Studio owner simulating nobody can do nothing


def test_studio_roles_and_input_checks(ctx):
    _, app, p = ctx
    p["editor"].post(f"{base(app)}/reset", json={})
    url = f"{base(app)}/flows/ReorderLowStock/run"
    assert p["viewer"].post(url, json={}, headers=acting("a", "admin")).status_code == 403  # running may write data
    assert p["editor"].post(f"{base(app)}/flows/Nope/run", json={}, headers=acting("a", "admin")).json()["error"]["code"] == "unknown_flow"
    bad = p["editor"].post(url, json={"args": {"min_gap": "x"}}, headers=acting("a", "admin")).json()
    assert bad["status"] == "failed" and "integer" in bad["error"]
    assert p["editor"].post(url, json={"args": {}, "bogus": 1}, headers=acting("a", "admin")).status_code == 422


def test_hook_flows_are_not_directly_runnable_and_fire_on_writes(ctx, raw_model):
    _, app, p = ctx
    hooked = copy.deepcopy(raw_model)
    hooked["modules"][0]["entities"][0]["hooks"] = {"before_create": "Guard"}
    hooked["modules"][0]["flows"].append({
        "key": "Guard", "steps": [
            {"id": "start", "type": "start"},
            {"id": "d", "type": "decision", "config": {"expr": "record.quantity > 1000"}},
            {"id": "no", "type": "raise_error", "config": {"message": "quantity too large"}},
            {"id": "ok", "type": "end"}],
        "edges": [{"id": "e1", "source": "start", "target": "d"},
                  {"id": "e2", "source": "d", "target": "no", "handle": "true"},
                  {"id": "e3", "source": "d", "target": "ok", "handle": "false"}]})
    assert p["editor"].post(f"/applications/{app['id']}/revisions", json={"model": hooked}).status_code == 201
    p["editor"].post(f"{base(app)}/reset", json={})
    r = p["editor"].post(f"{base(app)}/flows/Guard/run", json={}, headers=acting("a", "admin"))
    assert r.status_code == 422 and r.json()["error"]["code"] == "not_runnable"
    ok = p["editor"].post(f"{base(app)}/data/{PRODUCT}", headers=acting("a", "admin"),
                          json={"name": "n", "sku": "s1", "quantity": 5, "reorder_level": 1})
    assert ok.status_code == 201
    veto = p["editor"].post(f"{base(app)}/data/{PRODUCT}", headers=acting("a", "admin"),
                            json={"name": "n", "sku": "s2", "quantity": 5000, "reorder_level": 1})
    assert veto.status_code == 409 and veto.json()["error"]["code"] == "hook_failed"
    assert "quantity too large" in veto.json()["error"]["message"]


def test_declared_flow_tests_appear_in_generated_test_runs(ctx):
    _, app, p = ctx
    run = p["editor"].post(f"/revisions/{app['current_revision_id']}/test-runs", json={}).json()
    assert run["status"] == "succeeded" and run["summary"]["coverage"]["flows"] == {"covered": 1, "total": 1}
    report = p["viewer"].get(f"/test-runs/{run['id']}?report=true").json()["report"]
    flow_cases = [r for r in report["results"] if r["kind"] == "flow"]
    assert len(flow_cases) == 2 and {r["status"] for r in flow_cases} == {"passed"}


def test_flow_runs_are_audited(ctx):
    proj, app, p = ctx
    p["editor"].post(f"{base(app)}/reset", json={})
    p["editor"].post(f"{base(app)}/flows/ReorderLowStock/run", json={"args": {"min_gap": 0}}, headers=acting("a", "admin"))
    events = p["owner"].get(f"/organizations/{proj['organization_id']}/audit-events?action_prefix=flow.").json()
    assert events["total"] == 1 and events["items"][0]["metadata"]["as_user"] == "a"
