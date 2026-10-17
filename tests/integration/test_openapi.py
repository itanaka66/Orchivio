"""The generated OpenAPI document must describe what the API really does (FR-032)."""

import copy
import json

import pytest
from jsonschema import Draft202012Validator
from openapi_spec_validator import validate
from test_tenancy import add_member, make_org

from orchivio.codegen import build_openapi
from orchivio.model import AppModel, assign_element_ids

PRODUCT = "core.Product"


def schema_validator(spec, name):
    return Draft202012Validator({"$ref": f"#/components/schemas/{name}", "components": spec["components"]})


def problems(spec, name, instance):
    return [e.message for e in schema_validator(spec, name).iter_errors(instance)]


@pytest.fixture
def model(raw_model):
    return assign_element_ids(AppModel.model_validate(raw_model))


@pytest.fixture
def spec(model):
    return build_openapi(model)


# --- the document itself ----------------------------------------------------------------------------------


def test_the_document_is_valid_openapi(spec):
    validate(spec)
    assert spec["openapi"] == "3.1.0" and spec["info"]["title"] == "Inventory Manager"
    assert {p: sorted(ops) for p, ops in spec["paths"].items()} == {
        "/data/core.Product": ["get", "post"], "/data/core.Product/{id}": ["delete", "get", "patch"]}


def test_roles_and_row_constraints_are_documented(spec):
    ops = spec["paths"]["/data/core.Product"]
    assert ops["post"]["x-orchivio-roles"] == ["editor", "admin"]
    assert ops["get"]["x-orchivio-roles"] == ["viewer", "editor", "admin"]
    assert ops["get"]["x-orchivio-row-constraints"] == {"editor": "record.owner == $currentUser"}
    assert spec["paths"]["/data/core.Product/{id}"]["delete"]["x-orchivio-roles"] == ["admin"]


def test_operations_nobody_may_perform_are_omitted(raw_model):
    raw = copy.deepcopy(raw_model)
    raw["security"]["access"] = [r for r in raw["security"]["access"] if r["role"] == "viewer"]  # read only
    raw["security"]["pages"] = {"product-list": ["viewer"]}
    raw["modules"][0]["pages"] = raw["modules"][0]["pages"][:1]
    spec = build_openapi(AppModel.model_validate(raw))
    assert {p: sorted(ops) for p, ops in spec["paths"].items()} == {
        "/data/core.Product": ["get"], "/data/core.Product/{id}": ["get"]}
    validate(spec)


def test_schemas_follow_the_model(spec):
    s = spec["components"]["schemas"]
    assert set(s) >= {"Error", "Product", "ProductCreate", "ProductUpdate", "ProductPage"}
    out, create = s["Product"]["properties"], s["ProductCreate"]
    assert out["id"]["readOnly"] and out["status"]["x-orchivio-computed"] == "virtual" and out["status"]["enum"] == ["in_stock", "low", "out_of_stock"]
    assert set(create["properties"]) == {"name", "sku", "quantity", "reorder_level", "owner"}  # no system-managed fields
    assert create["required"] == ["name", "sku", "quantity", "reorder_level"] and create["additionalProperties"] is False
    assert create["properties"]["name"]["maxLength"] == 200 and create["properties"]["quantity"]["minimum"] == 0
    assert "required" not in s["ProductUpdate"]
    assert s["Product"]["required"] == list(out)  # every field is always present in a response


def test_names_are_qualified_only_when_ambiguous(raw_model):
    raw = copy.deepcopy(raw_model)
    raw["modules"].append({"key": "other", "enumerations": copy.deepcopy(raw["modules"][0]["enumerations"]),
                           "entities": [copy.deepcopy(raw["modules"][0]["entities"][0])]})
    raw["modules"][1]["entities"][0].pop("element_id", None)
    raw["security"]["access"].append({"entity": "other.Product", "role": "admin", "operations": ["read"]})
    spec = build_openapi(AppModel.model_validate(raw))
    assert {"core_Product", "other_Product"} <= set(spec["components"]["schemas"])
    validate(spec)


# --- the document against the running API ---------------------------------------------------------------------


@pytest.fixture
def ctx(new_api, raw_model):
    owner = new_api("owner@example.com")
    org = make_org(owner)
    proj = owner.post(f"/organizations/{org['id']}/projects", json={"name": "Inv", "slug": "inventory"}).json()
    editor = new_api("editor@example.com")
    add_member(owner, org, "editor@example.com", "editor")
    app = owner.post(f"/projects/{proj['id']}/applications", json={"key": "inventory", "name": "Inv", "model": raw_model}).json()
    editor.post(f"/applications/{app['id']}/preview/reset", json={})
    return app, editor, owner


def acting(user, *roles):
    return {"X-Preview-User": user, "X-Preview-Roles": ",".join(roles)}


def test_the_endpoint_serves_the_same_document(ctx, spec):
    app, editor, _ = ctx
    served = editor.get(f"/revisions/{app['current_revision_id']}/openapi.json").json()
    assert served["paths"] == spec["paths"] and served["components"] == spec["components"]
    assert served["info"]["x-orchivio-model-hash"].startswith("sha256:") and "+r1" in served["info"]["version"]
    validate(served)


