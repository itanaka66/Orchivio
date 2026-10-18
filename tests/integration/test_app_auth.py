"""End users of generated applications: management, sign-in, roles, plane separation, anonymous access."""

import copy

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_tenancy import add_member, make_org

from orchivio.db.models import AppSession, AuditEvent

PW = "temporary pass phrase 1"
NEW_PW = "a much better pass phrase 2"


class AppClient:
    """An end user's browser: own cookie jar, CSRF header after login."""

    def __init__(self, app, app_id):
        self.c = TestClient(app, raise_server_exceptions=False)
        self.base = f"/api/v1/apps/{app_id}"
        self.csrf = None

    def req(self, method, path, **kw):
        headers = dict(kw.pop("headers", {}))
        if self.csrf and "X-CSRF-Token" not in headers:
            headers["X-CSRF-Token"] = self.csrf
        return self.c.request(method, self.base + path, headers=headers, **kw)

    def get(self, p, **kw):
        return self.req("GET", p, **kw)

    def post(self, p, **kw):
        return self.req("POST", p, **kw)

    def patch(self, p, **kw):
        return self.req("PATCH", p, **kw)

    def delete(self, p, **kw):
        return self.req("DELETE", p, **kw)

    def login(self, username, password=PW):
        r = self.post("/auth/login", json={"username": username, "password": password})
        if r.status_code == 200:
            self.csrf = r.json()["csrf_token"]
        return r


@pytest.fixture
def world(new_api, raw_model, app):
    owner = new_api("owner@example.com")
    org = make_org(owner)
    proj = owner.post(f"/organizations/{org['id']}/projects", json={"name": "Inv", "slug": "inventory"}).json()
    editor = new_api("editor@example.com")
    add_member(owner, org, "editor@example.com", "editor")
    outsider = new_api("outsider@example.com")
    a = owner.post(f"/projects/{proj['id']}/applications", json={"key": "inventory", "name": "Inv", "model": raw_model}).json()
    owner.post(f"/applications/{a['id']}/preview/reset", json={})
    return a, {"owner": owner, "editor": editor, "outsider": outsider}, proj, raw_model


def add_user(owner, a, name, roles, password=PW):
    r = owner.post(f"/applications/{a['id']}/app-users", json={"username": name, "password": password, "roles": roles})
    assert r.status_code == 201, r.text
    return r.json()


def ready_client(app, owner, a, name, roles):
    """A signed-in user who has already replaced the temporary password."""
    add_user(owner, a, name, roles)
    c = AppClient(app, a["id"])
    assert c.login(name).status_code == 200
    assert c.post("/auth/change-password", json={"current_password": PW, "new_password": NEW_PW}).status_code == 204
    return c


# --- management --------------------------------------------------------------------------------


def test_user_management_is_admin_only_and_validated(world):
    a, p, proj, _ = world
    o = p["owner"]
    u = add_user(o, a, "alice", ["viewer"])
    assert u["must_change_password"] is True and u["roles"] == ["viewer"] and "password_hash" not in u and PW not in str(u)
    base = f"/applications/{a['id']}/app-users"
    assert o.post(base, json={"username": "alice", "password": PW}).json()["error"]["code"] == "username_taken"
    assert o.post(base, json={"username": "bob", "password": PW, "roles": ["wizard"]}).json()["error"]["code"] == "unknown_role"
    assert o.post(base, json={"username": "bob", "password": "short"}).json()["error"]["code"] == "weak_password"
    for bad in ("Alice2", "a", "has space", "-x"):
        assert o.post(base, json={"username": bad, "password": PW}).status_code == 422
    assert p["editor"].post(base, json={"username": "eve", "password": PW}).status_code == 403
    assert p["editor"].get(base).status_code == 403
    assert p["outsider"].get(base).status_code == 404
    assert o.get(base).json()["total"] == 1
    assert p["outsider"].get(f"/app-users/{u['id']}").status_code == 404
    patched = o.patch(f"/app-users/{u['id']}", json={"roles": ["editor", "viewer"], "display_name": "Alice"}).json()
    assert patched["roles"] == ["editor", "viewer"] and patched["display_name"] == "Alice"
    assert o.patch(f"/app-users/{u['id']}", json={"roles": ["wizard"]}).status_code == 422
    assert o.delete(f"/app-users/{u['id']}").status_code == 204
    assert o.get(f"/app-users/{u['id']}").status_code == 404


