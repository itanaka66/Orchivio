"""OIDC sign-in for application users, against a real local identity provider."""

import base64
import hashlib
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa, utils
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_app_auth import AppClient, add_user
from test_tenancy import add_member, make_org

from orchivio.db.models import AppUser, AppUserIdentity, AppUserRole, AuditEvent, OidcLoginState
from orchivio.integrations import PinnedTransport, check_url, destination_policy
from orchivio.integrations.oidc import OidcClient

CLIENT_ID = "orchivio-test-client"
SECRET = "client-secret-value"


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def jint(n: int) -> str:
    return b64(n.to_bytes((n.bit_length() + 7) // 8, "big"))


RSA_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_RSA = rsa.generate_private_key(public_exponent=65537, key_size=2048)
EC_KEY = ec.generate_private_key(ec.SECP256R1())


def jwk_rsa(key, kid):
    nums = key.public_key().public_numbers()
    return {"kty": "RSA", "kid": kid, "use": "sig", "alg": "RS256", "n": jint(nums.n), "e": jint(nums.e)}


def jwk_ec(key, kid):
    nums = key.public_key().public_numbers()
    return {"kty": "EC", "kid": kid, "use": "sig", "crv": "P-256", "x": jint(nums.x), "y": jint(nums.y)}


def sign_jwt(claims, key=RSA_KEY, alg="RS256", kid="k1", header=None):
    h = {"alg": alg, "typ": "JWT", **({"kid": kid} if kid else {}), **(header or {})}
    signing = f"{b64(json.dumps(h).encode())}.{b64(json.dumps(claims).encode())}"
    if alg == "RS256":
        sig = key.sign(signing.encode(), padding.PKCS1v15(), hashes.SHA256())
    elif alg == "ES256":
        r, s = utils.decode_dss_signature(key.sign(signing.encode(), ec.ECDSA(hashes.SHA256())))
        sig = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    elif alg == "none":
        sig = b""
    else:  # HS256 with the RSA public numbers as the "secret": the classic algorithm-confusion attempt
        import hmac
        sig = hmac.new(str(RSA_KEY.public_key().public_numbers().n).encode(), signing.encode(), hashlib.sha256).digest()
    return f"{signing}.{b64(sig)}"


class Idp:
    """Just enough of an OpenID provider: discovery, keys and a token endpoint that checks PKCE and client auth."""

    def __init__(self):
        self.codes, self.requests = {}, []
        self.keys = [jwk_rsa(RSA_KEY, "k1"), jwk_ec(EC_KEY, "ec1")]
        self.issuer_override = None
        self.token_status = 200
        idp = self

        class H(BaseHTTPRequestHandler):
            def _json(self, status, obj):
                data = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):  # noqa: N802
                idp.requests.append(("GET", self.path))
                if self.path == "/.well-known/openid-configuration":
                    base = idp.base
                    self._json(200, {"issuer": idp.issuer_override or base, "authorization_endpoint": f"{base}/authorize",
                                     "token_endpoint": f"{base}/token", "jwks_uri": f"{base}/jwks"})
                elif self.path == "/jwks":
                    self._json(200, {"keys": idp.keys})
                else:
                    self._json(404, {})

            def do_POST(self):  # noqa: N802
                form = parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode())
                form = {k: v[0] for k, v in form.items()}
                idp.requests.append(("POST", self.path, form))
                pending = idp.codes.pop(form.get("code"), None)
                ok = (idp.token_status == 200 and pending is not None and form.get("client_id") == CLIENT_ID and form.get("client_secret") == SECRET
                      and b64(hashlib.sha256(form.get("code_verifier", "").encode()).digest()) == pending["challenge"]
                      and form.get("redirect_uri") == pending["redirect_uri"])
                self._json(200 if ok else 400, {"id_token": pending["token"]} if ok else {"error": "invalid_grant"})

            def log_message(self, *a):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), H)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def claims(self, **over):
        now = int(time.time())
        c = {"iss": self.base, "aud": CLIENT_ID, "sub": "user-1", "exp": now + 300, "iat": now, "email": "pat@example.com",
             "email_verified": True, "name": "Pat", "groups": ["staff"]}
        c.update(over)
        return {k: v for k, v in c.items() if v is not None}

    def stage(self, authorize_url, claims_fn=None, token=None, **claim_over):
        """Register the code the provider would hand back for this authorization request; returns the code."""
        q = {k: v[0] for k, v in parse_qs(urlparse(authorize_url).query).items()}
        claims = self.claims(nonce=q["nonce"], **claim_over)
        code = f"code-{len(self.codes) + 1}-{q['state'][:6]}"
        self.codes[code] = {"challenge": q["code_challenge"], "redirect_uri": q["redirect_uri"], "token": token or sign_jwt(claims)}
        return code, q["state"]

    def close(self):
        self.server.shutdown()


