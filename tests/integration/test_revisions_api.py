"""Applications and revisions over HTTP: validation, risk gating, diff, rollback, isolation."""

import copy

import pytest
from test_tenancy import add_member, make_org

from orchivio.db.models import AppRevision


@pytest.fixture
def ctx(new_api):
    owner = new_api("owner@example.com")
    org = make_org(owner)
    proj = owner.post(f"/organizations/{org['id']}/projects", json={"name": "Inv", "slug": "inventory"}).json()
    people = {"owner": owner}
    for role in ("editor", "viewer"):
        people[role] = new_api(f"{role}@example.com")
        add_member(owner, org, f"{role}@example.com", role)
    people["outsider"] = new_api("outsider@example.com")
    return proj, people


def create_app(api, proj, model=None, key="inventory"):
    body = {"key": key, "name": "Inventory"}
    if model is not None:
        body["model"] = model
    return api.post(f"/projects/{proj['id']}/applications", json=body)


def test_create_application_with_initial_model(ctx, raw_model):
    proj, p = ctx
    r = create_app(p["editor"], proj, raw_model)
    assert r.status_code == 201
    app = r.json()
    assert app["current_revision_no"] == 1 and app["current_revision_id"]
    revs = p["viewer"].get(f"/applications/{app['id']}/revisions").json()
    assert revs["total"] == 1 and revs["items"][0]["parent_revision_ids"] == []
    assert "model_json" not in revs["items"][0]


def test_create_application_without_model_and_key_conflict(ctx):
    proj, p = ctx
    app = create_app(p["editor"], proj).json()
    assert app["current_revision_id"] is None
    assert create_app(p["editor"], proj).json()["error"]["code"] == "key_taken"
    assert create_app(p["viewer"], proj, key="other").status_code == 403


def test_invalid_initial_model_creates_nothing(ctx, raw_model):
    proj, p = ctx
    raw_model["security"]["access"] = []
    r = create_app(p["editor"], proj, raw_model)
    assert r.status_code == 422 and r.json()["error"]["code"] == "model_invalid"
    assert any(i["code"] == "no-access-rule" for i in r.json()["error"]["details"])
    assert p["editor"].get(f"/projects/{proj['id']}/applications").json()["total"] == 0


def test_revision_workflow_roles_and_history(ctx, raw_model):
    proj, p = ctx
    app = create_app(p["owner"], proj, raw_model).json()
    url = f"/applications/{app['id']}/revisions"
    changed = copy.deepcopy(raw_model)
    changed["modules"][0]["entities"][0]["fields"].append({"name": "note", "type": "string"})

    assert p["viewer"].post(url, json={"model": changed}).status_code == 403
    r = p["editor"].post(url, json={"model": changed, "message": "add note"})
    assert r.status_code == 201 and r.json()["revision_no"] == 2 and r.json()["message"] == "add note"
    first = p["viewer"].get(url).json()["items"][1]
    assert r.json()["parent_revision_ids"] == [first["id"]]

    detail = p["viewer"].get(f"/revisions/{r.json()['id']}").json()
    assert any(f["name"] == "note" for f in detail["model_json"]["modules"][0]["entities"][0]["fields"])
    assert p["viewer"].get(f"/applications/{app['id']}").json()["current_revision_no"] == 2


def test_identical_model_is_rejected_as_no_change(ctx, raw_model):
    proj, p = ctx
    app = create_app(p["owner"], proj, raw_model).json()
    r = p["editor"].post(f"/applications/{app['id']}/revisions", json={"model": raw_model})
    assert r.status_code == 409 and r.json()["error"]["code"] == "no_changes"


def test_invalid_revision_is_rejected_without_writing(ctx, raw_model, app):
    proj, p = ctx
    a = create_app(p["owner"], proj, raw_model).json()
    bad = copy.deepcopy(raw_model)
    bad["modules"][0]["entities"][0]["fields"][3]["type"] = "enum:Nope"
    r = p["editor"].post(f"/applications/{a['id']}/revisions", json={"model": bad})
    assert r.status_code == 422 and r.json()["error"]["code"] == "model_invalid"
    assert p["editor"].get(f"/applications/{a['id']}").json()["current_revision_no"] == 1
    # structural errors never echo input
    bad["modules"][0]["entities"][0]["extra_secret"] = "TOPSECRET"
    r = p["editor"].post(f"/applications/{a['id']}/revisions", json={"model": bad})
    assert r.status_code == 422 and "TOPSECRET" not in r.text


