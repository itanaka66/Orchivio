"""Page and action enforcement: navigation, rows, forms, actions and direct flow runs, decided on the server."""

import copy

import pytest
from test_app_auth import AppClient, ready_client
from test_tenancy import make_org

from orchivio.model import AppModel, assign_element_ids, diff_models, migrate_to_latest, validate_model
from orchivio.security import page_access


def paged(raw):
    m = copy.deepcopy(raw)
    mod = m["modules"][0]
    mod["pages"] = [
        {"key": "product-list", "path": "/products", "type": "list", "entity": "Product", "layout": "main",
         "columns": ["name", "sku", "quantity", "status"],
         "actions": ["view", "create", {"key": "edit", "visible_expr": "$currentUser.hasRole('editor') || $currentUser.hasRole('admin')"},
                     {"key": "delete"},
                     {"key": "restock", "visible_expr": "quantity <= reorder_level", "flow": "ReorderLowStock", "args": {"min_gap": "0"}},
                     {"key": "note"}]},
        {"key": "product-create", "path": "/products/new", "type": "form", "entity": "Product", "mode": "create",
         "columns": ["name", "sku", "quantity", "reorder_level"]},
        {"key": "product-edit", "path": "/products/edit", "type": "form", "entity": "Product", "mode": "edit", "columns": ["name", "quantity"],
         "actions": [{"key": "edit", "editable_expr": "quantity < 1000"}]},
        {"key": "product-detail", "path": "/products/detail", "type": "detail", "entity": "Product", "columns": ["name", "sku"]},
        {"key": "audit-list", "path": "/audit", "type": "list", "entity": "Product"},
    ]
    m["security"]["pages"] = {"product-list": ["viewer", "editor", "admin"], "product-create": ["editor", "admin"],
                              "product-edit": ["editor", "admin"], "product-detail": ["viewer", "editor", "admin"], "audit-list": ["admin"]}
    rules = {r["role"]: r for r in m["security"]["access"]}
    rules["editor"].pop("row_constraint", None)
    rules["admin"]["operations"] = ["read", "create", "update", "delete"]
    return m


@pytest.fixture
def world(new_api, raw_model, app):
    owner = new_api("owner@example.com")
    org = make_org(owner)
    proj = owner.post(f"/organizations/{org['id']}/projects", json={"name": "Inv", "slug": "inventory"}).json()
    r = owner.post(f"/projects/{proj['id']}/applications", json={"key": "inventory", "name": "Inv", "model": paged(raw_model)})
    assert r.status_code == 201, r.text
    a = r.json()
    owner.post(f"/applications/{a['id']}/preview/reset", json={})
    users = {n: ready_client(app, owner, a, n, [role]) for n, role in (("vera", "viewer"), ("ed", "editor"), ("ada", "admin"))}
    ada = users["ada"]
    me = ada.get("/auth/me").json()["id"]
    for sku, qty, reorder in (("low", 1, 5), ("fine", 50, 5)):
        r = ada.post("/data/core.Product", json={"name": sku.title(), "sku": sku, "quantity": qty, "reorder_level": reorder, "owner": me})
        assert r.status_code == 201, r.text
    return a, owner, users, raw_model


def keys(c):
    return [p["key"] for p in c.get("/pages").json()["items"]]


def ids(c):
    return {i["sku"]: i["id"] for i in c.get("/data/core.Product").json()["items"]}


# --- navigation --------------------------------------------------------------------------------


def test_the_menu_shows_only_pages_the_caller_may_open(world):
    a, owner, u, _ = world
    assert keys(u["vera"]) == ["product-list", "product-detail"]
    assert keys(u["ed"]) == ["product-list", "product-create", "product-edit", "product-detail"]
    assert keys(u["ada"]) == ["product-list", "product-create", "product-edit", "product-detail", "audit-list"]
    item = u["vera"].get("/pages").json()["items"][0]
    assert item["layout"] == "main" and item["entity"] == "core.Product" and item["columns"] == ["name", "sku", "quantity", "status"]
    assert item["actions"] == ["view", "restock", "note"]


