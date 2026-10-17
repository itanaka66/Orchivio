"""Consumed REST APIs: model rules, flow steps, destinations, the real HTTP path, and OpenAPI import."""

import copy
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from test_tenancy import add_member, make_org

from orchivio.integrations import integration_client_factory
from orchivio.model import (
    AppModel,
    assign_element_ids,
    build_archive,
    diff_models,
    flatten,
    has_errors,
    migrate_to_latest,
    read_archive,
    unflatten,
    validate_model,
)

TOKEN = "billing-token-123"


def billing_model(raw):
    m = copy.deepcopy(raw)
    mod = m["modules"][0]
    mod["entities"] += [
        {"name": "Rate", "transient": True, "fields": [{"name": "code", "type": "string", "required": True}, {"name": "rate", "type": "decimal", "required": True}]},
        {"name": "InvoiceRequest", "transient": True, "fields": [{"name": "sku", "type": "string", "required": True}, {"name": "quantity", "type": "integer", "required": True}]},
        {"name": "InvoiceReceipt", "transient": True, "fields": [{"name": "id", "type": "string", "required": True}, {"name": "total_amount", "type": "decimal"}]},
        {"name": "ItemInfo", "transient": True, "fields": [{"name": "name", "type": "string", "required": True}]},
    ]
    mod["integrations"] = [{
        "key": "billing", "destination": "billing-api", "description": "Billing",
        "operations": [
            {"id": "get_rate", "method": "GET", "path": "/rates/{currency_code}", "params": [{"name": "currency_code", "location": "path", "type": "string"}],
             "response": "Rate", "response_kind": "object"},
            {"id": "list_items", "method": "GET", "path": "/items", "params": [{"name": "limit", "location": "query", "type": "integer", "required": False, "json_name": "max"}],
             "response": "ItemInfo", "response_kind": "list"},
            {"id": "create_invoice", "method": "POST", "path": "/invoices", "body": "InvoiceRequest", "response": "InvoiceReceipt", "response_kind": "object",
             "response_map": {"total_amount": "totalAmount"}},
            {"id": "ping", "method": "DELETE", "path": "/ping"},
        ]}]
    mod["flows"] += [
        {"key": "CheckRate", "kind": "server", "params": [{"name": "code", "type": "string"}],
         "steps": [{"id": "start", "type": "start"},
                   {"id": "call", "type": "call_integration", "config": {"integration": "billing", "operation": "get_rate", "args": {"currency_code": "code"}, "into": "rate"}},
                   {"id": "high", "type": "decision", "config": {"expr": "rate.rate > 100"}},
                   {"id": "fail", "type": "raise_error", "config": {"message": "rate too high"}},
                   {"id": "end", "type": "end"}],
         "edges": [{"id": "e1", "source": "start", "target": "call"}, {"id": "e2", "source": "call", "target": "high"},
                   {"id": "e3", "source": "high", "target": "fail", "handle": "true"}, {"id": "e4", "source": "high", "target": "end", "handle": "false"}],
         "tests": [
             {"name": "a high rate stops the flow", "args": {"code": "JPY"}, "mocks": {"billing.get_rate": {"json": {"code": "JPY", "rate": 150}}},
              "expect": {"status": "failed", "error_contains": "rate too high", "visited": ["call", "high", "fail"]}},
             {"name": "a low rate passes", "args": {"code": "JPY"}, "mocks": {"billing.get_rate": {"json": {"code": "JPY", "rate": 50}}},
              "expect": {"status": "succeeded", "not_visited": ["fail"]}},
             {"name": "an upstream error fails the flow", "args": {"code": "JPY"}, "mocks": {"billing.get_rate": {"status": 503}},
              "expect": {"status": "failed", "error_contains": "HTTP 503"}},
             {"name": "a response of the wrong shape fails the flow", "args": {"code": "JPY"}, "mocks": {"billing.get_rate": {"json": {"code": "JPY", "rate": "lots"}}},
              "expect": {"status": "failed", "error_contains": "rate"}},
             {"name": "a missing mock is an error, never a real call", "args": {"code": "JPY"},
              "expect": {"status": "failed", "error_contains": "no mock"}},
         ]},
        {"key": "Invoice", "kind": "server", "params": [{"name": "sku", "type": "string"}, {"name": "qty", "type": "integer"}],
         "steps": [{"id": "start", "type": "start"},
                   {"id": "send", "type": "call_integration", "config": {"integration": "billing", "operation": "create_invoice", "body": {"sku": "sku", "quantity": "qty"}, "into": "receipt"}},
                   {"id": "end", "type": "end"}],
         "edges": [{"id": "e1", "source": "start", "target": "send"}, {"id": "e2", "source": "send", "target": "end"}]},
        {"key": "Items", "kind": "server", "params": [],
         "steps": [{"id": "start", "type": "start"},
                   {"id": "get", "type": "call_integration", "config": {"integration": "billing", "operation": "list_items", "args": {"limit": "2"}, "into": "items"}},
                   {"id": "end", "type": "end"}],
         "edges": [{"id": "e1", "source": "start", "target": "get"}, {"id": "e2", "source": "get", "target": "end"}]},
        {"key": "Ping", "kind": "server", "params": [],
         "steps": [{"id": "start", "type": "start"}, {"id": "p", "type": "call_integration", "config": {"integration": "billing", "operation": "ping"}}, {"id": "end", "type": "end"}],
         "edges": [{"id": "e1", "source": "start", "target": "p"}, {"id": "e2", "source": "p", "target": "end"}]},
    ]
    return m