def test_risky_change_needs_acknowledgement(ctx, raw_model):
    proj, p = ctx
    app = create_app(p["owner"], proj, raw_model).json()
    url = f"/applications/{app['id']}/revisions"
    risky = copy.deepcopy(raw_model)
    del risky["security"]["access"][1]["row_constraint"]  # permission expansion => A3

    r = p["editor"].post(url, json={"model": risky})
    err = r.json()["error"]
    assert r.status_code == 409 and err["code"] == "approval_required"
    assert err["details"]["level"] == "A3" and err["details"]["risky_changes"][0]["risks"] == ["permission-expansion"]
    assert p["viewer"].get(f"/applications/{app['id']}").json()["current_revision_no"] == 1

    ok = p["editor"].post(url, json={"model": risky, "acknowledge_risks": True})
    assert ok.status_code == 201 and ok.json()["revision_no"] == 2


def test_destructive_change_needs_acknowledgement(ctx, raw_model):
    proj, p = ctx
    app = create_app(p["owner"], proj, raw_model).json()
    smaller = copy.deepcopy(raw_model)
    fields = smaller["modules"][0]["entities"][0]["fields"]
    smaller["modules"][0]["entities"][0]["fields"] = [f for f in fields if f["name"] != "created_at"]
    r = p["editor"].post(f"/applications/{app['id']}/revisions", json={"model": smaller})
    err = r.json()["error"]
    assert r.status_code == 409 and err["code"] == "approval_required"
    assert err["details"]["risky_changes"][0]["risks"] == ["destructive"]
    ok = p["editor"].post(f"/applications/{app['id']}/revisions", json={"model": smaller, "acknowledge_risks": True})
    assert ok.status_code == 201


def test_diff_endpoint_and_rollback(ctx, raw_model):
    proj, p = ctx
    app = create_app(p["owner"], proj, raw_model).json()
    v1 = app["current_revision_id"]
    v2model = copy.deepcopy(raw_model)
    v2model["modules"][0]["entities"][0]["fields"][3]["minimum"] = 1
    v2 = p["editor"].post(f"/applications/{app['id']}/revisions", json={"model": v2model}).json()

    d = p["viewer"].get(f"/revisions/{v2['id']}/diff/{v1}").json()
    assert d["level"] == "A2" and d["changes"][0]["properties"] == ["minimum"]

    assert p["viewer"].post(f"/applications/{app['id']}/rollback", json={"revision_id": v1}).status_code == 403
    r = p["editor"].post(f"/applications/{app['id']}/rollback", json={"revision_id": v1})
    assert r.status_code == 201 and r.json()["revision_no"] == 3 and r.json()["message"] == "Rollback to revision 1"
    assert r.json()["model_hash"] == p["viewer"].get(f"/revisions/{v1}").json()["model_hash"]
    again = p["editor"].post(f"/applications/{app['id']}/rollback", json={"revision_id": v1})
    assert again.json()["error"]["code"] == "no_changes"


def test_rollback_of_risky_direction_needs_acknowledgement(ctx, raw_model):
    proj, p = ctx
    app = create_app(p["owner"], proj, raw_model).json()
    v1 = app["current_revision_id"]
    wider = copy.deepcopy(raw_model)
    del wider["security"]["access"][1]["row_constraint"]
    p["owner"].post(f"/applications/{app['id']}/revisions", json={"model": wider, "acknowledge_risks": True})
    # going back to the constrained version narrows access: no approval needed
    assert p["editor"].post(f"/applications/{app['id']}/rollback", json={"revision_id": v1}).status_code == 201


def test_revision_ids_cannot_cross_applications_or_tenants(ctx, raw_model):
    proj, p = ctx
    a1 = create_app(p["owner"], proj, raw_model, key="one").json()
    a2 = create_app(p["owner"], proj, raw_model, key="two").json()
    other = p["outsider"]
    assert other.get(f"/applications/{a1['id']}").status_code == 404
    assert other.get(f"/revisions/{a1['current_revision_id']}").status_code == 404
    assert other.get(f"/applications/{a1['id']}/revisions").status_code == 404
    assert other.post(f"/applications/{a1['id']}/revisions", json={"model": raw_model}).status_code == 404
    assert p["owner"].post(f"/applications/{a1['id']}/rollback",
                           json={"revision_id": a2["current_revision_id"]}).status_code == 404
    assert p["owner"].get(f"/revisions/{a1['current_revision_id']}/diff/{a2['current_revision_id']}").status_code == 404