def test_actions_listed_in_the_menu_follow_entity_permissions(world):
    a, owner, u, _ = world
    acts = lambda c: next(p for p in c.get("/pages").json()["items"] if p["key"] == "product-list")["actions"]  # noqa: E731
    assert acts(u["vera"]) == ["view", "restock", "note"]  # no create/edit/delete rights, so those verbs are not offered
    assert acts(u["ed"]) == ["view", "create", "edit", "restock", "note"]
    assert acts(u["ada"]) == ["view", "create", "edit", "delete", "restock", "note"]


def test_a_page_without_a_matching_role_or_entity_right_does_not_exist_for_the_caller(world, app):
    a, owner, u, _ = world
    for key in ("audit-list", "product-create", "product-edit", "no-such-page"):
        assert u["vera"].get(f"/pages/{key}").status_code == 404, key
    assert u["vera"].get("/pages/audit-list/rows").status_code == 404
    assert u["ed"].get("/pages/audit-list").json() == u["ed"].get("/pages/no-such-page").json()  # same answer
    assert u["ada"].get("/pages/audit-list").status_code == 200
    assert AppClient(app, a["id"]).get("/pages").status_code == 401


def test_page_access_also_needs_the_entity_operation_the_page_implies(raw_model):
    m = paged(raw_model)
    m["security"]["pages"]["product-create"] = ["viewer", "editor"]  # viewers have no create right
    model = assign_element_ids(AppModel.model_validate(migrate_to_latest(m)))
    page = next(p for p in model.modules[0].pages if p.key == "product-create")
    assert page_access(model, ["viewer"], page).allowed is False
    assert page_access(model, ["viewer"], page).reason == "entity:create"
    assert page_access(model, ["editor"], page).allowed is True
    assert page_access(model, ["ghost"], page).reason == "page"
    edit = next(p for p in model.modules[0].pages if p.key == "product-edit")
    assert page_access(model, ["editor", "admin"], edit).allowed is True


# --- rows --------------------------------------------------------------------------------------


def test_list_rows_show_only_the_page_columns_and_per_row_action_state(world):
    a, owner, u, _ = world
    rows = {r["sku"]: r for r in u["ed"].get("/pages/product-list/rows").json()["items"]}
    assert set(rows["low"]) == {"id", "name", "sku", "quantity", "status", "_actions"}  # not reorder_level, not owner
    assert set(rows["low"]["_actions"]) == {"view", "create", "edit", "restock", "note"}
    assert "restock" not in rows["fine"]["_actions"]  # its visible_expr is false for this row
    assert rows["low"]["_actions"]["edit"] == {"editable": True}
    vera = {r["sku"]: r["_actions"] for r in u["vera"].get("/pages/product-list/rows").json()["items"]}
    assert set(vera["low"]) == {"view", "restock", "note"} and "edit" not in vera["low"]
    ada = {r["sku"]: r["_actions"] for r in u["ada"].get("/pages/product-list/rows").json()["items"]}
    assert "delete" in ada["fine"]


def test_filters_and_sorting_are_limited_to_the_pages_columns(world):
    a, owner, u, _ = world
    assert u["ed"].get("/pages/product-list/rows?f.sku=low").json()["total"] == 1
    assert u["ed"].get("/pages/product-list/rows?order_by=quantity&desc=true").json()["items"][0]["sku"] == "fine"
    for q in ("f.reorder_level=5", "order_by=owner", "f.owner=x"):
        assert u["ed"].get(f"/pages/product-list/rows?{q}").status_code == 422, q


def test_detail_pages_return_one_row_with_page_columns(world):
    a, owner, u, _ = world
    pid = ids(u["vera"])["low"]
    d = u["vera"].get(f"/pages/product-detail/rows/{pid}")
    assert d.status_code == 200 and set(d.json()) == {"id", "name", "sku", "_actions"}
    assert u["vera"].get(f"/pages/product-list/rows/{pid}").status_code == 409  # a list page has rows, not a single record view
    assert u["vera"].get("/pages/product-detail/rows/00000000-0000-0000-0000-000000000000").status_code == 404


# --- forms -------------------------------------------------------------------------------------