def identified(raw):
    return assign_element_ids(AppModel.model_validate(migrate_to_latest(raw)))


def errors(raw):
    return {(i.code) for i in validate_model(identified(raw)) if i.severity == "error"}


# --- the model ---------------------------------------------------------------------------------


def test_the_sample_integration_model_is_valid(raw_model):
    assert errors(billing_model(raw_model)) == set()


@pytest.mark.parametrize("change, code", [
    (lambda m: m["modules"][0]["integrations"].append(copy.deepcopy(m["modules"][0]["integrations"][0])), "duplicate-integration"),
    (lambda m: m["modules"][0]["integrations"][0]["operations"].append(copy.deepcopy(m["modules"][0]["integrations"][0]["operations"][0])), "duplicate-operation"),
    (lambda m: m["modules"][0]["integrations"][0]["operations"][0].update(path="/rates/{nope}"), "integration-path"),
    (lambda m: m["modules"][0]["integrations"][0]["operations"][0].update(path="/rates"), "integration-path"),
    (lambda m: m["modules"][0]["integrations"][0]["operations"][0]["params"][0].update(required=False), "integration-path"),
    (lambda m: m["modules"][0]["integrations"][0]["operations"][0].update(body="InvoiceRequest"), "integration-body"),
    (lambda m: m["modules"][0]["integrations"][0]["operations"][0].update(response_kind=None), "integration-response"),
    (lambda m: m["modules"][0]["integrations"][0]["operations"][0].update(response="Product"), "integration-entity"),
    (lambda m: m["modules"][0]["integrations"][0]["operations"][0].update(response="Nope"), "integration-entity"),
    (lambda m: m["modules"][0]["integrations"][0]["operations"][0].update(response_map={"ghost": "x"}), "integration-map"),
    (lambda m: m["modules"][0]["integrations"][0]["operations"][3].update(body_map={"a": "b"}), "integration-map"),
])
def test_integration_rules(raw_model, change, code):
    m = billing_model(raw_model)
    change(m)
    assert code in errors(m)


def flow_of(m, key):
    return next(f for f in m["modules"][0]["flows"] if f["key"] == key)


@pytest.mark.parametrize("config, expect", [
    ({"integration": "ghost"}, "unknown integration"),
    ({"operation": "ghost"}, "no operation"),
    ({"args": {}}, "missing argument 'currency_code'"),
    ({"args": {"currency_code": "code", "extra": "1"}}, "no parameter 'extra'"),
    ({"args": {"currency_code": "1 + 1"}}, ""),  # an integer where a string is needed
    ({"into": None}, "'into'"),
    ({"body": {"sku": "code"}}, "sends no body"),
])
def test_call_integration_step_rules(raw_model, config, expect):
    m = billing_model(raw_model)
    step = next(s for s in flow_of(m, "CheckRate")["steps"] if s["id"] == "call")
    for k, v in config.items():
        if v is None:
            step["config"].pop(k)
        else:
            step["config"][k] = v
    issues = [i for i in validate_model(identified(m)) if i.severity == "error"]
    assert issues and any(expect in i.message for i in issues), [i.message for i in issues]