# --- sign-in -----------------------------------------------------------------------------------


def test_login_flow_and_forced_password_change(world, app):
    a, p, *_ = world
    add_user(p["owner"], a, "alice", ["viewer"])
    c = AppClient(app, a["id"])
    assert c.get("/auth/me").status_code == 401
    bad = c.login("alice", "wrong password 123")
    unknown = c.login("nobody", "wrong password 123")
    assert bad.status_code == unknown.status_code == 401 and bad.json() == unknown.json()  # no user enumeration
    ok = c.login("ALICE")  # usernames are case-insensitive
    assert ok.status_code == 200 and ok.json()["user"]["roles"] == ["viewer"]
    assert c.get("/auth/me").json()["must_change_password"] is True
    blocked = c.get("/data/core.Product")
    assert blocked.status_code == 403 and blocked.json()["error"]["code"] == "password_change_required"
    assert c.post("/auth/change-password", json={"current_password": "nope nope nope 1", "new_password": NEW_PW}).status_code == 401
    assert c.post("/auth/change-password", json={"current_password": PW, "new_password": "short"}).status_code == 422
    assert c.post("/auth/change-password", json={"current_password": PW, "new_password": PW}).status_code == 422
    assert c.post("/auth/change-password", json={"current_password": PW, "new_password": NEW_PW}).status_code == 204
    assert c.get("/data/core.Product").status_code == 200


def test_old_password_stops_working_after_change(world, app):
    a, p, *_ = world
    ready_client(app, p["owner"], a, "alice", ["viewer"])
    assert AppClient(app, a["id"]).login("alice", PW).status_code == 401
    assert AppClient(app, a["id"]).login("alice", NEW_PW).status_code == 200


def test_sessions_end_on_logout_disable_and_reset(world, app):
    a, p, *_ = world
    o = p["owner"]
    c = ready_client(app, o, a, "alice", ["viewer"])
    other = AppClient(app, a["id"])
    other.login("alice", NEW_PW)
    uid = o.get(f"/applications/{a['id']}/app-users").json()["items"][0]["id"]
    assert c.get("/auth/me").json()["anonymous"] is False
    token = c.c.cookies.get("orchivio_session_app", path=f"/api/v1/apps/{a['id']}")
    assert c.post("/auth/logout").status_code == 204
    assert c.c.cookies.get("orchivio_session_app", path=f"/api/v1/apps/{a['id']}") is None
    stale = AppClient(app, a["id"])
    stale.c.cookies.set("orchivio_session_app", token, path=f"/api/v1/apps/{a['id']}")
    assert stale.get("/auth/me").status_code == 401  # replaying the old cookie fails server-side

    assert other.get("/auth/me").status_code == 200
    o.patch(f"/app-users/{uid}", json={"status": "disabled"})
    assert other.get("/auth/me").status_code == 401
    assert AppClient(app, a["id"]).login("alice", NEW_PW).status_code == 401

    o.patch(f"/app-users/{uid}", json={"status": "active"})
    again = AppClient(app, a["id"])
    assert again.login("alice", NEW_PW).status_code == 200
    o.post(f"/app-users/{uid}/reset-password", json={"password": "reset pass phrase 3"})
    assert again.get("/auth/me").status_code == 401
    forced = AppClient(app, a["id"])
    forced.login("alice", "reset pass phrase 3")
    assert forced.get("/data/core.Product").json()["error"]["code"] == "password_change_required"


def test_failed_logins_are_throttled_per_user(world, app):
    a, p, *_ = world
    add_user(p["owner"], a, "alice", ["viewer"])
    add_user(p["owner"], a, "bob", ["viewer"])
    c = AppClient(app, a["id"])
    for _ in range(5):
        assert c.login("alice", "wrong password 123").status_code == 401
    locked = c.login("alice", PW)
    assert locked.status_code == 429 and "Retry-After" in locked.headers
    assert locked.json()["error"]["code"] == "too_many_attempts"


