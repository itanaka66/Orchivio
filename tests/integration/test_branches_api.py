"""Branches and three-way merge over HTTP: lifecycle, clean merge, conflicts, gating, isolation."""

import pytest
from test_revisions_api import create_app
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
    people["outsider"] = new_api("outsider@example.com")
    app = create_app(people["editor"], proj, raw_model).json()
    return app, people


def head(api, app_id, branch_id=None):
    if branch_id is None:
        rid = api.get(f"/applications/{app_id}").json()["current_revision_id"]
    else:
        rid = next(b for b in api.get(f"/applications/{app_id}/branches").json() if b["id"] == branch_id)["head_revision_id"]
    return api.get(f"/revisions/{rid}").json()["model_json"]


def entity(model, name):
    return next(e for m in model["modules"] for e in m["entities"] if e["name"] == name)


def field(ent, name):
    return next(f for f in ent["fields"] if f["name"] == name)


def commit(api, app_id, model, branch="main", **kw):
    return api.post(f"/applications/{app_id}/revisions", json={"model": model, "branch": branch, **kw})


def new_branch(api, app_id, name="feature-x"):
    r = api.post(f"/applications/{app_id}/branches", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()


def test_branch_lifecycle_and_validation(ctx):
    app, p = ctx
    listed = p["viewer"].get(f"/applications/{app['id']}/branches").json()
    assert [b["name"] for b in listed] == ["main"]
    b = new_branch(p["editor"], app["id"])
    assert b["status"] == "open" and b["head_revision_id"] == app["current_revision_id"]
    assert p["editor"].post(f"/applications/{app['id']}/branches", json={"name": "feature-x"}).json()["error"]["code"] == "branch_exists"
    for bad in ("Upper", "-x", "a..b", "trail/", "has space"):
        r = p["editor"].post(f"/applications/{app['id']}/branches", json={"name": bad})
        assert r.status_code == 422, bad
    assert p["viewer"].post(f"/applications/{app['id']}/branches", json={"name": "v"}).status_code == 403
    assert p["outsider"].get(f"/applications/{app['id']}/branches").status_code == 404
    assert p["editor"].delete(f"/branches/{b['id']}").status_code == 204
    after = p["editor"].get(f"/applications/{app['id']}/branches").json()
    assert {x["name"]: x["status"] for x in after} == {"main": "open", "feature-x": "abandoned"}
    # a closed branch accepts no revisions and no merges
    model = head(p["editor"], app["id"])
    assert commit(p["editor"], app["id"], model, "feature-x").json()["error"]["code"] in ("branch_closed", "no_changes")
    assert p["editor"].post(f"/branches/{b['id']}/merge", json={}).json()["error"]["code"] == "branch_closed"


def test_main_cannot_be_abandoned_or_merged_into_itself(ctx):
    app, p = ctx
    main = next(b for b in p["editor"].get(f"/applications/{app['id']}/branches").json() if b["name"] == "main")
    assert p["editor"].delete(f"/branches/{main['id']}").json()["error"]["code"] == "main_protected"
    assert p["editor"].post(f"/branches/{main['id']}/merge", json={"into": "main"}).status_code == 422


def test_branch_revisions_do_not_move_main(ctx):
    app, p = ctx
    b = new_branch(p["editor"], app["id"])
    m = head(p["editor"], app["id"])
    field(entity(m, "Product"), "name")["display_name"] = "Title"
    r = commit(p["editor"], app["id"], m, "feature-x")
    assert r.status_code == 201 and r.json()["parent_revision_ids"] == [app["current_revision_id"]]
    assert p["editor"].get(f"/applications/{app['id']}").json()["current_revision_id"] == app["current_revision_id"]
    assert field(entity(head(p["editor"], app["id"]), "Product"), "name").get("display_name") != "Title"
    assert commit(p["editor"], app["id"], m, "nope").status_code == 404
    assert b["id"]


def test_clean_merge_of_independent_changes(ctx):
    app, p = ctx
    ed = p["editor"]
    b = new_branch(ed, app["id"])
    m = head(ed, app["id"])
    field(entity(m, "Product"), "name")["display_name"] = "Title"
    assert commit(ed, app["id"], m, "feature-x").status_code == 201
    mm = head(ed, app["id"])  # main changes a different element meanwhile
    entity(mm, "Product")["display_name"] = "Item"
    assert commit(ed, app["id"], mm).status_code == 201

    dry = ed.post(f"/branches/{b['id']}/merge", json={"dry_run": True})
    assert dry.status_code == 200 and dry.json()["status"] == "clean" and dry.json()["revision_id"] is None
    assert len(ed.get(f"/applications/{app['id']}/revisions").json()["items"]) == 3  # nothing written

    done = ed.post(f"/branches/{b['id']}/merge", json={})
    assert done.status_code == 201, done.text
    body = done.json()
    assert body["status"] == "merged" and body["revision_no"] == 4
    merged = head(ed, app["id"])
    assert entity(merged, "Product")["display_name"] == "Item"
    assert field(entity(merged, "Product"), "name")["display_name"] == "Title"
    rev = ed.get(f"/revisions/{body['revision_id']}").json()
    assert len(rev["parent_revision_ids"]) == 2
    assert next(x for x in ed.get(f"/applications/{app['id']}/branches").json() if x["id"] == b["id"])["status"] == "merged"
    # merged branches are closed
    assert ed.post(f"/branches/{b['id']}/merge", json={}).json()["error"]["code"] == "branch_closed"


def test_merge_up_to_date_when_nothing_new(ctx):
    app, p = ctx
    b = new_branch(p["editor"], app["id"])
    r = p["editor"].post(f"/branches/{b['id']}/merge", json={})
    assert r.status_code == 200 and r.json()["status"] == "up_to_date"


def test_conflict_is_reported_then_resolved(ctx):
    app, p = ctx
    ed = p["editor"]
    b = new_branch(ed, app["id"])
    m = head(ed, app["id"])
    entity(m, "Product")["display_name"] = "Theirs"
    commit(ed, app["id"], m, "feature-x")
    mm = head(ed, app["id"])
    entity(mm, "Product")["display_name"] = "Ours"
    commit(ed, app["id"], mm)

    r = ed.post(f"/branches/{b['id']}/merge", json={})
    assert r.status_code == 409 and r.json()["error"]["code"] == "merge_conflicts"
    conflicts = r.json()["error"]["details"]
    assert len(conflicts) == 1 and conflicts[0]["kind"] == "property" and conflicts[0]["property"] == "display_name"
    assert conflicts[0]["ours"] == "Ours" and conflicts[0]["theirs"] == "Theirs"
    assert len(ed.get(f"/applications/{app['id']}/revisions").json()["items"]) == 3  # still nothing merged

    bad = ed.post(f"/branches/{b['id']}/merge", json={"resolutions": {conflicts[0]["id"]: "banana"}})
    assert bad.status_code == 409  # an invalid choice does not count as a resolution

    ok = ed.post(f"/branches/{b['id']}/merge", json={"resolutions": {conflicts[0]["id"]: "theirs"}})
    assert ok.status_code == 201 and ok.json()["conflicts_resolved"] == 1
    assert entity(head(ed, app["id"]), "Product")["display_name"] == "Theirs"


def test_viewer_and_outsider_cannot_merge(ctx):
    app, p = ctx
    b = new_branch(p["editor"], app["id"])
    assert p["viewer"].post(f"/branches/{b['id']}/merge", json={}).status_code == 403
    assert p["outsider"].post(f"/branches/{b['id']}/merge", json={}).status_code == 404
    assert p["viewer"].delete(f"/branches/{b['id']}").status_code == 403
    assert p["outsider"].delete(f"/branches/{b['id']}").status_code == 404


def test_risky_merge_into_main_needs_acknowledgement(ctx):
    app, p = ctx
    ed = p["editor"]
    m = head(ed, app["id"])
    entity(m, "Product")["fields"].append({"name": "note", "type": "text"})
    assert commit(ed, app["id"], m).status_code == 201
    b = new_branch(ed, app["id"])
    m = head(ed, app["id"])
    ent = entity(m, "Product")
    ent["fields"] = [f for f in ent["fields"] if f["name"] != "note"]  # dropping a field is destructive (A3)
    r = commit(ed, app["id"], m, "feature-x")
    assert r.status_code == 201, r.text  # branches take risky changes freely; the gate is at main
    first = ed.post(f"/branches/{b['id']}/merge", json={})
    assert first.status_code == 409 and first.json()["error"]["code"] == "approval_required"
    assert len(ed.get(f"/applications/{app['id']}/revisions").json()["items"]) == 3
    ok = ed.post(f"/branches/{b['id']}/merge", json={"acknowledge_risks": True})
    assert ok.status_code == 201 and ok.json()["level"] == "A3"
    assert all(f["name"] != "note" for f in entity(head(ed, app["id"]), "Product")["fields"])