def test_real_responses_match_the_documented_schemas(ctx, spec):
    app, editor, _ = ctx
    url = f"/applications/{app['id']}/preview/data/{PRODUCT}"
    admin = acting("a", "admin")
    body = {"name": "Widget", "sku": "W-1", "quantity": 5, "reorder_level": 2}
    created = editor.post(url, json=body, headers=admin)
    assert created.status_code == 201
    assert problems(spec, "Product", created.json()) == []
    assert problems(spec, "ProductCreate", body) == []
    row = created.json()
    assert problems(spec, "ProductPage", editor.get(url, headers=admin).json()) == []
    assert problems(spec, "Product", editor.get(f"{url}/{row['id']}", headers=admin).json()) == []
    patched = editor.patch(f"{url}/{row['id']}", json={"quantity": 0}, headers=admin)
    assert patched.status_code == 200 and problems(spec, "ProductUpdate", {"quantity": 0}) == []
    assert problems(spec, "Product", patched.json()) == [] and patched.json()["status"] == "out_of_stock"
    assert editor.delete(f"{url}/{row['id']}", headers=admin).status_code == 204


def test_every_error_the_api_returns_matches_the_error_schema(ctx, spec):
    app, editor, _ = ctx
    url = f"/applications/{app['id']}/preview/data/{PRODUCT}"
    admin = acting("a", "admin")
    body = {"name": "n", "sku": "dup", "quantity": 1, "reorder_level": 1}
    editor.post(url, json=body, headers=admin)
    errors = {
        403: editor.post(url, json=body, headers=acting("v", "viewer")),
        404: editor.get(f"{url}/00000000-0000-4000-8000-000000000000", headers=admin),
        409: editor.post(url, json=body, headers=admin),
        422: editor.post(url, json={"sku": "x"}, headers=admin),
    }
    for status, resp in errors.items():
        assert resp.status_code == status, (status, resp.text)
        assert problems(spec, "Error", resp.json()) == []
        documented = {k for p in spec["paths"].values() for op in p.values() for k in op["responses"]}
        assert str(status) in documented


CASES = [  # (create body, should be accepted)
    ({"name": "a", "sku": "s1", "quantity": 1, "reorder_level": 1}, True),
    ({"name": "a", "sku": "s2", "quantity": 1, "reorder_level": 1, "owner": "someone"}, True),
    ({"name": "a", "sku": "s3", "quantity": 0, "reorder_level": 0}, True),
    ({"sku": "s4", "quantity": 1, "reorder_level": 1}, False),  # name missing
    ({"name": "a", "sku": "s5", "quantity": -1, "reorder_level": 1}, False),  # below minimum
    ({"name": "a", "sku": "s6", "quantity": "1", "reorder_level": 1}, False),  # wrong type
    ({"name": "x" * 201, "sku": "s7", "quantity": 1, "reorder_level": 1}, False),  # too long
    ({"name": "a", "sku": "s8", "quantity": 1, "reorder_level": 1, "status": "low"}, False),  # computed
    ({"name": "a", "sku": "s9", "quantity": 1, "reorder_level": 1, "id": "x"}, False),  # system-managed
    ({"name": "a", "sku": "s10", "quantity": 1, "reorder_level": 1, "bogus": 1}, False),  # unknown field
    ({"name": "a", "sku": "s11", "quantity": True, "reorder_level": 1}, False),  # bool is not an integer
    ({"name": "a", "sku": "s12", "quantity": 1.5, "reorder_level": 1}, False),  # not an integer
]


@pytest.mark.parametrize("body,accepted", CASES)
def test_documented_request_schema_agrees_with_the_runtime(ctx, spec, body, accepted):
    app, editor, _ = ctx
    r = editor.post(f"/applications/{app['id']}/preview/data/{PRODUCT}", json=body, headers=acting("a", "admin"))
    assert (r.status_code == 201) is accepted, r.text
    assert (problems(spec, "ProductCreate", body) == []) is accepted, problems(spec, "ProductCreate", body)


def test_documented_query_parameters_are_accepted(ctx, spec):
    app, editor, _ = ctx
    url = f"/applications/{app['id']}/preview/data/{PRODUCT}"
    editor.post(url, json={"name": "n", "sku": "k", "quantity": 3, "reorder_level": 1}, headers=acting("a", "admin"))
    params = spec["paths"]["/data/core.Product"]["get"]["parameters"]
    names = [p["name"] for p in params]
    assert {"limit", "offset", "order_by", "desc", "f.sku", "f.quantity", "f.owner"} <= set(names)
    assert "f.status" not in names  # a computed field is not filterable, and the runtime agrees:
    assert editor.get(url + "?f.status=low", headers=acting("a", "admin")).status_code == 422
    for field in next(p for p in params if p["name"] == "order_by")["schema"]["enum"]:
        assert editor.get(url + f"?order_by={field}&desc=true&limit=5&offset=0", headers=acting("a", "admin")).status_code == 200
    assert editor.get(url + "?f.sku=k&f.quantity=3", headers=acting("a", "admin")).json()["total"] == 1


def test_the_cli_writes_the_same_document(tmp_path, model):
    import io

    from orchivio.cli import main
    from tests_support import EXAMPLES

    out = tmp_path / "openapi.json"
    assert main(["openapi", str(EXAMPLES / "model.yaml"), "-o", str(out)], io.StringIO()) == 0
    written = json.loads(out.read_text(encoding="utf-8"))
    validate(written)
    assert written["paths"] == build_openapi(model)["paths"]