# --- authorization -----------------------------------------------------------------------------


def test_roles_decide_what_end_users_can_do(world, app):
    a, p, *_ = world
    o = p["owner"]
    viewer = ready_client(app, o, a, "vera", ["viewer"])
    editor = ready_client(app, o, a, "ed", ["editor"])
    admin = ready_client(app, o, a, "ada", ["admin"])
    row = {"name": "Widget", "sku": "w-1", "quantity": 3, "reorder_level": 5}
    assert viewer.post("/data/core.Product", json=row).status_code == 403
    made = editor.post("/data/core.Product", json=row)
    assert made.status_code == 201, made.text
    pid = made.json()["id"]
    assert viewer.get(f"/data/core.Product/{pid}").json()["sku"] == "w-1"
    assert viewer.get("/data/core.Product").json()["total"] == 1
    assert editor.delete(f"/data/core.Product/{pid}").status_code == 403
    assert admin.delete(f"/data/core.Product/{pid}").status_code == 204

    # a role change applies on the very next request, without signing in again
    uid = next(u["id"] for u in o.get(f"/applications/{a['id']}/app-users").json()["items"] if u["username"] == "ed")
    o.patch(f"/app-users/{uid}", json={"roles": ["viewer"]})
    assert editor.post("/data/core.Product", json={**row, "sku": "w-2"}).status_code == 403
    o.patch(f"/app-users/{uid}", json={"roles": []})
    assert editor.get("/data/core.Product").status_code == 403  # no role, no access (deny by default)


def test_flows_run_as_the_signed_in_user(world, app, raw_model):
    a, p, *_ = world
    o = p["owner"]
    admin = ready_client(app, o, a, "ada", ["admin"])
    viewer = ready_client(app, o, a, "vera", ["viewer"])
    admin.post("/data/core.Product", json={"name": "L", "sku": "low", "quantity": 1, "reorder_level": 5})
    denied = viewer.post("/flows/ReorderLowStock/run", json={"args": {"min_gap": 0}})
    assert denied.status_code == 200 and denied.json()["status"] == "failed"  # the flow's own writes are checked
    ran = admin.post("/flows/ReorderLowStock/run", json={"args": {"min_gap": 0}}).json()
    assert ran["status"] == "succeeded" and set(ran["trace"][0]) == {"step", "status"}
    from jsonschema import Draft202012Validator

    from orchivio.codegen import build_openapi
    from orchivio.model import AppModel

    doc = build_openapi(AppModel.model_validate(raw_model))
    for answer in (ran, denied.json()):  # the end-user answer fits the documented result schema
        assert list(Draft202012Validator({"$ref": "#/components/schemas/FlowRunResult", "components": doc["components"]}).iter_errors(answer)) == []
    assert admin.post("/flows/Nope/run", json={}).status_code == 404


# --- planes and CSRF ---------------------------------------------------------------------------


def test_studio_credentials_do_not_work_in_the_app_plane_and_vice_versa(world, app):
    a, p, *_ = world
    o = p["owner"]
    c = ready_client(app, o, a, "ada", ["admin"])
    studio_on_app = o.get(f"/apps/{a['id']}/data/core.Product")  # Studio cookie + CSRF on the app endpoint
    assert studio_on_app.status_code == 401
    tok = o.post("/tokens", json={"name": "t", "organization_id": o.get("/me").json()["organizations"][0]["organization_id"]}).json()["token"]
    bearer = TestClient(app, headers={"Authorization": f"Bearer {tok}"})
    assert bearer.get(f"/api/v1/apps/{a['id']}/data/core.Product").status_code == 401
    # the end user's session is not a Studio session
    studio = TestClient(app)
    studio.cookies.update(c.c.cookies)
    assert studio.get("/api/v1/me").status_code == 401
    assert c.c.get("/api/v1/me", headers={"Authorization": "Bearer oas_whatever"}).status_code == 401
    assert c.get("/data/core.Product").status_code == 200