def test_forms_accept_only_their_declared_columns(world):
    a, owner, u, _ = world
    me = u["ed"].get("/auth/me").json()["id"]
    ok = u["ed"].post("/pages/product-create/rows", json={"name": "New", "sku": "new-1", "quantity": 3, "reorder_level": 1})
    assert ok.status_code == 201 and set(ok.json()) == {"id", "name", "sku", "quantity", "reorder_level"}  # owner is filled by its default
    extra = u["ed"].post("/pages/product-create/rows", json={"name": "N", "sku": "n-2", "quantity": 3, "reorder_level": 1, "owner": me})
    assert extra.status_code == 422 and extra.json()["error"]["details"] == [{"field": "owner", "message": "unknown field"}]
    assert u["vera"].post("/pages/product-create/rows", json={}).status_code == 404  # cannot even see the form
    assert u["ed"].post("/pages/product-list/rows", json={}).status_code == 409  # not a create form


def test_editing_enforces_columns_and_the_editable_expression(world, app):
    a, owner, u, _ = world
    pid = ids(u["ed"])["fine"]
    ok = u["ed"].patch(f"/pages/product-edit/rows/{pid}", json={"quantity": 60})
    assert ok.status_code == 200 and ok.json()["quantity"] == 60 and set(ok.json()) == {"id", "name", "quantity"}
    sku = u["ed"].patch(f"/pages/product-edit/rows/{pid}", json={"sku": "changed"})
    assert sku.status_code == 422 and sku.json()["error"]["details"][0]["field"] == "sku"  # the form does not offer it
    u["ada"].patch(f"/data/core.Product/{pid}", json={"quantity": 5000})
    locked = u["ed"].patch(f"/pages/product-edit/rows/{pid}", json={"name": "Nope"})
    assert locked.status_code == 403  # editable_expr: quantity < 1000 no longer holds
    assert u["ed"].patch("/pages/product-edit/rows/00000000-0000-0000-0000-000000000000", json={"name": "x"}).status_code == 404
    assert u["vera"].patch(f"/pages/product-edit/rows/{pid}", json={"name": "x"}).status_code == 404
    assert u["ed"].patch(f"/pages/product-list/rows/{pid}", json={"name": "x"}).status_code == 409


# --- actions -----------------------------------------------------------------------------------


def test_actions_run_flows_as_the_caller_when_visible(world):
    a, owner, u, _ = world
    low, fine = ids(u["ed"])["low"], ids(u["ed"])["fine"]
    ok = u["ed"].post("/pages/product-list/actions/restock", json={"row_id": low})
    assert ok.status_code == 200 and ok.json()["status"] == "succeeded", ok.text
    assert u["ed"].post("/pages/product-list/actions/restock", json={"row_id": fine}).status_code == 404  # not visible for this row
    assert u["ed"].post("/pages/product-list/actions/restock", json={}).json()["error"]["code"] == "row_required"
    assert u["ed"].post("/pages/product-list/actions/ghost", json={"row_id": low}).status_code == 404
    assert u["ed"].post("/pages/audit-list/actions/restock", json={"row_id": low}).status_code == 404
    assert u["ed"].post("/pages/product-list/actions/restock", json={"row_id": low, "extra": 1}).status_code == 422
    denied = u["vera"].post("/pages/product-list/actions/restock", json={"row_id": low})
    assert denied.status_code == 200 and denied.json()["status"] == "failed"  # the viewer may press it; the flow's own writes are still checked


def test_standard_actions_follow_entity_operations(world):
    a, owner, u, _ = world
    fine = ids(u["ed"])["fine"]
    assert u["ed"].post("/pages/product-list/actions/delete", json={"row_id": fine}).status_code == 404  # editors cannot delete
    assert u["vera"].post("/pages/product-list/actions/delete", json={"row_id": fine}).status_code == 404
    assert u["ada"].post("/pages/product-list/actions/delete", json={"row_id": fine}).json()["status"] == "succeeded"
    assert "fine" not in ids(u["ada"])
    assert u["ed"].post("/pages/product-list/actions/note", json={"row_id": ids(u["ed"])["low"]}).json()["error"]["code"] == "no_behaviour"


# --- flows are not callable by everyone ---------------------------------------------------------


