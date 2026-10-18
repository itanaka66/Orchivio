"""Registration, login, sessions, CSRF, throttling, API tokens."""

from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from orchivio.api import create_app
from orchivio.api.deps import utcnow
from orchivio.api.settings import Settings
from orchivio.db.models import AuditEvent, User, UserSession
from tests_support import Api

GOOD = {"email": "alice@example.com", "password": "correct horse battery", "display_name": "Alice"}


def test_register_login_me_logout(app):
    api = Api(app)
    r = api.post("/auth/register", json=GOOD)
    assert r.status_code == 201 and set(r.json()) == {"id", "email", "display_name"}
    assert "password" not in r.text
    api.login("alice@example.com")
    me = api.get("/me").json()
    assert me["user"]["email"] == "alice@example.com" and me["auth"] == "session" and me["organizations"] == []
    assert api.post("/auth/logout").status_code == 204
    assert api.get("/me").status_code == 401


def test_register_rules(app):
    api = Api(app)
    assert api.post("/auth/register", json=GOOD).status_code == 201
    dup = api.post("/auth/register", json={**GOOD, "email": "ALICE@example.com"})
    assert dup.status_code == 409 and dup.json()["error"]["code"] == "email_taken"
    weak = api.post("/auth/register", json={**GOOD, "email": "b@example.com", "password": "short"})
    assert weak.status_code == 422 and weak.json()["error"]["code"] == "weak_password"
    assert api.post("/auth/register", json={**GOOD, "email": "not-an-email"}).status_code == 422
    assert api.post("/auth/register", json={**GOOD, "email": "c@example.com", "is_admin": True}).status_code == 422


def test_registration_can_be_disabled(db_url):
    app = create_app(Settings(env="dev", database_url=db_url, preview_database_url="sqlite:///:memory:", allow_registration=False))
    r = Api(app).post("/auth/register", json=GOOD)
    assert r.status_code == 403 and r.json()["error"]["code"] == "registration_disabled"


def test_passwords_are_stored_hashed(app, db_url):
    Api(app).post("/auth/register", json=GOOD)
    with app.state.session_factory() as s:
        user = s.scalar(select(User))
        assert user.password_hash.startswith("scrypt$") and GOOD["password"] not in user.password_hash


def test_login_failures_are_indistinguishable_and_audited(app):
    api = Api(app)
    api.post("/auth/register", json=GOOD)
    wrong = api.post("/auth/login", json={"email": GOOD["email"], "password": "wrong password here"})
    ghost = api.post("/auth/login", json={"email": "nobody@example.com", "password": "wrong password here"})
    assert wrong.status_code == ghost.status_code == 401 and wrong.json() == ghost.json()
    with app.state.session_factory() as s:
        assert s.scalars(select(AuditEvent.action).where(AuditEvent.action == "auth.login_failed")).all() == [
            "auth.login_failed"] * 2


def test_login_throttle_and_reset(db_url):
    app = create_app(Settings(env="dev", database_url=db_url, preview_database_url="sqlite:///:memory:", login_max_failures=3))
    api = Api(app)
    api.post("/auth/register", json=GOOD)
    bad = {"email": GOOD["email"], "password": "wrong password here"}
    assert [api.post("/auth/login", json=bad).status_code for _ in range(3)] == [401, 401, 401]
    r = api.post("/auth/login", json={**bad, "password": GOOD["password"]})  # even the right password
    assert r.status_code == 429 and r.headers["retry-after"]
    app.state.throttle._failures.clear()
    assert api.post("/auth/login", json={**bad, "password": GOOD["password"]}).status_code == 200


def test_cookie_attributes(app, db_url):
    Api(app).post("/auth/register", json=GOOD)
    login = {"email": GOOD["email"], "password": GOOD["password"]}
    cookie = Api(app).post("/auth/login", json=login).headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie and "path=/" in cookie and "secure" not in cookie
    prod = create_app(Settings(env="prod", database_url=db_url, preview_database_url="sqlite:///:memory:"))  # same database, secure cookies on
    r = TestClient(prod, base_url="https://testserver").post("/api/v1/auth/login", json=login)
    assert "secure" in r.headers["set-cookie"].lower()


def test_session_token_is_stored_only_as_a_hash(app):
    api = Api(app).signup("a@example.com")
    cookie = api.c.cookies.get("orchivio_session")
    with app.state.session_factory() as s:
        row = s.scalar(select(UserSession))
        assert cookie not in (row.token_hash, row.csrf_hash) and api.csrf not in (row.token_hash, row.csrf_hash)


