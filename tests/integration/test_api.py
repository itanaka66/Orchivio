"""Operational endpoints and the authenticated model tools."""

import copy

import pytest
from fastapi.testclient import TestClient
from tests_support import Api

from orchivio.api import create_app
from orchivio.api.settings import Settings


def test_health_is_open_and_has_request_id(app):
    r = TestClient(app).get("/healthz")
    assert r.json()["status"] == "ok" and r.headers["x-request-id"]
    assert r.headers["x-content-type-options"] == "nosniff"


def test_inbound_request_id_is_reused_only_if_well_formed(app):
    c = TestClient(app)
    assert c.get("/healthz", headers={"X-Request-ID": "abc-12345678"}).headers["x-request-id"] == "abc-12345678"
    assert c.get("/healthz", headers={"X-Request-ID": "bad id\n"}).headers["x-request-id"] != "bad id\n"


def test_tools_require_authentication(app, raw_model):
    c = TestClient(app)
    for path, body in [("/models/validate", {"model": raw_model}),
                       ("/models/diff", {"base": raw_model, "new": raw_model}),
                       ("/expressions/check", {"model": raw_model, "expression": "1"})]:
        assert c.post("/api/v1" + path, json=body).status_code == 401


def test_validate_ok(new_api, raw_model):
    body = new_api().post("/models/validate", json={"model": raw_model}).json()
    assert body["valid"] is True and body["model_hash"].startswith("sha256:") and body["security_findings"] == []


def test_validate_accepts_schema_1_0(new_api):
    from tests_support import load_yaml

    r = new_api().post("/models/validate", json={"model": load_yaml("model-1.0.yaml")})
    assert r.json()["valid"] is True and r.json()["schema_version"] == "1.1"


def test_validate_reports_semantic_errors(new_api, raw_model):
    raw_model["security"]["access"] = []
    body = new_api().post("/models/validate", json={"model": raw_model}).json()
    assert body["valid"] is False and any(i["code"] == "no-access-rule" for i in body["issues"])


def test_schema_errors_do_not_echo_input(new_api, raw_model):
    raw_model["application"]["name"] = ""
    raw_model["application"]["secret_marker"] = "TOPSECRET"
    r = new_api().post("/models/validate", json={"model": raw_model})
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_model" and "TOPSECRET" not in r.text


def test_malformed_legacy_model_is_a_422_not_a_500(new_api):
    r = new_api().post("/models/validate", json={"model": {"schema_version": "1.0", "entities": "nope"}})
    assert r.status_code == 422


def test_unsupported_version(new_api):
    r = new_api().post("/models/validate", json={"model": {"schema_version": "0.1"}})
    assert r.json()["error"]["code"] == "unsupported_schema_version"


def test_diff_flags_permission_expansion(new_api, raw_model):
    new = copy.deepcopy(raw_model)
    del new["security"]["access"][1]["row_constraint"]
    body = new_api().post("/models/diff", json={"base": raw_model, "new": new}).json()
    assert body["level"] == "A3" and body["security_changed"] and body["changes"][0]["risks"] == ["permission-expansion"]


def test_expression_check(new_api, raw_model):
    api = new_api()
    ok = api.post("/expressions/check", json={"model": raw_model, "entity": "core.Product",
                                              "expression": "quantity <= reorder_level", "expected": "bool"}).json()
    assert ok == {"valid": True, "type": "bool", "fields": ["quantity", "reorder_level"]}
    bad = api.post("/expressions/check", json={"model": raw_model, "entity": "core.Product",
                                               "expression": "quantity + 'a'"}).json()
    assert bad["valid"] is False and "needs numbers" in bad["error"]["message"]
    r = api.post("/expressions/check", json={"model": raw_model, "entity": "core.Nope", "expression": "1"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "unknown_entity"
    r = api.post("/expressions/check", json={"model": raw_model, "expression": "x" * 5000})
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_request"


def test_body_size_limit(db_url):
    c = TestClient(create_app(Settings(env="dev", database_url=db_url, preview_database_url="sqlite:///:memory:", max_body_bytes=100)))
    assert c.post("/api/v1/auth/login", json={"email": "a@b.co", "password": "x" * 500}).status_code == 413


def test_unhandled_errors_do_not_leak_details(app):
    @app.get("/boom")
    def boom():
        raise RuntimeError("secret internal detail")

    r = TestClient(app, raise_server_exceptions=False).get("/boom")
    assert r.status_code == 500 and "secret" not in r.text and r.json()["error"]["code"] == "internal_error"


def test_prod_refuses_insecure_cookies(db_url):
    with pytest.raises(RuntimeError):
        create_app(Settings(env="prod", database_url=db_url, cookie_secure=False))
    assert Settings(env="prod").secure_cookies is True and Settings(env="dev").secure_cookies is False


def test_api_helper_isolation(app):
    a, b = Api(app).signup("a@example.com"), Api(app).signup("b@example.com")
    assert a.get("/me").json()["user"]["email"] == "a@example.com"
    assert b.get("/me").json()["user"]["email"] == "b@example.com"


def test_validate_flow_endpoint(new_api, raw_model):
    api = new_api()
    flow = raw_model["modules"][0]["flows"][0]
    ok = api.post("/models/validate-flow", json={"model": raw_model, "module": "core", "flow": flow}).json()
    assert ok == {"valid": True, "issues": []}

    bad = copy.deepcopy(flow)
    next(s for s in bad["steps"] if s["id"] == "any")["config"]["expr"] = "size(low)"
    body = api.post("/models/validate-flow", json={"model": raw_model, "module": "core", "flow": bad}).json()
    assert body["valid"] is False and body["issues"][0]["path"].endswith("/steps/any/config/expr")

    broken = {"key": "X", "steps": [{"id": "BAD ID", "type": "start"}]}
    r = api.post("/models/validate-flow", json={"model": raw_model, "module": "core", "flow": broken})
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_flow"
    r = api.post("/models/validate-flow", json={"model": raw_model, "module": "nope", "flow": flow})
    assert r.status_code == 422 and r.json()["error"]["code"] == "unknown_module"
    assert TestClient(api.c.app).post("/api/v1/models/validate-flow", json={}).status_code == 401