def test_body_fields_and_return_values_are_checked(raw_model):
    m = billing_model(raw_model)
    step = next(s for s in flow_of(m, "Invoice")["steps"] if s["id"] == "send")
    step["config"]["body"] = {"sku": "sku"}  # quantity is required
    assert any("quantity" in i.message for i in validate_model(identified(m)) if i.severity == "error")
    m = billing_model(raw_model)
    step = next(s for s in flow_of(m, "Ping")["steps"] if s["id"] == "p")
    step["config"]["into"] = "nothing"
    assert any("returns nothing" in i.message for i in validate_model(identified(m)))


def test_client_flows_cannot_call_integrations_and_mocks_must_name_operations(raw_model):
    m = billing_model(raw_model)
    flow_of(m, "Ping")["kind"] = "client"
    assert "step-not-allowed" in errors(m)
    m = billing_model(raw_model)
    flow_of(m, "CheckRate")["tests"][0]["mocks"] = {"billing.nope": {"json": 1}}
    assert "test-config" in errors(m)


def test_integrations_survive_canonical_files_archives_and_merge_flattening(raw_model):
    model = identified(billing_model(raw_model))
    assert read_archive(build_archive(model)) == model
    again = unflatten(flatten(model), schema_version=model.schema_version)
    assert again.modules[0].integrations == model.modules[0].integrations
    plain = identified(raw_model)
    assert plain.modules[0].integrations is None  # models without any are unchanged (hash-stable)


def test_external_calls_need_a3(raw_model):
    plain = identified(raw_model)
    with_integration = billing_model(raw_model)
    with_integration["modules"][0]["flows"] = [f for f in with_integration["modules"][0]["flows"] if f["key"] in ("ReorderLowStock",)]
    d = diff_models(plain, assign_element_ids(AppModel.model_validate(migrate_to_latest(with_integration)), previous=plain))
    assert d.level.name == "A3" and any("external-integration" in c.risks for c in d.changes)
    # a new call inside an existing flow is flagged even though the integration itself is unchanged
    base = identified(billing_model(raw_model))
    m = billing_model(raw_model)
    flow = flow_of(m, "Ping")
    flow["steps"].insert(2, {"id": "p2", "type": "call_integration", "config": {"integration": "billing", "operation": "ping"}})
    flow["edges"] = [{"id": "e1", "source": "start", "target": "p"}, {"id": "e3", "source": "p", "target": "p2"}, {"id": "e2", "source": "p2", "target": "end"}]
    d2 = diff_models(base, assign_element_ids(AppModel.model_validate(migrate_to_latest(m)), previous=base))
    assert d2.level.name == "A2"  # the same operation again is not a new capability
    flow["steps"][2]["config"]["operation"] = "get_rate"
    flow["steps"][2]["config"].update(args={"currency_code": "'X'"}, into="r")
    d3 = diff_models(base, assign_element_ids(AppModel.model_validate(migrate_to_latest(m)), previous=base))
    assert any("external-call" in c.risks for c in d3.changes) and d3.level.name == "A3"


# --- the app: flow tests (no network) ------------------------------------------------------------


@pytest.fixture
def world(new_api, raw_model, app):
    owner = new_api("owner@example.com")
    org = make_org(owner)
    proj = owner.post(f"/organizations/{org['id']}/projects", json={"name": "Inv", "slug": "inventory"}).json()
    editor = new_api("editor@example.com")
    add_member(owner, org, "editor@example.com", "editor")
    outsider = new_api("outsider@example.com")
    r = owner.post(f"/projects/{proj['id']}/applications", json={"key": "inventory", "name": "Inv", "model": billing_model(raw_model)})
    assert r.status_code == 201, r.text
    a = r.json()
    owner.post(f"/applications/{a['id']}/preview/reset", json={})
    return a, {"owner": owner, "editor": editor, "outsider": outsider}, raw_model


def test_generated_flow_tests_run_with_mocks_and_never_touch_the_network(world):
    a, p, _ = world
    r = p["owner"].post(f"/revisions/{a['current_revision_id']}/test-runs", json={})
    assert r.status_code == 201, r.text
    run = p["owner"].get(f"/test-runs/{r.json()['id']}?report=true").json()
    flow_results = [x for x in run["report"]["results"] if x["id"].startswith("flow:") or "CheckRate" in x["id"]]
    assert flow_results and all(x["status"] == "passed" for x in flow_results), flow_results