def test_csrf_required_for_unsafe_methods_with_cookie_auth(app):
    api = Api(app).signup("a@example.com")
    body = {"name": "Org", "slug": "org-one"}
    assert api.post("/organizations", json=body, headers={"X-CSRF-Token": ""}).status_code == 403
    r = api.post("/organizations", json=body, headers={"X-CSRF-Token": "nope"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "csrf_failed"
    assert api.get("/organizations").status_code == 200  # safe methods need no token
    assert api.post("/organizations", json=body).status_code == 201


def test_logout_revokes_the_session_server_side(app):
    api = Api(app).signup("a@example.com")
    stolen = api.c.cookies.get("orchivio_session")
    api.post("/auth/logout")
    other = TestClient(app, cookies={"orchivio_session": stolen})
    assert other.get("/api/v1/me").status_code == 401


def test_expired_session_is_rejected(app):
    api = Api(app).signup("a@example.com")
    with app.state.session_factory() as s:
        s.scalar(select(UserSession)).expires_at = utcnow() - timedelta(seconds=1)
        s.commit()
    assert api.get("/me").status_code == 401


def test_disabled_user_loses_access_immediately(app):
    api = Api(app).signup("a@example.com")
    with app.state.session_factory() as s:
        s.scalar(select(User)).status = "disabled"
        s.commit()
    assert api.get("/me").status_code == 401
    assert Api(app).post("/auth/login", json={"email": "a@example.com", "password": Api.PASSWORD}).status_code == 401


# --- API tokens --------------------------------------------------------------


def _org_and_project(api, slug="acme"):
    org = api.post("/organizations", json={"name": "Acme", "slug": slug}).json()
    proj = api.post(f"/organizations/{org['id']}/projects", json={"name": "P", "slug": "proj"}).json()
    return org, proj


def _bearer(app, token):
    return TestClient(app, headers={"Authorization": f"Bearer {token}"})


def test_token_lifecycle(app):
    api = Api(app).signup("a@example.com")
    org, proj = _org_and_project(api)
    r = api.post("/tokens", json={"name": "ci", "organization_id": org["id"]})
    assert r.status_code == 201
    created = r.json()
    assert created["token"].startswith("orv_")
    assert "token" not in api.get("/tokens").json()["items"][0]

    bearer = _bearer(app, created["token"])  # no cookies, no CSRF header needed
    assert bearer.get(f"/api/v1/projects/{proj['id']}").status_code == 200
    assert bearer.get("/api/v1/me").json()["auth"] == "token"

    assert api.delete(f"/tokens/{created['id']}").status_code == 204
    assert bearer.get("/api/v1/me").status_code == 401


def test_tokens_cannot_mint_tokens_or_organizations(app):
    api = Api(app).signup("a@example.com")
    org, _ = _org_and_project(api)
    tok = api.post("/tokens", json={"name": "ci", "organization_id": org["id"]}).json()["token"]
    b = _bearer(app, tok)
    assert b.post("/api/v1/tokens", json={"name": "x", "organization_id": org["id"]}).json()["error"]["code"] == "session_required"
    assert b.post("/api/v1/organizations", json={"name": "N", "slug": "nn"}).status_code == 403


def test_token_scopes_and_validation(app):
    api = Api(app).signup("a@example.com")
    org, proj = _org_and_project(api)
    assert api.post("/tokens", json={"name": "x", "organization_id": org["id"], "scopes": ["bogus"]}).status_code == 422
    tok = api.post("/tokens", json={"name": "ro", "organization_id": org["id"], "scopes": ["project.read"]}).json()["token"]
    b = _bearer(app, tok)
    assert b.get(f"/api/v1/projects/{proj['id']}").status_code == 200
    r = b.patch(f"/api/v1/projects/{proj['id']}", json={"name": "New"})
    assert r.status_code == 403 and "scope" in r.json()["error"]["message"]


def test_token_bound_to_org_and_project(app):
    api = Api(app).signup("a@example.com")
    org, p1 = _org_and_project(api)
    p2 = api.post(f"/organizations/{org['id']}/projects", json={"name": "Q", "slug": "quux"}).json()
    other_org = api.post("/organizations", json={"name": "Other", "slug": "other"}).json()
    tok = api.post("/tokens", json={"name": "t", "organization_id": org["id"], "project_id": p1["id"]}).json()["token"]
    b = _bearer(app, tok)
    assert b.get(f"/api/v1/projects/{p1['id']}").status_code == 200
    assert b.get(f"/api/v1/projects/{p2['id']}").status_code == 403
    assert b.get(f"/api/v1/organizations/{other_org['id']}").status_code == 404
    assert api.post("/tokens", json={"name": "t", "organization_id": org["id"], "project_id": other_org["id"]}).status_code == 404


def test_expired_and_malformed_bearer_tokens(app):
    api = Api(app).signup("a@example.com")
    org, _ = _org_and_project(api)
    tok = api.post("/tokens", json={"name": "t", "organization_id": org["id"], "expires_in_days": 1}).json()["token"]
    from orchivio.db.models import ApiToken

    with app.state.session_factory() as s:
        s.scalar(select(ApiToken)).expires_at = utcnow() - timedelta(seconds=1)
        s.commit()
    assert _bearer(app, tok).get("/api/v1/me").status_code == 401
    for bad in ["Bearer", "Bearer nope", "Basic abc", "Bearer orv_" + "x" * 43]:
        assert TestClient(app).get("/api/v1/me", headers={"Authorization": bad}).status_code == 401


def test_bad_bearer_does_not_fall_back_to_cookie(app):
    api = Api(app).signup("a@example.com")
    assert api.get("/me", headers={"Authorization": "Bearer orv_wrong"}).status_code == 401


def test_users_cannot_revoke_each_others_tokens(app, new_api):
    a, b = new_api("a@example.com"), new_api("b@example.com")
    org, _ = _org_and_project(a)
    tid = a.post("/tokens", json={"name": "t", "organization_id": org["id"]}).json()["id"]
    assert b.delete(f"/tokens/{tid}").status_code == 404
    assert b.post("/tokens", json={"name": "t", "organization_id": org["id"]}).status_code == 404