def test_direct_flow_runs_need_run_roles(world, raw_model):
    a, owner, u, _ = world
    assert u["ed"].post("/flows/ReorderLowStock/run", json={"args": {"min_gap": 0}}).status_code == 200  # run_roles in the sample model
    m = paged(raw_model)
    m["modules"][0]["flows"][0]["run_roles"] = ["admin"]
    r = owner.post(f"/applications/{a['id']}/revisions", json={"model": m, "acknowledge_risks": True})
    assert r.status_code == 201, r.text
    owner.post(f"/applications/{a['id']}/preview/reset", json={})
    assert u["ed"].post("/flows/ReorderLowStock/run", json={"args": {"min_gap": 0}}).status_code == 404  # same as a flow that does not exist
    assert u["ed"].post("/flows/Nope/run", json={}).status_code == 404
    assert u["ada"].post("/flows/ReorderLowStock/run", json={"args": {"min_gap": 0}}).status_code == 200


def test_flows_without_run_roles_are_not_runnable_from_the_app_api(world, raw_model):
    a, owner, u, _ = world
    m = paged(raw_model)
    m["modules"][0]["flows"][0].pop("run_roles")
    owner.post(f"/applications/{a['id']}/revisions", json={"model": m, "acknowledge_risks": True})
    owner.post(f"/applications/{a['id']}/preview/reset", json={})
    assert u["ada"].post("/flows/ReorderLowStock/run", json={"args": {"min_gap": 0}}).status_code == 404  # deny by default, even for admins


# --- the model ---------------------------------------------------------------------------------


def errors(raw, mutate):
    m = paged(raw)
    mutate(m)
    return {i.code for i in validate_model(assign_element_ids(AppModel.model_validate(migrate_to_latest(m)))) if i.severity == "error"}


def action(m, key):
    return next(a for a in m["modules"][0]["pages"][0]["actions"] if isinstance(a, dict) and a["key"] == key)


@pytest.mark.parametrize("mutate, code", [
    (lambda m: action(m, "restock").update(flow="Ghost"), "page-action-flow"),
    (lambda m: action(m, "restock").update(args={}), "page-action-flow"),
    (lambda m: action(m, "restock").update(args={"min_gap": "0", "extra": "1"}), "page-action-flow"),
    (lambda m: action(m, "restock").update(args={"min_gap": "'text'"}), "expression"),
    (lambda m: action(m, "note").update(args={"x": "1"}), "page-action-flow"),
    (lambda m: m["modules"][0]["flows"][0].update(run_roles=["ghost"]), "unknown-role"),
    (lambda m: m["modules"][0]["flows"][0].update(kind="client", run_roles=["admin"]), "run-roles-not-runnable"),
])
def test_page_action_and_run_roles_rules(raw_model, mutate, code):
    assert code in errors(raw_model, mutate)


def test_exposing_a_flow_to_more_people_is_a_permission_expansion(raw_model):
    base = assign_element_ids(AppModel.model_validate(migrate_to_latest(paged(raw_model))))

    def level(fn):
        m = paged(raw_model)
        fn(m)
        return diff_models(base, assign_element_ids(AppModel.model_validate(migrate_to_latest(m)), previous=base)).level.name

    assert level(lambda m: action(m, "note").update(flow="ReorderLowStock", args={"min_gap": "0"})) == "A3"
    assert level(lambda m: m["modules"][0]["flows"][0].update(run_roles=["viewer", "editor", "admin", "viewer"])) == "A2"  # same roles
    assert level(lambda m: m["modules"][0]["flows"][0].update(run_roles=["admin"])) == "A2"  # narrower
    assert level(lambda m: m["modules"][0]["pages"][0].update(layout="other")) == "A2"  # an ordinary page change
    plain = copy.deepcopy(paged(raw_model))
    plain["modules"][0]["flows"][0]["run_roles"] = None
    base2 = assign_element_ids(AppModel.model_validate(migrate_to_latest(plain)))
    widened = paged(raw_model)
    assert diff_models(base2, assign_element_ids(AppModel.model_validate(migrate_to_latest(widened)), previous=base2)).level.name == "A3"