def test_revisions_are_audited(ctx, raw_model):
    proj, p = ctx
    create_app(p["owner"], proj, raw_model)
    org_id = proj["organization_id"]
    events = p["owner"].get(f"/organizations/{org_id}/audit-events?action_prefix=revision.").json()
    assert events["total"] == 1 and events["items"][0]["action"] == "revision.created"
    assert events["items"][0]["metadata"]["revision_no"] == 1


def test_pagination_bounds(ctx, raw_model):
    proj, p = ctx
    app = create_app(p["owner"], proj, raw_model).json()
    url = f"/applications/{app['id']}/revisions"
    assert p["viewer"].get(url + "?limit=0").status_code == 422
    assert p["viewer"].get(url + "?limit=201").status_code == 422
    assert p["viewer"].get(url + "?offset=-1").status_code == 422
    assert p["viewer"].get(url + "?offset=5").json()["items"] == []


def test_deleting_a_project_removes_its_revisions(ctx, raw_model, app):
    proj, p = ctx
    create_app(p["owner"], proj, raw_model)
    assert p["owner"].delete(f"/projects/{proj['id']}?confirm=inventory").status_code == 204
    with app.state.session_factory() as s:
        assert s.query(AppRevision).count() == 0


# --- lint and usages --------------------------------------------------------------------


def test_blocking_lint_error_rejects_the_revision_until_suppressed(ctx, raw_model):
    proj, p = ctx
    bad = copy.deepcopy(raw_model)
    bad["security"]["access"][1]["row_constraint"] = "record.quantity / 2 > 1"
    r = create_app(p["owner"], proj, bad)
    assert r.status_code == 422
    assert any(i["code"] == "lint:sec-constraint-not-compilable" for i in r.json()["error"]["details"])

    lint = p["owner"].post("/models/lint", json={"model": bad}).json()
    assert lint["blocking"] == 1 and len(lint["rules"]) >= 20

    # Ids don't exist before the first revision, so a client that wants to suppress a rule in a
    # new model supplies its own element_id for the target element.
    rule_id = "11111111-2222-4333-8444-555555555555"
    bad["security"]["access"][1]["element_id"] = rule_id
    bad["lint_suppressions"] = [{"rule": "sec-constraint-not-compilable", "element_id": rule_id,
                                 "justification": "Filtered in the reporting database instead"}]
    ok = create_app(p["owner"], proj, bad)
    assert ok.status_code == 201
    findings = p["owner"].get(f"/revisions/{ok.json()['current_revision_id']}/lint?include_suppressed=true").json()
    sup = next(f for f in findings["items"] if f["rule_id"] == "sec-constraint-not-compilable")
    assert sup["suppressed"] and "reporting database" in sup["justification"]
    hidden = p["owner"].get(f"/revisions/{ok.json()['current_revision_id']}/lint").json()
    assert all(not f["suppressed"] for f in hidden["items"])


def test_lint_results_and_usages_are_stored_per_revision(ctx, raw_model):
    proj, p = ctx
    app = create_app(p["owner"], proj, raw_model).json()
    rev_id = app["current_revision_id"]
    lint = p["viewer"].get(f"/revisions/{rev_id}/lint").json()
    rules = {f["rule_id"] for f in lint["items"]}
    assert "a11y-column-without-label" in rules and lint["total"] == len(lint["items"])

    detail = p["viewer"].get(f"/revisions/{rev_id}").json()
    fields = {f["name"]: f["element_id"] for f in detail["model_json"]["modules"][0]["entities"][0]["fields"]}
    used = p["viewer"].get(f"/revisions/{rev_id}/elements/{fields['quantity']}/usages").json()
    kinds = {u["kind"] for u in used["used_by"]}
    assert "expr-field" in kinds and "page-column" in kinds
    other = p["outsider"].get(f"/revisions/{rev_id}/lint")
    assert other.status_code == 404
