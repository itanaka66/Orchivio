"""Field-level read/write access (FR-064): hidden fields are removed, not blanked, on every path."""

import copy

import pytest
from test_app_auth import ready_client
from test_tenancy import make_org

from orchivio.model import AppModel, assign_element_ids, diff_models, migrate_to_latest
from orchivio.security import check_consistency, resolve_fields


def with_fields(raw):
    m = copy.deepcopy(raw)
    rules = {r["role"]: r for r in m["security"]["access"]}
    rules["viewer"]["fields"] = {"read": ["name", "sku", "status"]}
    rules["editor"]["fields"] = {"read": ["name", "sku", "quantity", "reorder_level", "status"],
                                 "write": ["name", "sku", "quantity", "reorder_level"]}
    rules["editor"].pop("row_constraint", None)  # keep the focus on fields
    return m


@pytest.fixture
def world(new_api, raw_model, app):
    owner = new_api("owner@example.com")
    org = make_org(owner)
    proj = owner.post(f"/organizations/{org['id']}/projects", json={"name": "Inv", "slug": "inventory"}).json()
    a = owner.post(f"/projects/{proj['id']}/applications", json={"key": "inventory", "name": "Inv", "model": with_fields(raw_model)})
    assert a.status_code == 201, a.text
    a = a.json()
    owner.post(f"/applications/{a['id']}/preview/reset", json={})
    admin = ready_client(app, owner, a, "ada", ["admin"])
    row = admin.post("/data/core.Product", json={"name": "Widget", "sku": "w-1", "quantity": 3, "reorder_level": 5,
                                                  "owner": admin.get("/auth/me").json()["id"]})
    assert row.status_code == 201, row.text
    return a, owner, admin, row.json()["id"], raw_model


def keys(d):
    return set(d)


# --- reading -----------------------------------------------------------------------------------


def test_hidden_fields_are_absent_from_every_read_response(world, app):
    a, owner, admin, pid, _ = world
    viewer = ready_client(app, owner, a, "vera", ["viewer"])
    editor = ready_client(app, owner, a, "ed", ["editor"])
    assert keys(admin.get(f"/data/core.Product/{pid}").json()) >= {"quantity", "reorder_level", "owner", "created_at"}
    for body in (viewer.get(f"/data/core.Product/{pid}").json(), viewer.get("/data/core.Product").json()["items"][0]):
        assert keys(body) == {"id", "name", "sku", "status"}  # the key is gone, not null
    assert keys(editor.get(f"/data/core.Product/{pid}").json()) == {"id", "name", "sku", "quantity", "reorder_level", "status"}
    # "view as role" in the Studio preview is the same code path
    seen = owner.get(f"/applications/{a['id']}/preview/data/core.Product", headers={"X-Preview-User": "x", "X-Preview-Roles": "viewer"})
    assert keys(seen.json()["items"][0]) == {"id", "name", "sku", "status"}


def test_hidden_fields_cannot_be_probed_by_filtering_or_sorting(world, app):
    a, owner, admin, pid, _ = world
    viewer = ready_client(app, owner, a, "vera", ["viewer"])
    hidden = viewer.get("/data/core.Product?f.quantity=3")
    unknown = viewer.get("/data/core.Product?f.bogus=3")
    assert hidden.status_code == unknown.status_code == 422
    assert hidden.json()["error"]["details"][0]["message"] == unknown.json()["error"]["details"][0]["message"]
    assert viewer.get("/data/core.Product?f.sku=w-1").json()["total"] == 1  # visible fields still filter
    assert viewer.get("/data/core.Product?order_by=quantity").status_code == 422
    assert viewer.get("/data/core.Product?order_by=name").status_code == 200
    assert admin.get("/data/core.Product?f.quantity=3&order_by=quantity").json()["total"] == 1


# --- writing -----------------------------------------------------------------------------------


def test_writes_to_fields_outside_the_allow_list_are_refused(world, app):
    a, owner, admin, pid, _ = world
    editor = ready_client(app, owner, a, "ed", ["editor"])
    ok = editor.patch(f"/data/core.Product/{pid}", json={"quantity": 9})
    assert ok.status_code == 200 and ok.json()["quantity"] == 9 and "owner" not in ok.json()  # response honours read access
    readonly = editor.patch(f"/data/core.Product/{pid}", json={"status": "ok"})
    hidden = editor.patch(f"/data/core.Product/{pid}", json={"owner": "someone"})
    bogus = editor.patch(f"/data/core.Product/{pid}", json={"bogus": 1})
    assert readonly.status_code == hidden.status_code == bogus.status_code == 422
    assert readonly.json()["error"]["details"] == [{"field": "status", "message": "read-only for your roles"}]
    assert hidden.json()["error"]["details"][0]["message"] == "unknown field"  # cannot read it, so it "does not exist"
    assert bogus.json()["error"]["details"][0]["message"] == "unknown field"
    assert admin.get(f"/data/core.Product/{pid}").json()["quantity"] == 9  # the refused patches changed nothing