def test_app_session_is_bound_to_its_application(world, app):
    a, p, proj, raw = world
    o = p["owner"]
    c = ready_client(app, o, a, "ada", ["admin"])
    token = c.c.cookies.get("orchivio_session_app", path=f"/api/v1/apps/{a['id']}")
    second = o.post(f"/projects/{proj['id']}/applications", json={"key": "other", "name": "Other", "model": raw}).json()
    o.post(f"/applications/{second['id']}/preview/reset", json={})
    thief = TestClient(app)
    thief.cookies.set("orchivio_session_app", token, path=f"/api/v1/apps/{second['id']}")
    assert thief.get(f"/api/v1/apps/{second['id']}/auth/me").status_code == 401


def test_unsafe_requests_need_the_csrf_token(world, app):
    a, p, *_ = world
    c = ready_client(app, p["owner"], a, "ed", ["editor"])
    row = {"name": "W", "sku": "w", "quantity": 1, "reorder_level": 1}
    assert c.post("/data/core.Product", json=row, headers={"X-CSRF-Token": "wrong"}).status_code == 403
    csrf, c.csrf = c.csrf, None
    r = c.post("/data/core.Product", json=row)
    assert r.status_code == 403 and r.json()["error"]["code"] == "csrf_failed"
    c.csrf = csrf
    assert c.post("/data/core.Product", json=row).status_code == 201


def test_unknown_or_unmodelled_applications_look_alike(world, app):
    a, p, *_ = world
    import uuid
    assert AppClient(app, uuid.uuid4()).get("/auth/me").status_code == 404
    assert AppClient(app, uuid.uuid4()).login("x", "y").status_code == 404


# --- anonymous access --------------------------------------------------------------------------


def anonymous_model(raw):
    m = copy.deepcopy(raw)
    m["security"]["roles"].append("anonymous")
    m["security"]["access"].append({"entity": "core.Product", "role": "anonymous", "operations": ["read"]})
    m["security"]["anonymous"] = True
    return m


def test_anonymous_access_is_off_by_default_and_read_only_when_granted(world, app):
    a, p, proj, raw = world
    o = p["owner"]
    assert AppClient(app, a["id"]).get("/data/core.Product").status_code == 401
    risky = o.post(f"/applications/{a['id']}/revisions", json={"model": anonymous_model(raw)})
    assert risky.status_code == 409 and risky.json()["error"]["code"] == "approval_required"  # A3, as designed
    ok = o.post(f"/applications/{a['id']}/revisions", json={"model": anonymous_model(raw), "acknowledge_risks": True})
    assert ok.status_code == 201, ok.text
    o.post(f"/applications/{a['id']}/preview/reset", json={})
    anon = AppClient(app, a["id"])
    assert anon.get("/auth/me").json() == {"anonymous": True, "roles": ["anonymous"]}
    assert anon.get("/data/core.Product").status_code == 200
    denied = anon.post("/data/core.Product", json={"name": "W", "sku": "w", "quantity": 1, "reorder_level": 1})
    assert denied.status_code == 403
    assert anon.post("/auth/logout").status_code == 401  # no account to sign out of
    assert anon.post("/auth/change-password", json={"current_password": "x", "new_password": NEW_PW}).status_code == 401


# --- audit -------------------------------------------------------------------------------------


def test_application_plane_events_are_audited_without_secrets(world, app):
    a, p, *_ = world
    add_user(p["owner"], a, "alice", ["viewer"])
    c = AppClient(app, a["id"])
    c.login("alice", "wrong password 123")
    c.login("alice", PW)
    with app.state.session_factory() as s:
        events = s.scalars(select(AuditEvent).order_by(AuditEvent.id)).all()
        sessions = s.scalars(select(AppSession)).all()
    app_events = [e for e in events if e.plane == "application"]
    assert [e.action for e in app_events] == ["appauth.login_failed", "appauth.login"]
    assert all(PW not in str(e.meta) and "wrong password" not in str(e.meta) for e in events)
    assert any(e.action == "appuser.created" and e.plane == "platform" for e in events)
    assert len(sessions) == 1 and sessions[0].token_hash and "oas_" not in sessions[0].token_hash