# --- destinations and the real HTTP path -------------------------------------------------------------


class Billing(BaseHTTPRequestHandler):
    seen = []

    def _answer(self, body=b""):
        Billing.seen.append((self.command, self.path, dict(self.headers), body))
        routes = {("GET", "/rates/JPY"): (200, {"code": "JPY", "rate": 150}), ("GET", "/rates/EUR"): (200, {"code": "EUR", "rate": 0.9}),
                  ("GET", "/items?max=2"): (200, [{"name": "a"}, {"name": "b"}]), ("POST", "/invoices"): (201, {"id": "inv-1", "totalAmount": "12.50"}),
                  ("GET", "/rates/BAD"): (500, {"error": "boom"}), ("GET", "/rates/WEIRD"): (200, [1, 2]), ("DELETE", "/ping"): (204, None)}
        status, payload = routes.get((self.command, self.path), (404, {"error": "no"}))
        data = b"" if payload is None else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        self._answer()

    def do_DELETE(self):  # noqa: N802
        self._answer()

    def do_POST(self):  # noqa: N802
        self._answer(self.rfile.read(int(self.headers.get("Content-Length", 0))))

    def log_message(self, *a):
        pass


@pytest.fixture
def billing_server(app, monkeypatch):
    Billing.seen = []
    server = HTTPServer(("127.0.0.1", 0), Billing)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("ORCHIVIO_TEST_BILLING_TOKEN", TOKEN)
    app.state.settings.webhook_allow_http = True
    app.state.settings.webhook_allow_private_networks = True
    app.state.integration_clients = integration_client_factory(app.state.settings, app.state.session_factory)
    yield server.server_address[1]
    server.shutdown()


def configure(owner, a, port, **auth):
    r = owner.put(f"/applications/{a['id']}/destinations/billing-api",
                  json={"base_url": f"http://127.0.0.1:{port}", "auth": auth or {"type": "bearer", "secret_ref": "env:ORCHIVIO_TEST_BILLING_TOKEN"}})
    assert r.status_code == 200, r.text
    return r.json()


def run_flow(owner, a, flow, **args):
    r = owner.post(f"/applications/{a['id']}/preview/flows/core.{flow}/run", json={"args": args},
                   headers={"X-Preview-User": "u", "X-Preview-Roles": "admin"})
    assert r.status_code == 200, r.text
    return r.json()


def test_a_flow_calls_the_configured_api_with_typed_arguments(world, billing_server):
    a, p, _ = world
    configure(p["owner"], a, billing_server)
    ok = run_flow(p["owner"], a, "CheckRate", code="EUR")
    assert ok["status"] == "succeeded" and any(s["detail"] == "billing.get_rate -> HTTP 200" for s in ok["trace"])
    method, path, headers, _ = Billing.seen[0]
    assert (method, path) == ("GET", "/rates/EUR") and headers["Authorization"] == f"Bearer {TOKEN}" and headers["Accept"] == "application/json"
    assert run_flow(p["owner"], a, "CheckRate", code="JPY")["error"] == "rate too high"  # the typed response drove the decision
    items = run_flow(p["owner"], a, "Items")
    assert items["status"] == "succeeded" and Billing.seen[-1][1] == "/items?max=2"  # json_name used on the wire
    inv = run_flow(p["owner"], a, "Invoice", sku="s-1", qty=3)
    assert inv["status"] == "succeeded"
    _, path, headers, body = Billing.seen[-1]
    assert path == "/invoices" and json.loads(body) == {"sku": "s-1", "quantity": 3} and headers["Content-Type"] == "application/json"
    assert run_flow(p["owner"], a, "Ping")["status"] == "succeeded"  # 204 with no body and no response entity


def test_path_values_are_encoded_and_dot_segments_refused(world, billing_server):
    a, p, _ = world
    configure(p["owner"], a, billing_server)
    out = run_flow(p["owner"], a, "CheckRate", code="a/b?c")
    assert Billing.seen[-1][1] == "/rates/a%2Fb%3Fc" and out["status"] == "failed"  # the server answered 404 for that path
    before = len(Billing.seen)
    dots = run_flow(p["owner"], a, "CheckRate", code="..")
    assert dots["status"] == "failed" and "cannot be used in a URL path" in dots["error"] and len(Billing.seen) == before


