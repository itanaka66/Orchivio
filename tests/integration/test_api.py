import copy

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from orchivio.api import create_app
from orchivio.api.settings import Settings


@pytest.fixture
def client():
    return TestClient(create_app(Settings(env="dev")), raise_server_exceptions=False)


def test_health(client):
    assert client.get("/healthz").json()["status"] == "ok"


def test_validate_ok(client, raw_model):
    r = client.post("/api/v1/models/validate", json={"model": raw_model})
    body = r.json()
    assert r.status_code == 200 and body["valid"] is True
    assert body["model_hash"].startswith("sha256:") and body["security_findings"] == []


def test_validate_accepts_schema_1_0_and_reports_issues(client):
    from conftest import load_yaml

    r = client.post("/api/v1/models/validate", json={"model": load_yaml("model-1.0.yaml")})
    assert r.json()["valid"] is True and r.json()["schema_version"] == "1.1"


def test_validate_reports_semantic_errors(client, raw_model):
    raw_model["security"]["access"] = []
    body = client.post("/api/v1/models/validate", json={"model": raw_model}).json()
    assert body["valid"] is False and any(i["code"] == "no-access-rule" for i in body["issues"])


def test_schema_errors_do_not_echo_input(client, raw_model):
    raw_model["application"]["name"] = ""
    raw_model["application"]["secret_marker"] = "TOPSECRET"
    r = client.post("/api/v1/models/validate", json={"model": raw_model})
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_model"
    assert "TOPSECRET" not in r.text


def test_unsupported_version(client):
    r = client.post("/api/v1/models/validate", json={"model": {"schema_version": "0.1"}})
    assert r.status_code == 422 and r.json()["error"]["code"] == "unsupported_schema_version"


def test_diff_flags_permission_expansion(client, raw_model):
    new = copy.deepcopy(raw_model)
    del new["security"]["access"][1]["row_constraint"]
    r = client.post("/api/v1/models/diff", json={"base": raw_model, "new": new})
    body = r.json()
    assert r.status_code == 200 and body["level"] == "A3" and body["security_changed"] is True
    assert body["changes"][0]["risks"] == ["permission-expansion"]


def test_diff_identical_models_is_empty(client, raw_model):
    body = client.post("/api/v1/models/diff", json={"base": raw_model, "new": raw_model}).json()
    assert body["changes"] == [] and body["level"] == "A0"


def test_expression_check(client, raw_model):
    ok = client.post(
        "/api/v1/expressions/check",
        json={"model": raw_model, "entity": "core.Product", "expression": "quantity <= reorder_level", "expected": "bool"},
    ).json()
    assert ok == {"valid": True, "type": "bool", "fields": ["quantity", "reorder_level"]}
    bad = client.post(
        "/api/v1/expressions/check",
        json={"model": raw_model, "entity": "core.Product", "expression": "quantity + 'a'"},
    ).json()
    assert bad["valid"] is False and "needs numbers" in bad["error"]["message"]


def test_expression_check_validates_inputs(client, raw_model):
    r = client.post("/api/v1/expressions/check", json={"model": raw_model, "entity": "core.Nope", "expression": "1"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "unknown_entity"
    r = client.post("/api/v1/expressions/check", json={"model": raw_model, "expression": "x" * 5000})
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_request"


def test_body_size_limit():
    c = TestClient(create_app(Settings(env="dev", max_body_bytes=100)))
    r = c.post("/api/v1/models/validate", json={"model": {"pad": "x" * 500}})
    assert r.status_code == 413


def test_bearer_token_enforced_when_configured(raw_model):
    c = TestClient(create_app(Settings(env="prod", api_token=SecretStr("s3cret"))))
    url = "/api/v1/models/validate"
    assert c.post(url, json={"model": raw_model}).status_code == 401
    assert c.post(url, json={"model": raw_model}, headers={"Authorization": "Bearer nope"}).status_code == 401
    assert c.post(url, json={"model": raw_model}, headers={"Authorization": "Bearer s3cret"}).status_code == 200
    assert c.get("/healthz").status_code == 200  # health stays open for orchestrators


def test_prod_requires_a_token():
    with pytest.raises(RuntimeError):
        create_app(Settings(env="prod"))
