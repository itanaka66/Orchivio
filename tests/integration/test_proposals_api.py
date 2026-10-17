"""Proposals: nothing changes the canonical model until a sufficiently privileged human approves."""

import copy

import pytest
from test_tenancy import add_member, make_org


@pytest.fixture
def ctx(new_api):
    owner = new_api("owner@example.com")
    org = make_org(owner)
    proj = owner.post(f"/organizations/{org['id']}/projects", json={"name": "Inv", "slug": "inventory"}).json()
    people = {"owner": owner}
    for role in ("admin", "editor", "viewer"):
        people[role] = new_api(f"{role}@example.com")
        add_member(owner, org, f"{role}@example.com", role)
    people["outsider"] = new_api("outsider@example.com")
    return proj, people


def make_app(api, proj, model=None, key="inventory"):
    body = {"key": key, "name": "Inventory"}
    if model is not None:
        body["model"] = model
    r = api.post(f"/projects/{proj['id']}/applications", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def propose(api, app_id, model, title="change"):
    return api.post(f"/applications/{app_id}/proposals", json={"model": model, "title": title})


def with_note(raw):
    m = copy.deepcopy(raw)
    m["modules"][0]["entities"][0]["fields"].append({"name": "note", "type": "string"})
    return m


def current_no(api, app_id):
    return api.get(f"/applications/{app_id}").json()["current_revision_no"]


def test_proposal_does_not_change_the_model_until_approved(ctx, raw_model):
    proj, p = ctx
    app = make_app(p["owner"], proj, raw_model)
    r = propose(p["editor"], app["id"], with_note(raw_model), "add note")
    assert r.status_code == 201
    prop = r.json()
    assert prop["status"] == "pending" and prop["approval_level"] == "A2" and prop["source"] == "human"
    assert prop["diff"]["changes"][0]["element_type"] == "field" and prop["model"] is None
    assert current_no(p["owner"], app["id"]) == 1

    done = p["editor"].post(f"/proposals/{prop['id']}/approval", json={"decision": "approve", "comment": "ok"})
    assert done.status_code == 200 and done.json()["status"] == "applied" and done.json()["result_revision_id"]
    assert current_no(p["owner"], app["id"]) == 2
    assert p["editor"].post(f"/proposals/{prop['id']}/approval", json={"decision": "approve"}).json()["error"]["code"] == "not_pending"
    assert p["viewer"].get(f"/proposals/{prop['id']}?include_model=true").json()["model"]["schema_version"] == "1.1"


def test_roles(ctx, raw_model):
    proj, p = ctx
    app = make_app(p["owner"], proj, raw_model)
    assert propose(p["viewer"], app["id"], with_note(raw_model)).status_code == 403
    prop = propose(p["editor"], app["id"], with_note(raw_model)).json()
    assert p["viewer"].get(f"/proposals/{prop['id']}").status_code == 200  # viewers can read
    assert p["viewer"].post(f"/proposals/{prop['id']}/approval", json={"decision": "approve"}).status_code == 403
    assert p["viewer"].post(f"/proposals/{prop['id']}/approval", json={"decision": "reject"}).status_code == 403
    assert p["outsider"].get(f"/proposals/{prop['id']}").status_code == 404
    assert p["outsider"].post(f"/proposals/{prop['id']}/approval", json={"decision": "approve"}).status_code == 404


def test_reject_is_final(ctx, raw_model):
    proj, p = ctx
    app = make_app(p["owner"], proj, raw_model)
    prop = propose(p["editor"], app["id"], with_note(raw_model)).json()
    r = p["editor"].post(f"/proposals/{prop['id']}/approval", json={"decision": "reject", "comment": "no thanks"})
    assert r.json()["status"] == "rejected" and r.json()["decision"]["comment"] == "no thanks"
    assert p["editor"].post(f"/proposals/{prop['id']}/approval", json={"decision": "approve"}).status_code == 409
    assert current_no(p["owner"], app["id"]) == 1


def test_risky_proposals_need_an_admin_and_acknowledgement(ctx, raw_model):
    proj, p = ctx
    app = make_app(p["owner"], proj, raw_model)
    risky = copy.deepcopy(raw_model)
    del risky["security"]["access"][1]["row_constraint"]
    prop = propose(p["editor"], app["id"], risky, "widen editor access").json()
    assert prop["approval_level"] == "A3"

    r = p["editor"].post(f"/proposals/{prop['id']}/approval", json={"decision": "approve", "acknowledge_risks": True})
    assert r.status_code == 403
    r = p["admin"].post(f"/proposals/{prop['id']}/approval", json={"decision": "approve"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "approval_required"
    assert r.json()["error"]["details"]["diff"]["changes"][0]["risks"] == ["permission-expansion"]
    assert current_no(p["owner"], app["id"]) == 1
    ok = p["admin"].post(f"/proposals/{prop['id']}/approval", json={"decision": "approve", "acknowledge_risks": True})
    assert ok.status_code == 200 and current_no(p["owner"], app["id"]) == 2


def test_stale_proposals_are_superseded(ctx, raw_model):
    proj, p = ctx
    app = make_app(p["owner"], proj, raw_model)
    a = propose(p["editor"], app["id"], with_note(raw_model), "a").json()
    other = copy.deepcopy(raw_model)
    other["modules"][0]["entities"][0]["fields"].append({"name": "memo", "type": "string"})
    b = propose(p["editor"], app["id"], other, "b").json()
    assert p["editor"].post(f"/proposals/{a['id']}/approval", json={"decision": "approve"}).status_code == 200
    r = p["editor"].post(f"/proposals/{b['id']}/approval", json={"decision": "approve"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "stale_proposal"
    assert p["viewer"].get(f"/proposals/{b['id']}").json()["status"] == "superseded"
    assert current_no(p["owner"], app["id"]) == 2
    assert p["owner"].get(f"/organizations/{proj['organization_id']}/audit-events?action_prefix=proposal.superseded").json()["total"] == 1


def test_first_revision_can_come_from_a_proposal(ctx, raw_model):
    proj, p = ctx
    app = make_app(p["owner"], proj)  # no model yet
    prop = propose(p["editor"], app["id"], raw_model, "initial").json()
    assert prop["base_revision_id"] is None and prop["approval_level"] == "A2"
    assert p["editor"].post(f"/proposals/{prop['id']}/approval", json={"decision": "approve"}).status_code == 200
    assert current_no(p["owner"], app["id"]) == 1


def test_invalid_and_empty_proposals_are_not_stored(ctx, raw_model):
    proj, p = ctx
    app = make_app(p["owner"], proj, raw_model)
    bad = copy.deepcopy(raw_model)
    bad["security"]["access"] = []
    r = propose(p["editor"], app["id"], bad)
    assert r.status_code == 422 and r.json()["error"]["code"] == "model_invalid"
    assert propose(p["editor"], app["id"], raw_model).json()["error"]["code"] == "no_changes"
    assert p["editor"].get(f"/applications/{app['id']}/proposals").json()["total"] == 0


def test_listing_filters_and_audit(ctx, raw_model):
    proj, p = ctx
    app = make_app(p["owner"], proj, raw_model)
    a = propose(p["editor"], app["id"], with_note(raw_model)).json()
    p["editor"].post(f"/proposals/{a['id']}/approval", json={"decision": "approve"})
    other = copy.deepcopy(raw_model)
    other["application"]["name"] = "Renamed"
    propose(p["editor"], app["id"], other)
    url = f"/applications/{app['id']}/proposals"
    assert p["viewer"].get(url).json()["total"] == 2
    assert [x["status"] for x in p["viewer"].get(url + "?status=pending").json()["items"]] == ["pending"]
    assert p["viewer"].get(url + "?status=bogus").status_code == 422
    events = p["admin"].get(f"/organizations/{proj['organization_id']}/audit-events?action_prefix=proposal.").json()
    assert {e["action"] for e in events["items"]} == {"proposal.created", "proposal.approved"}
