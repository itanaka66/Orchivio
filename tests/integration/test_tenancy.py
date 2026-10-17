"""Organizations, members, projects, RBAC and tenant isolation."""

import pytest


def make_org(api, slug="acme", name="Acme"):
    r = api.post("/organizations", json={"name": name, "slug": slug})
    assert r.status_code == 201, r.text
    return r.json()


def add_member(owner, org, email, role):
    r = owner.post(f"/organizations/{org['id']}/members", json={"email": email, "role": role})
    assert r.status_code == 201, r.text
    return r.json()


@pytest.fixture
def team(new_api):
    """acme with an owner, admin, editor and viewer, plus an outsider."""
    owner = new_api("owner@example.com")
    org = make_org(owner)
    people = {"owner": owner}
    for role in ("admin", "editor", "viewer"):
        people[role] = new_api(f"{role}@example.com")
        add_member(owner, org, f"{role}@example.com", role)
    people["outsider"] = new_api("outsider@example.com")
    return org, people


def test_creator_becomes_owner_and_orgs_are_listed_per_member(new_api):
    a, b = new_api("a@example.com"), new_api("b@example.com")
    org = make_org(a)
    assert org["role"] == "owner"
    assert [o["slug"] for o in a.get("/organizations").json()["items"]] == ["acme"]
    assert b.get("/organizations").json() == {"items": [], "limit": 50, "offset": 0, "total": 0}
    assert a.post("/organizations", json={"name": "X", "slug": "acme"}).json()["error"]["code"] == "slug_taken"
    assert a.post("/organizations", json={"name": "X", "slug": "Bad Slug"}).status_code == 422


def test_non_members_cannot_tell_an_organization_exists(team):
    org, p = team
    outsider = p["outsider"]
    missing = outsider.get("/organizations/00000000-0000-4000-8000-000000000000")
    hidden = outsider.get(f"/organizations/{org['id']}")
    assert missing.status_code == hidden.status_code == 404
    assert missing.json()["error"]["message"] == hidden.json()["error"]["message"]
    for path in ("members", "projects", "audit-events"):
        assert outsider.get(f"/organizations/{org['id']}/{path}").status_code == 404


def test_member_management_permissions(team):
    org, p = team
    base = f"/organizations/{org['id']}/members"
    assert {m["role"] for m in p["viewer"].get(base).json()["items"]} == {"owner", "admin", "editor", "viewer"}
    assert p["editor"].post(base, json={"email": "outsider@example.com", "role": "viewer"}).status_code == 403
    assert p["admin"].post(base, json={"email": "outsider@example.com", "role": "owner"}).status_code == 403
    assert p["admin"].post(base, json={"email": "outsider@example.com", "role": "editor"}).status_code == 201
    dup = p["admin"].post(base, json={"email": "outsider@example.com", "role": "editor"})
    assert dup.status_code == 409
    ghost = p["admin"].post(base, json={"email": "ghost@example.com", "role": "viewer"})
    assert ghost.status_code == 404 and ghost.json()["error"]["code"] == "user_not_found"
    assert p["outsider"].get(f"/organizations/{org['id']}").json()["role"] == "editor"


def test_only_owners_touch_the_owner_role_and_the_last_owner_stays(team):
    org, p = team
    owner_id = p["owner"].get("/me").json()["user"]["id"]
    admin_id = p["admin"].get("/me").json()["user"]["id"]
    m = f"/organizations/{org['id']}/members"
    assert p["admin"].patch(f"{m}/{owner_id}", json={"role": "viewer"}).status_code == 403
    assert p["admin"].delete(f"{m}/{owner_id}").status_code == 403
    assert p["admin"].patch(f"{m}/{admin_id}", json={"role": "owner"}).status_code == 403
    assert p["owner"].patch(f"{m}/{owner_id}", json={"role": "admin"}).json()["error"]["code"] == "last_owner"
    assert p["owner"].delete(f"{m}/{owner_id}").json()["error"]["code"] == "last_owner"
    assert p["owner"].patch(f"{m}/{admin_id}", json={"role": "owner"}).status_code == 200
    assert p["owner"].patch(f"{m}/{owner_id}", json={"role": "admin"}).status_code == 200  # two owners now