def test_failures_become_flow_errors_without_leaking_the_response(world, billing_server):
    a, p, _ = world
    configure(p["owner"], a, billing_server)
    bad = run_flow(p["owner"], a, "CheckRate", code="BAD")
    assert bad["status"] == "failed" and "HTTP 500" in bad["error"] and "boom" not in bad["error"]
    weird = run_flow(p["owner"], a, "CheckRate", code="WEIRD")
    assert weird["status"] == "failed" and "expected a JSON object" in weird["error"]


def test_destination_problems_fail_the_call_clearly(world, billing_server, monkeypatch):
    a, p, _ = world
    missing = run_flow(p["owner"], a, "CheckRate", code="EUR")
    assert missing["status"] == "failed" and "destination 'billing-api' is not configured" in missing["error"]
    configure(p["owner"], a, billing_server)
    monkeypatch.delenv("ORCHIVIO_TEST_BILLING_TOKEN")
    assert "credential" in run_flow(p["owner"], a, "CheckRate", code="EUR")["error"]
    assert Billing.seen == []


def test_private_destinations_are_refused_at_call_time_by_default(world, app, billing_server):
    a, p, _ = world
    configure(p["owner"], a, billing_server)
    strict = integration_client_factory(type("S", (), {"webhook_allow_http": True, "webhook_allow_private_networks": False,
                                                       "webhook_allowed_hosts": [], "webhook_timeout_seconds": 5})(), app.state.session_factory)
    app.state.integration_clients = strict
    out = run_flow(p["owner"], a, "CheckRate", code="EUR")
    assert out["status"] == "failed" and "destination refused" in out["error"] and Billing.seen == []


def test_calls_are_refused_where_no_client_is_wired(world, app):
    import uuid

    from orchivio.db import to_model
    from orchivio.db.models import AppRevision
    from orchivio.logic import FlowRunner
    from orchivio.runtime import AppUser
    a, p, _ = world
    with app.state.session_factory() as s:
        rev = s.get(AppRevision, uuid.UUID(a["current_revision_id"]))
        model, mhash = to_model(rev), rev.model_hash
    data = app.state.preview_store.open(uuid.UUID(a["id"]), model, mhash)  # no integrations client
    out = FlowRunner(model, data).run("core.CheckRate", {"code": "EUR"}, AppUser("u", frozenset({"admin"})))
    assert out.status == "failed" and "cannot call external APIs" in out.error


# --- destination management ----------------------------------------------------------------------------


def test_destination_management(world, monkeypatch):
    a, p, _ = world
    monkeypatch.setenv("ORCHIVIO_TEST_BILLING_TOKEN", TOKEN)
    o = p["owner"]
    url = f"/applications/{a['id']}/destinations"
    good = {"base_url": "https://api.billing.example/v1", "auth": {"type": "header", "header": "X-Api-Key", "secret_ref": "env:ORCHIVIO_TEST_BILLING_TOKEN"}}
    r = o.put(f"{url}/billing-api", json=good)
    assert r.status_code == 200 and r.json()["auth"]["header"] == "X-Api-Key" and r.json()["credential_available"] is True
    assert TOKEN not in json.dumps(r.json())
    for patch, code in (({"base_url": "http://api.example/"}, "destination_refused"), ({"base_url": "https://169.254.169.254/"}, "destination_refused"),
                        ({"base_url": "https://localhost/"}, "destination_refused"), ({"base_url": "https://api.example/?k=v"}, "destination_refused"),
                        ({"base_url": "https://u:p@api.example/"}, "destination_refused"),
                        ({"auth": {"type": "bearer"}}, "invalid_secret_ref"), ({"auth": {"type": "bearer", "secret_ref": "vault:x"}}, "invalid_secret_ref"),
                        ({"auth": {"type": "bearer", "secret_ref": "env:NOT_SET_ANYWHERE_9"}}, "secret_unavailable"),
                        ({"auth": {"type": "header", "header": "Host", "secret_ref": "env:ORCHIVIO_TEST_BILLING_TOKEN"}}, "invalid_auth"),
                        ({"auth": {"type": "header", "secret_ref": "env:ORCHIVIO_TEST_BILLING_TOKEN"}}, "invalid_auth"),
                        ({"auth": {"type": "none", "secret_ref": "env:ORCHIVIO_TEST_BILLING_TOKEN"}}, "invalid_auth")):
        r = o.put(f"{url}/other", json={**good, **patch})
        assert r.status_code == 422 and r.json()["error"]["code"] == code, patch
    assert o.put(f"{url}/Bad_Name", json=good).json()["error"]["code"] == "invalid_name"
    assert p["editor"].put(f"{url}/other", json=good).status_code == 403
    assert p["editor"].get(url).status_code == 403
    assert p["outsider"].get(url).status_code == 404
    assert [d["name"] for d in o.get(url).json()["items"]] == ["billing-api"]
    assert o.delete(f"{url}/billing-api").status_code == 204 and o.delete(f"{url}/billing-api").status_code == 404