@pytest.fixture
def idp():
    p = Idp()
    yield p
    p.close()


@pytest.fixture
def world(new_api, raw_model, app, idp, monkeypatch):
    monkeypatch.setenv("ORCHIVIO_TEST_OIDC_SECRET", SECRET)
    owner = new_api("owner@example.com")
    org = make_org(owner)
    proj = owner.post(f"/organizations/{org['id']}/projects", json={"name": "Inv", "slug": "inventory"}).json()
    editor = new_api("editor@example.com")
    add_member(owner, org, "editor@example.com", "editor")
    outsider = new_api("outsider@example.com")
    a = owner.post(f"/projects/{proj['id']}/applications", json={"key": "inventory", "name": "Inv", "model": raw_model}).json()
    owner.post(f"/applications/{a['id']}/preview/reset", json={})
    return a, owner, editor, outsider, raw_model


def allow_local(app):
    app.state.settings.webhook_allow_http = True
    app.state.settings.webhook_allow_private_networks = True
    policy = destination_policy(app.state.settings)
    app.state.mcp_transport = PinnedTransport(policy)
    app.state.oidc = OidcClient(app.state.mcp_transport, check_endpoint=lambda u: check_url(u, policy))


def configure(owner, a, idp, **over):
    body = {"issuer": idp.base, "client_id": CLIENT_ID, "client_secret_ref": "env:ORCHIVIO_TEST_OIDC_SECRET", "auto_provision": True,
            "role_mapping": {"staff": "viewer", "stock": "editor", "boss": "admin"}, **over}
    r = owner.put(f"/applications/{a['id']}/oidc", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def browser(app, a):
    c = TestClient(app, follow_redirects=False)
    c.base = f"/api/v1/apps/{a['id']}"
    return c


def start(c, idp, **q):
    qs = "&".join(f"{k}={v}" for k, v in q.items())
    r = c.get(f"{c.base}/auth/oidc/login" + (f"?{qs}" if qs else ""))
    assert r.status_code == 302, r.text
    return r.headers["location"]


def finish(c, idp, url, **kw):
    code, state = idp.stage(url, **kw)
    return c.get(f"{c.base}/auth/oidc/callback?code={code}&state={state}")


def sign_in(app, a, idp, **kw):
    c = browser(app, a)
    r = finish(c, idp, start(c, idp), **kw)
    return c, r


def me(c):
    return c.get(f"{c.base}/auth/me").json()


# --- configuration -----------------------------------------------------------------------------


def test_configuration_is_validated_and_admin_only(world, idp, app):
    a, owner, editor, outsider, _ = world
    url = f"/applications/{a['id']}/oidc"
    good = {"issuer": "https://login.example.com", "client_id": "c", "role_mapping": {"x": "viewer"}}
    bad = [({"issuer": "http://login.example.com"}, "destination_refused"), ({"issuer": "https://169.254.169.254"}, "destination_refused"),
           ({"issuer": "https://login.example.com/"}, "invalid_issuer"), ({"scopes": "email"}, "invalid_scopes"),
           ({"post_login_url": "//evil.example"}, "invalid_redirect"), ({"post_login_url": "javascript:alert(1)"}, "invalid_redirect"),
           ({"role_mapping": {"x": "wizard"}}, "unknown_role"), ({"default_roles": ["ghost"]}, "unknown_role"),
           ({"client_secret_ref": "vault:x"}, "invalid_secret_ref"), ({"client_secret_ref": "env:NOT_SET_ANYWHERE_8"}, "secret_unavailable"),
           ({"allowed_email_domains": ["bad domain"]}, "invalid_domains"), ({"allowed_email_domains": []}, "invalid_domains")]
    for patch, code in bad:
        r = owner.put(url, json={**good, **patch})
        assert r.status_code == 422 and r.json()["error"]["code"] == code, (patch, r.text)
    assert owner.put(url, json={**good, "bogus": 1}).status_code == 422
    assert editor.put(url, json=good).status_code == 403
    assert outsider.get(url).status_code == 404
    ok = owner.put(url, json=good)
    assert ok.status_code == 200 and ok.json()["login_path"] == f"/api/v1/apps/{a['id']}/auth/oidc/login" and "secret" not in json.dumps(ok.json()).replace("client_secret_ref", "")
    assert owner.get(url).json()["issuer"] == "https://login.example.com"
    assert owner.delete(url).status_code == 204 and owner.get(url).status_code == 404


def test_the_test_endpoint_checks_discovery_and_keys(world, idp, app):
    a, owner, *_ = world
    allow_local(app)
    configure(owner, a, idp)
    r = owner.post(f"/applications/{a['id']}/oidc/test")
    assert r.status_code == 200 and r.json()["token_endpoint"] == f"{idp.base}/token" and r.json()["redirect_uri"].endswith("/auth/oidc/callback")
    idp.issuer_override = "https://someone-else.example"
    app.state.oidc._meta.clear()
    bad = owner.post(f"/applications/{a['id']}/oidc/test")
    assert bad.status_code == 502 and bad.json()["error"]["code"] == "issuer_mismatch"


# --- the happy path ----------------------------------------------------------------------------


def test_sign_in_provisions_an_account_and_maps_roles(world, idp, app):
    a, owner, *_ = world
    allow_local(app)
    configure(owner, a, idp, post_login_url="/home")
    c, r = sign_in(app, a, idp, groups=["staff", "stock", "unmapped"])
    assert r.status_code == 302 and r.headers["location"] == "/home"
    who = me(c)
    assert who["username"] == "pat@example.com" and who["roles"] == ["editor", "viewer"] and who["display_name"] == "Pat" and who["must_change_password"] is False
    assert any("orchivio_session_app" in h for h in r.headers.get_list("set-cookie"))
    token = c.get(f"{c.base}/auth/csrf").json()["csrf_token"]
    assert c.get(f"{c.base}/data/core.Product").status_code == 200
    created = c.post(f"{c.base}/data/core.Product", json={"name": "N", "sku": "n-1", "quantity": 1, "reorder_level": 1}, headers={"X-CSRF-Token": token})
    assert created.status_code == 201, created.text
    assert c.post(f"{c.base}/data/core.Product", json={}, headers={"X-CSRF-Token": "old"}).status_code == 403
    with app.state.session_factory() as s:
        user = s.scalar(select(AppUser).where(AppUser.username == "pat@example.com"))
        assert user.password_hash is None and s.scalar(select(AppUserIdentity)).subject == "user-1"
        assert {e.action for e in s.scalars(select(AuditEvent))} >= {"appauth.oidc_login"}
        assert "pat@example.com" not in json.dumps([e.meta for e in s.scalars(select(AuditEvent)) if e.action == "appauth.oidc_login"])
    # a federated account has no password to guess
    assert AppClient(app, a["id"]).login("pat@example.com", "anything at all 123").status_code == 401


def test_every_sign_in_reapplies_the_providers_roles(world, idp, app):
    a, owner, *_ = world
    allow_local(app)
    configure(owner, a, idp, default_roles=["viewer"])
    c1, _ = sign_in(app, a, idp, groups=["boss"])
    assert me(c1)["roles"] == ["admin", "viewer"]
    c2, _ = sign_in(app, a, idp, groups=[])  # demoted at the provider
    assert me(c2)["roles"] == ["viewer"]
    assert me(c1)["roles"] == ["viewer"]  # roles are read on every request, so the old session follows
    with app.state.session_factory() as s:
        assert s.scalar(select(AppUser.id).where(AppUser.username == "pat@example.com")) is not None
        assert len(list(s.scalars(select(AppUserIdentity)))) == 1  # same person, same account


def test_unknown_people_are_not_provisioned_unless_allowed(world, idp, app):
    a, owner, *_ = world
    allow_local(app)
    configure(owner, a, idp, auto_provision=False)
    c, r = sign_in(app, a, idp)
    assert r.status_code == 403 and r.json()["error"]["code"] == "no_account"
    with app.state.session_factory() as s:
        assert s.scalar(select(AppUser.id)) is None


def test_a_disabled_account_cannot_sign_in(world, idp, app):
    a, owner, *_ = world
    allow_local(app)
    configure(owner, a, idp)
    sign_in(app, a, idp)
    uid = owner.get(f"/applications/{a['id']}/app-users").json()["items"][0]["id"]
    owner.patch(f"/app-users/{uid}", json={"status": "disabled"})
    c, r = sign_in(app, a, idp)
    assert r.status_code == 403 and r.json()["error"]["code"] == "account_disabled"


def test_email_domain_allowlist_needs_a_verified_address(world, idp, app):
    a, owner, *_ = world
    allow_local(app)
    configure(owner, a, idp, allowed_email_domains=["example.com"])
    assert sign_in(app, a, idp)[1].status_code == 302
    for over in ({"email": "pat@evil.test"}, {"email_verified": False}, {"email": None}, {"email": "pat@example.com.evil.test"}):
        r = sign_in(app, a, idp, sub=f"u-{len(over)}-{list(over)[0]}", **over)[1]
        assert r.status_code == 403 and r.json()["error"]["code"] == "domain_not_allowed", over


def test_a_provider_cannot_take_over_a_local_account_by_claiming_its_name(world, idp, app):
    a, owner, *_ = world
    allow_local(app)
    add_user(owner, a, "pat@example.com", ["admin"])  # a local account with the same name the provider will claim
    configure(owner, a, idp)
    c, r = sign_in(app, a, idp, groups=["staff"])
    assert r.status_code == 302
    who = me(c)
    assert who["username"] != "pat@example.com" and who["username"].startswith("pat@example.com-") and who["roles"] == ["viewer"]
    with app.state.session_factory() as s:
        local = s.scalar(select(AppUser).where(AppUser.username == "pat@example.com"))
        assert local.password_hash is not None
        assert [r.role_key for r in s.scalars(select(AppUserRole).where(AppUserRole.app_user_id == local.id))] == ["admin"]  # untouched


# --- hostile input -----------------------------------------------------------------------------


def attempt(app, a, idp, **kw):
    return sign_in(app, a, idp, **kw)[1]


@pytest.mark.parametrize("over, code", [
    ({"nonce": "somebody-elses"}, "invalid_token"), ({"aud": "another-client"}, "invalid_token"), ({"iss": "https://evil.example"}, "invalid_token"),
    ({"exp": int(time.time()) - 3600}, "token_expired"), ({"iat": int(time.time()) + 3600}, "invalid_token"), ({"nbf": int(time.time()) + 3600}, "invalid_token"),
    ({"aud": [CLIENT_ID, "other"]}, "invalid_token"), ({"sub": ""}, "invalid_token"), ({"sub": None}, "invalid_token"), ({"exp": None}, "invalid_token"),
])
def test_claims_are_checked(world, idp, app, over, code):
    a, owner, *_ = world
    allow_local(app)
    configure(owner, a, idp)
    c = browser(app, a)
    url = start(c, idp)
    q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
    claims = idp.claims(**({"nonce": q["nonce"]} | over)) if "nonce" not in over else idp.claims(**over)
    code_, state = idp.stage(url, token=sign_jwt(claims))
    r = c.get(f"{c.base}/auth/oidc/callback?code={code_}&state={state}")
    assert r.status_code == 401 and r.json()["error"]["code"] == code, r.text
    assert "orchivio_session_app" not in " ".join(r.headers.get_list("set-cookie"))  # no session for a token that fails
    with app.state.session_factory() as s:
        assert s.scalar(select(AppUser.id)) is None  # nothing is created for a token that does not verify


def test_an_audience_list_needs_a_matching_azp(world, idp, app):
    a, owner, *_ = world
    allow_local(app)
    configure(owner, a, idp)
    c = browser(app, a)
    url = start(c, idp)
    nonce = parse_qs(urlparse(url).query)["nonce"][0]
    code, state = idp.stage(url, token=sign_jwt(idp.claims(nonce=nonce, aud=[CLIENT_ID, "other"], azp=CLIENT_ID)))
    assert c.get(f"{c.base}/auth/oidc/callback?code={code}&state={state}").status_code == 302


@pytest.mark.parametrize("name, make", [
    ("alg none", lambda claims: sign_jwt(claims, alg="none")),
    ("hmac confusion", lambda claims: sign_jwt(claims, alg="HS256")),
    ("wrong key", lambda claims: sign_jwt(claims, key=OTHER_RSA)),
    ("unknown kid", lambda claims: sign_jwt(claims, kid="ghost")),
    ("garbled signature", lambda claims: sign_jwt(claims)[:-6] + "AAAAAA"),
    ("tampered payload", lambda claims: sign_jwt(claims).split(".")[0] + "." + b64(json.dumps({**claims, "sub": "admin"}).encode()) + "." + sign_jwt(claims).split(".")[2]),
])
def test_forged_tokens_are_refused(world, idp, app, name, make):
    a, owner, *_ = world
    allow_local(app)
    configure(owner, a, idp)
    c = browser(app, a)
    url = start(c, idp)
    nonce = parse_qs(urlparse(url).query)["nonce"][0]
    code, state = idp.stage(url, token=make(idp.claims(nonce=nonce)))
    r = c.get(f"{c.base}/auth/oidc/callback?code={code}&state={state}")
    assert r.status_code == 401 and r.json()["error"]["code"] == "invalid_token", (name, r.text)
    assert me_anonymous_denied(c)


def me_anonymous_denied(c):
    return c.get(f"{c.base}/auth/me").status_code == 401


def test_es256_tokens_work_and_keys_may_rotate(world, idp, app):
    a, owner, *_ = world
    allow_local(app)
    configure(owner, a, idp)
    c = browser(app, a)
    url = start(c, idp)
    nonce = parse_qs(urlparse(url).query)["nonce"][0]
    code, state = idp.stage(url, token=sign_jwt(idp.claims(nonce=nonce), key=EC_KEY, alg="ES256", kid="ec1"))
    assert c.get(f"{c.base}/auth/oidc/callback?code={code}&state={state}").status_code == 302
    # the provider rotates to a key the cache has not seen: one refresh finds it
    new_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    idp.keys = [jwk_rsa(new_key, "k2")]
    app.state.oidc._keys[next(iter(app.state.oidc._keys))] = (time.time(), 0.0, app.state.oidc._keys[next(iter(app.state.oidc._keys))][2])
    c2 = browser(app, a)
    url2 = start(c2, idp)
    nonce2 = parse_qs(urlparse(url2).query)["nonce"][0]
    code2, state2 = idp.stage(url2, token=sign_jwt(idp.claims(nonce=nonce2, sub="user-2"), key=new_key, kid="k2"))
    assert c2.get(f"{c2.base}/auth/oidc/callback?code={code2}&state={state2}").status_code == 302


def test_small_rsa_keys_are_refused(world, idp, app):
    a, owner, *_ = world
    allow_local(app)
    small = rsa.generate_private_key(public_exponent=65537, key_size=1024)
    idp.keys = [jwk_rsa(small, "k1")]
    configure(owner, a, idp)
    c = browser(app, a)
    url = start(c, idp)
    nonce = parse_qs(urlparse(url).query)["nonce"][0]
    code, state = idp.stage(url, token=sign_jwt(idp.claims(nonce=nonce), key=small))
    r = c.get(f"{c.base}/auth/oidc/callback?code={code}&state={state}")
    assert r.status_code == 401 and "too small" in r.json()["error"]["message"]


def test_state_is_single_use_bound_to_the_browser_and_expires(world, idp, app):
    a, owner, *_ = world
    allow_local(app)
    configure(owner, a, idp)
    c = browser(app, a)
    url = start(c, idp)
    code, state = idp.stage(url)
    assert c.get(f"{c.base}/auth/oidc/callback?code={code}&state={state}").status_code == 302
    replay = c.get(f"{c.base}/auth/oidc/callback?code={code}&state={state}")
    assert replay.status_code == 400 and replay.json()["error"]["code"] == "invalid_state"

    victim = browser(app, a)
    url = start(victim, idp)
    code, state = idp.stage(url)
    attacker = browser(app, a)  # a different browser carrying the victim's state: login CSRF / fixation
    r = attacker.get(f"{attacker.base}/auth/oidc/callback?code={code}&state={state}")
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_state" and me_anonymous_denied(attacker)

    c3 = browser(app, a)
    url = start(c3, idp)
    code, state = idp.stage(url)
    with app.state.session_factory() as s:
        row = s.scalars(select(OidcLoginState).order_by(OidcLoginState.created_at.desc())).first()
        row.expires_at = row.created_at.replace(year=2000)
        s.commit()
    assert c3.get(f"{c3.base}/auth/oidc/callback?code={code}&state={state}").json()["error"]["code"] == "invalid_state"
    for q in ("", "?code=x", "?state=y", "?code=x&state=unknown", "?error=access_denied&state=y"):
        assert browser(app, a).get(f"/api/v1/apps/{a['id']}/auth/oidc/callback{q}").status_code == 400


def test_pkce_and_the_client_secret_are_sent_and_failures_are_reported(world, idp, app):
    a, owner, *_ = world
    allow_local(app)
    configure(owner, a, idp)
    c, r = sign_in(app, a, idp)
    token_call = next(x for x in idp.requests if x[0] == "POST")
    assert token_call[2]["client_secret"] == SECRET and token_call[2]["grant_type"] == "authorization_code" and len(token_call[2]["code_verifier"]) >= 43
    url = start(c, idp)
    q = parse_qs(urlparse(url).query)
    assert q["code_challenge_method"] == ["S256"] and q["response_type"] == ["code"] and q["scope"] == ["openid email profile"]
    idp.token_status = 400
    c2 = browser(app, a)
    r2 = finish(c2, idp, start(c2, idp))
    assert r2.status_code == 401 and r2.json()["error"]["code"] == "token_exchange_failed" and "invalid_grant" not in r2.text


def test_the_post_login_target_cannot_be_hijacked(world, idp, app):
    a, owner, *_ = world
    allow_local(app)
    configure(owner, a, idp, post_login_url="/home")
    for nxt, expected in (("/orders/7", "/orders/7"), ("//evil.example", "/home"), ("https://evil.example/x", "/home"), ("/\\evil.example", "/home"), ("javascript:alert(1)", "/home")):
        c = browser(app, a)
        r = finish(c, idp, start(c, idp, next=nxt))
        assert r.status_code == 302 and r.headers["location"] == expected, nxt
        idp.codes.clear()


def test_a_disabled_provider_and_a_missing_one_look_the_same(world, idp, app):
    a, owner, *_ = world
    allow_local(app)
    c = browser(app, a)
    assert c.get(f"{c.base}/auth/oidc/login").status_code == 404
    configure(owner, a, idp, status="disabled")
    assert c.get(f"{c.base}/auth/oidc/login").status_code == 404
    assert c.get(f"{c.base}/auth/oidc/callback?code=x&state=y").status_code == 404


def test_an_unreachable_or_refused_provider_is_a_gateway_error(world, idp, app):
    a, owner, *_ = world
    from orchivio.integrations import Policy
    allow_local(app)
    configure(owner, a, idp)
    app.state.oidc = OidcClient(PinnedTransport(Policy()))  # the strict default policy: a loopback http provider is refused when called
    c = browser(app, a)
    r = c.get(f"{c.base}/auth/oidc/login")
    assert r.status_code == 502 and r.json()["error"]["code"] == "provider_unreachable"