def test_create_checks_writable_fields_too(world, app):
    a, owner, admin, pid, _ = world
    editor = ready_client(app, owner, a, "ed", ["editor"])
    base = {"name": "N", "sku": "n-1", "quantity": 1, "reorder_level": 1}
    r = editor.post("/data/core.Product", json={**base, "owner": admin.get("/auth/me").json()["id"]})
    assert r.status_code == 422 and r.json()["error"]["details"][0]["field"] == "owner"
    r = editor.post("/data/core.Product", json=base)  # owner falls back to its default
    assert r.status_code == 201 and "owner" not in r.json()


# --- flows act as the caller -------------------------------------------------------------------


def test_flows_cannot_filter_on_fields_the_caller_may_not_read(world, app):
    a, owner, admin, pid, _ = world
    viewer = ready_client(app, owner, a, "vera", ["viewer"])
    out = viewer.post("/flows/ReorderLowStock/run", json={"args": {"min_gap": 0}}).json()
    assert out["status"] == "failed" and "may not read" in out["error"] and "quantity" in out["error"]
    editor = ready_client(app, owner, a, "ed", ["editor"])
    assert editor.post("/flows/ReorderLowStock/run", json={"args": {"min_gap": 0}}).json()["status"] == "succeeded"


# --- the model ---------------------------------------------------------------------------------


def identified(raw):
    return assign_element_ids(AppModel.model_validate(migrate_to_latest(raw)))


def test_validation_of_field_lists(world, app):
    a, owner, _, _, raw = world
    m = with_fields(raw)
    rules = {r["role"]: r for r in m["security"]["access"]}
    rules["viewer"]["fields"] = {"read": ["name", "nope", "name"], "write": ["name"]}
    codes = {i["code"] for i in owner.post("/models/validate", json={"model": m}).json()["issues"] if i["severity"] == "error"}
    assert {"unknown-access-field", "duplicate-access-field", "access-field-unused"} <= codes
    rules["viewer"]["fields"] = {"read": "name"}
    assert owner.post("/models/validate", json={"model": m}).status_code == 422  # a list, not a string
    rules["viewer"]["fields"] = {"read": [], "surprise": 1}
    assert owner.post("/models/validate", json={"model": m}).status_code == 422


def test_widening_field_access_needs_a3(raw_model):
    narrow = identified(with_fields(raw_model))

    def widen(change):
        m = with_fields(raw_model)
        change({r["role"]: r for r in m["security"]["access"]}["viewer"])
        return diff_models(narrow, assign_element_ids(AppModel.model_validate(migrate_to_latest(m)), previous=narrow))

    assert widen(lambda r: r["fields"].update(read=["name", "sku", "status", "quantity"])).level.name == "A3"
    assert widen(lambda r: r.pop("fields")).level.name == "A3"  # unrestricted again
    assert widen(lambda r: r["fields"].update(read=["name"])).level.name == "A2"  # narrowing is an ordinary change
    assert widen(lambda r: r["fields"].update(write=["name"])).level.name == "A2"  # no write op granted: only a lint error elsewhere


def test_resolve_fields_semantics(raw_model):
    m = identified(with_fields(raw_model))
    assert resolve_fields(m, ["viewer"], "core.Product", "read") == {"name", "sku", "status"}
    assert resolve_fields(m, ["admin"], "core.Product", "read") is None
    assert resolve_fields(m, ["viewer", "admin"], "core.Product", "read") is None  # one unrestricted rule wins
    assert resolve_fields(m, ["viewer", "editor"], "core.Product", "read") == {"name", "sku", "quantity", "reorder_level", "status"}
    assert resolve_fields(m, ["viewer"], "core.Product", "update") == frozenset()  # no grant, nothing writable
    assert resolve_fields(m, ["editor"], "core.Product", "update") == {"name", "sku", "quantity", "reorder_level"}
    assert resolve_fields(m, ["ghost"], "core.Product", "read") == frozenset()
    assert resolve_fields(m, ["editor"], "core.Product", "delete") is None


def test_consistency_findings_for_field_traps(raw_model):
    m = with_fields(raw_model)
    rules = {r["role"]: r for r in m["security"]["access"]}
    rules["viewer"]["fields"] = {"read": ["name", "status"]}  # status is computed from quantity and reorder_level
    rules["editor"]["fields"] = {"read": ["name"], "write": ["name"]}  # cannot write the other required fields
    found = {(f.code, f.path) for f in check_consistency(identified(m))}
    assert any(c == "computed-field-leak" for c, _ in found)
    assert any(c == "required-field-not-writable" for c, _ in found)
    clean = {f.code for f in check_consistency(identified(with_fields(raw_model)))}
    assert "required-field-not-writable" not in clean


def test_openapi_documents_field_access(raw_model):
    from orchivio.codegen import build_openapi

    spec = build_openapi(identified(with_fields(raw_model)))
    schemas = spec["components"]["schemas"]
    product = next(v for k, v in schemas.items() if k.endswith("Product"))
    assert "quantity" in product["properties"]  # known to some roles
    assert "quantity" not in product["required"] and "name" in product["required"] and "id" in product["required"]
    ops = [op for path in spec["paths"].values() for op in path.values() if isinstance(op, dict) and "x-orchivio-field-access" in op]
    assert ops and any(o["x-orchivio-field-access"]["restricted_roles"].get("viewer") == ["name", "sku", "status"] for o in ops)