# --- OpenAPI import ---------------------------------------------------------------------------------------

SPEC = {
    "openapi": "3.0.3", "info": {"title": "Weather"},
    "paths": {"/forecast/{cityId}": {"get": {"operationId": "getForecast",
                                             "parameters": [{"name": "cityId", "in": "path", "required": True, "schema": {"type": "string"}},
                                                            {"name": "units", "in": "query", "schema": {"type": "string"}}],
                                             "responses": {"200": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/Forecast"}}}}}}}},
    "components": {"schemas": {"Forecast": {"type": "object", "required": ["tempC"], "properties": {"tempC": {"type": "number"}, "summary": {"type": "string"}}}}},
}


def test_openapi_import_creates_an_a3_proposal_and_nothing_else(world):
    a, p, _ = world
    o = p["owner"]
    base = f"/applications/{a['id']}/integrations/openapi-import"
    r = o.post(f"{base}?key=weather&destination=weather-api", content=json.dumps(SPEC).encode(), headers={"Content-Type": "application/json"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "pending" and body["approval_level"] == "A3"
    assert body["import"]["integration"] == "core.weather" and body["import"]["operations"] == ["get_forecast"] and body["import"]["entities"] == ["Forecast"]
    assert o.get(f"/applications/{a['id']}").json()["current_revision_no"] == 1  # nothing applied yet
    assert p["editor"].post(f"{base}?key=w2&destination=x", content=json.dumps(SPEC).encode()).status_code == 403

    done = o.post(f"/proposals/{body['id']}/approval", json={"decision": "approve", "acknowledge_risks": True})
    assert done.status_code in (200, 201), done.text
    model = o.get(f"/applications/{a['id']}").json()
    rev = o.get(f"/revisions/{model['current_revision_id']}").json()["model_json"]
    ig = next(i for i in rev["modules"][0]["integrations"] if i["key"] == "weather")
    assert ig["destination"] == "weather-api" and ig["operations"][0]["path"] == "/forecast/{city_id}"
    assert ig["operations"][0]["params"][0]["json_name"] == "cityId"
    again = o.post(f"{base}?key=weather&destination=weather-api", content=json.dumps(SPEC).encode())
    assert again.status_code == 409 and again.json()["error"]["code"] == "integration_exists"


def test_openapi_import_rejects_bad_input(world):
    a, p, _ = world
    base = f"/applications/{a['id']}/integrations/openapi-import?key=x&destination=y"
    o = p["owner"]
    assert o.post(base, content=b"{not yaml: [").json()["error"]["code"] == "invalid_spec"
    assert o.post(base, content=json.dumps({"openapi": "2.0", "paths": {"/a": {}}}).encode()).json()["error"]["code"] == "invalid_spec"
    evil = copy.deepcopy(SPEC)
    evil["paths"]["/forecast/{cityId}"]["get"]["responses"]["200"]["content"]["application/json"]["schema"] = {"$ref": "http://169.254.169.254/x"}
    r = o.post(base, content=json.dumps(evil).encode())
    assert r.status_code == 422 and "local references" in r.json()["error"]["message"]
    big = b" " * (2 * 1024 * 1024 + 1)
    assert o.post(base, content=big).status_code == 413
    assert o.post(f"/applications/{a['id']}/integrations/openapi-import?key=Bad&destination=y", content=b"{}").status_code == 422
    assert p["outsider"].post(base, content=b"{}").status_code == 404
    yaml_spec = "openapi: 3.0.0\npaths:\n  /ping:\n    get:\n      operationId: ping2\n      responses:\n        '200': {description: ok}\n"
    assert o.post(base, content=yaml_spec.encode()).status_code == 201  # YAML is accepted too