def test_members_can_leave_but_not_remove_others(team):
    org, p = team
    viewer_id = p["viewer"].get("/me").json()["user"]["id"]
    editor_id = p["editor"].get("/me").json()["user"]["id"]
    m = f"/organizations/{org['id']}/members"
    assert p["viewer"].delete(f"{m}/{editor_id}").status_code == 403
    assert p["viewer"].delete(f"{m}/{viewer_id}").status_code == 204
    assert p["viewer"].get(f"/organizations/{org['id']}").status_code == 404


def test_project_creation_needs_admin(team):
    org, p = team
    url = f"/organizations/{org['id']}/projects"
    body = {"name": "Inventory", "slug": "inventory"}
    assert p["editor"].post(url, json=body).status_code == 403
    assert p["viewer"].post(url, json=body).status_code == 403
    r = p["admin"].post(url, json=body)
    assert r.status_code == 201 and r.json()["organization_id"] == org["id"]
    assert p["admin"].post(url, json=body).json()["error"]["code"] == "slug_taken"
    assert [x["slug"] for x in p["viewer"].get(url).json()["items"]] == ["inventory"]


def test_same_project_slug_allowed_in_different_orgs(new_api):
    a, b = new_api("a@example.com"), new_api("b@example.com")
    oa, ob = make_org(a, "org-a"), make_org(b, "org-b")
    for api, org in ((a, oa), (b, ob)):
        assert api.post(f"/organizations/{org['id']}/projects", json={"name": "P", "slug": "shared"}).status_code == 201


def test_project_update_and_delete(team):
    org, p = team
    proj = p["admin"].post(f"/organizations/{org['id']}/projects", json={"name": "Inv", "slug": "inventory"}).json()
    url = f"/projects/{proj['id']}"
    assert p["editor"].patch(url, json={"name": "New"}).status_code == 403
    r = p["admin"].patch(url, json={"name": "New", "settings": {"theme": "dark"}})
    assert r.json()["name"] == "New" and r.json()["settings"] == {"theme": "dark"}
    assert p["admin"].patch(url, json={"slug": "other"}).status_code == 422  # slug is immutable here
    assert p["admin"].delete(f"{url}?confirm=inventory").status_code == 403  # owner only
    assert p["owner"].delete(url).json()["error"]["code"] == "confirmation_required"
    assert p["owner"].delete(f"{url}?confirm=wrong").status_code == 400
    assert p["owner"].delete(f"{url}?confirm=inventory").status_code == 204
    assert p["owner"].get(url).status_code == 404


def test_projects_are_isolated_between_organizations(new_api):
    a, b = new_api("a@example.com"), new_api("b@example.com")
    oa, ob = make_org(a, "org-a"), make_org(b, "org-b")
    pa = a.post(f"/organizations/{oa['id']}/projects", json={"name": "P", "slug": "proj-a"}).json()
    assert b.get(f"/projects/{pa['id']}").status_code == 404
    assert b.patch(f"/projects/{pa['id']}", json={"name": "x"}).status_code == 404
    assert b.delete(f"/projects/{pa['id']}?confirm=proj-a").status_code == 404
    assert b.post(f"/organizations/{oa['id']}/projects", json={"name": "P", "slug": "evil"}).status_code == 404
    assert b.get(f"/organizations/{ob['id']}/projects").json()["total"] == 0


def test_audit_log_is_admin_only_scoped_and_paginated(team):
    org, p = team
    url = f"/organizations/{org['id']}/audit-events"
    p["admin"].post(f"/organizations/{org['id']}/projects", json={"name": "P1", "slug": "proj-1"})
    assert p["editor"].get(url).status_code == 403 and p["viewer"].get(url).status_code == 403
    page = p["admin"].get(url + "?limit=2").json()
    assert page["limit"] == 2 and len(page["items"]) == 2 and page["total"] >= 5
    assert page["items"][0]["id"] > page["items"][1]["id"]  # newest first
    only = p["admin"].get(url + "?action_prefix=member.").json()
    assert only["total"] == 3 and {e["action"] for e in only["items"]} == {"member.added"}
    assert all(e["plane"] == "platform" and e["channel"] == "api" and e["metadata"]["request_id"] for e in page["items"])
    assert p["admin"].get(url + "?limit=1000").status_code == 422
    # another organization's events never appear
    other = p["outsider"]
    org2 = make_org(other, "other-org")
    assert all(e["action"].startswith("organization.") for e in other.get(f"/organizations/{org2['id']}/audit-events").json()["items"])
