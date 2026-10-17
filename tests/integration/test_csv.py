"""CSV import (per-row errors, modes, dry run) and permissioned, audited export."""

import copy

import pytest
from sqlalchemy import select
from test_app_auth import add_user, ready_client
from test_tenancy import make_org

from orchivio.db.models import AuditEvent
from orchivio.model import AppModel, assign_element_ids, diff_models, migrate_to_latest
from orchivio.runtime import outbox


def csv_model(raw):
    m = copy.deepcopy(raw)
    rules = {r["role"]: r for r in m["security"]["access"]}
    rules["admin"]["operations"] = ["read", "create", "update", "delete", "export"]
    rules["editor"].pop("row_constraint", None)
    rules["editor"]["fields"] = {"read": ["name", "sku", "quantity", "reorder_level", "status"],
                                 "write": ["name", "sku", "quantity", "reorder_level"]}
    rules["viewer"]["operations"] = ["read", "export"]
    rules["viewer"]["fields"] = {"read": ["name", "sku", "status"]}
    return m


@pytest.fixture
def world(new_api, raw_model, app):
    owner = new_api("owner@example.com")
    org = make_org(owner)
    proj = owner.post(f"/organizations/{org['id']}/projects", json={"name": "Inv", "slug": "inventory"}).json()
    a = owner.post(f"/projects/{proj['id']}/applications", json={"key": "inventory", "name": "Inv", "model": csv_model(raw_model)})
    assert a.status_code == 201, a.text
    a = a.json()
    owner.post(f"/applications/{a['id']}/preview/reset", json={})
    editor = ready_client(app, owner, a, "ed", ["editor"])
    return a, owner, editor, raw_model


def listing(c):
    return c.get("/data/core.Product?order_by=sku").json()


GOOD = "name,sku,quantity,reorder_level\nWidget,w-1,3,5\nGadget,g-1,10,2\nGizmo,z-1,0,1\n"
MIXED = ("name,sku,quantity,reorder_level\n"
         "Good,ok-1,1,1\n"
         "Bad number,bn-1,abc,1\n"
         ",no-name,1,1\n"
         "Dup,ok-1,2,2\n"
         "Fine,ok-2,5,5\n")


def post_csv(c, entity, body, **params):
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    return c.post(f"/import/{entity}" + (f"?{qs}" if qs else ""), content=body.encode() if isinstance(body, str) else body,
                  headers={"Content-Type": "text/csv"})


# --- importing ---------------------------------------------------------------------------------


def test_a_clean_file_is_imported_through_the_normal_create_path(world, app):
    a, owner, ed, _ = world
    r = post_csv(ed, "core.Product", GOOD)
    assert r.status_code == 200, r.text
    out = r.json()
    assert (out["total_rows"], out["imported"], out["failed"], out["committed"]) == (3, 3, 0, True)
    assert out["columns"] == {"name": "name", "sku": "sku", "quantity": "quantity", "reorder_level": "reorder_level"}
    items = listing(ed)["items"]
    assert [i["sku"] for i in items] == ["g-1", "w-1", "z-1"] and items[1]["quantity"] == 3
    # events and audit come from the same path as API writes
    assert [e["type"] for e in outbox.pending(app.state.preview_store.engine)] == ["core.Product.created"] * 3
    with app.state.session_factory() as s:
        actions = [e.action for e in s.scalars(select(AuditEvent).order_by(AuditEvent.id))]
    assert actions.count("import.completed") == 1 and actions.count("record.created") == 3


def test_all_or_nothing_reports_every_bad_row_and_keeps_nothing(world, app):
    a, owner, ed, _ = world
    r = post_csv(ed, "core.Product", MIXED)
    assert r.status_code == 422 and r.json()["error"]["code"] == "import_rejected"
    detail = r.json()["error"]["details"]
    assert (detail["total_rows"], detail["imported"], detail["failed"], detail["committed"]) == (5, 0, 3, False)
    errs = {e["row"]: e for e in detail["errors"]}
    assert set(errs) == {2, 3, 4}  # the data row numbers, not counting the header
    assert errs[2]["line"] == 3 and errs[2]["errors"] == [{"field": "quantity", "message": "must be a whole number"}]
    assert {"field": "name", "message": "is required"} in errs[3]["errors"]
    assert errs[4]["errors"][0]["message"].startswith("a record with the same value")  # duplicate of row 1, inside the file
    assert listing(ed)["total"] == 0
    assert outbox.pending(app.state.preview_store.engine) == []  # no events for rows that were rolled back


def test_skip_errors_keeps_the_good_rows(world):
    a, owner, ed, _ = world
    r = post_csv(ed, "core.Product", MIXED, mode="skip_errors")
    assert r.status_code == 200
    out = r.json()
    assert (out["imported"], out["failed"], out["committed"]) == (2, 3, True)
    assert [i["sku"] for i in listing(ed)["items"]] == ["ok-1", "ok-2"]


def test_dry_run_changes_nothing_but_finds_in_file_duplicates(world, app):
    a, owner, ed, _ = world
    r = post_csv(ed, "core.Product", MIXED, dry_run="true")
    assert r.status_code == 200
    out = r.json()
    # a dry run predicts the real run: in the default mode one bad row means nothing would be imported
    assert (out["dry_run"], out["committed"], out["imported"], out["failed"]) == (True, False, 0, 3)
    assert post_csv(ed, "core.Product", MIXED, dry_run="true", mode="skip_errors").json()["imported"] == 2
    assert listing(ed)["total"] == 0 and outbox.pending(app.state.preview_store.engine) == []
    clean = post_csv(ed, "core.Product", GOOD, dry_run="true").json()
    assert clean["imported"] == 3 and clean["committed"] is False and listing(ed)["total"] == 0
    with_skip = post_csv(ed, "core.Product", MIXED, dry_run="true", mode="skip_errors").json()
    assert with_skip["committed"] is False and listing(ed)["total"] == 0


def test_existing_records_are_conflicts_not_overwritten(world):
    a, owner, ed, _ = world
    post_csv(ed, "core.Product", GOOD)
    r = post_csv(ed, "core.Product", "name,sku,quantity,reorder_level\nOther,w-1,99,1\n")
    assert r.status_code == 422 and r.json()["error"]["details"]["errors"][0]["errors"][0]["message"].startswith("a record with the same")
    assert listing(ed)["items"][1]["quantity"] == 3


def test_header_rules_and_mapping(world):
    a, owner, ed, _ = world
    body = "Item,SKU,Qty,reorder_level\nWidget,w-1,3,5\n"
    unknown = post_csv(ed, "core.Product", body)
    assert unknown.status_code == 422 and unknown.json()["error"]["code"] == "invalid_csv"
    assert {p["column"] for p in unknown.json()["error"]["details"]} == {"Item", "Qty"}  # SKU matches case-insensitively
    mapped = post_csv(ed, "core.Product", body, **{"map.Item": "name", "map.Qty": "quantity"})
    assert mapped.status_code == 200 and mapped.json()["columns"]["Item"] == "name"
    assert listing(ed)["total"] == 1
    ignored = post_csv(ed, "core.Product", "name,sku,quantity,reorder_level,notes\nW2,w-2,1,1,hello\n", ignore_unknown="true")
    assert ignored.status_code == 200 and "notes" not in ignored.json()["columns"]
    dropped = post_csv(ed, "core.Product", "name,sku,quantity,reorder_level,notes\nW3,w-3,1,1,x\n", **{"map.notes": ""})
    assert dropped.status_code == 200
    for body, why in (("name,name,sku,quantity,reorder_level\na,b,s,1,1\n", "maps to"),
                      ("name,sku,quantity,reorder_level,status\na,s,1,1,ok\n", "may import"),
                      ("name,sku,quantity,reorder_level,created_at\na,s,1,1,2020-01-01\n", "may import"),
                      ("name,sku,quantity,reorder_level,owner\na,s,1,1,x\n", "may import")):  # owner is not writable by editors
        r = post_csv(ed, "core.Product", body)
        assert r.status_code == 422 and any(why in p["message"] for p in r.json()["error"]["details"]), body
    assert post_csv(ed, "core.Product", "").json()["error"]["message"] == "the file is empty"
    assert "no rows" in post_csv(ed, "core.Product", "name,sku\n").json()["error"]["message"]
    assert post_csv(ed, "core.Product", "foo,bar\n1,2\n").status_code == 422


def test_file_formats(world):
    a, owner, ed, _ = world
    bom = b"\xef\xbb\xbfname,sku,quantity,reorder_level\r\nWidget,w-1,3,5\r\n"
    assert post_csv(ed, "core.Product", bom).status_code == 200
    semi = post_csv(ed, "core.Product", "name;sku;quantity;reorder_level\nG;g-1;1;1\n", delimiter=";")
    assert semi.status_code == 200
    tab = post_csv(ed, "core.Product", "name\tsku\tquantity\treorder_level\nT\tt-1\t1\t1\n", delimiter="tab")
    assert tab.status_code == 200
    quoted = post_csv(ed, "core.Product", 'name,sku,quantity,reorder_level\n"Big, ""quoted""\nname",q-1,1,1\n\n,,,\n')
    assert quoted.status_code == 200 and quoted.json()["total_rows"] == 1  # blank lines are skipped
    assert any(i["name"] == 'Big, "quoted"\nname' for i in listing(ed)["items"])
    assert post_csv(ed, "core.Product", "a,b\n1,2\n", delimiter="x").json()["error"]["code"] == "invalid_delimiter"
    assert post_csv(ed, "core.Product", "name,sku\nA\x00,b\n").json()["error"]["code"] == "invalid_csv"
    assert post_csv(ed, "core.Product", b"name,sku\n\xff\xfe,b\n").json()["error"]["message"] == "the file must be UTF-8 text"
    assert post_csv(ed, "core.Product", "name,sku,quantity,reorder_level\na,b,1,1\n", mode="replace").json()["error"]["code"] == "invalid_csv"
    short = post_csv(ed, "core.Product", "name,sku,quantity,reorder_level\nOnly name\n")
    assert short.status_code == 422 and {"field": "sku", "message": "is required"} in short.json()["error"]["details"]["errors"][0]["errors"]
    long = post_csv(ed, "core.Product", "name,sku,quantity,reorder_level\na,b,1,1,EXTRA\n")
    assert long.status_code == 422 and "cells but only" in long.json()["error"]["details"]["errors"][0]["errors"][0]["message"]


def test_size_and_row_limits(world, app):
    a, owner, ed, _ = world
    app.state.settings.csv_max_rows = 2
    r = post_csv(ed, "core.Product", GOOD)
    assert r.status_code == 422 and "more than 2 rows" in r.json()["error"]["message"]
    app.state.settings.csv_max_bytes = 50
    assert post_csv(ed, "core.Product", GOOD).status_code == 413
    app.state.settings.csv_max_bytes = 2 * 1024 * 1024
    app.state.settings.csv_max_rows = 5000


def test_import_is_authorized_like_any_write(world, app):
    a, owner, ed, _ = world
    viewer = ready_client(app, owner, a, "vera", ["viewer"])
    denied = post_csv(viewer, "core.Product", GOOD)
    assert denied.status_code == 403 and listing(ed)["total"] == 0
    assert post_csv(ed, "core.Nope", GOOD).status_code == 404
    from fastapi.testclient import TestClient
    assert TestClient(app).post(f"/api/v1/apps/{a['id']}/import/core.Product", content=GOOD).status_code == 401
    fresh = add_user(owner, a, "temp", ["editor"])
    from test_app_auth import AppClient
    c = AppClient(app, a["id"])
    c.login("temp")
    assert post_csv(c, "core.Product", GOOD).json()["error"]["code"] == "password_change_required"
    assert fresh["must_change_password"] is True


# --- exporting ---------------------------------------------------------------------------------


def test_export_needs_the_export_permission_and_respects_field_access(world, app):
    a, owner, ed, _ = world
    post_csv(ed, "core.Product", GOOD)
    assert ed.get("/export/core.Product").status_code == 403  # editors can read, but export is a separate grant
    viewer = ready_client(app, owner, a, "vera", ["viewer"])
    r = viewer.get("/export/core.Product")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert r.headers["content-disposition"] == 'attachment; filename="core-product.csv"'
    lines = r.content.decode("utf-8-sig").splitlines()
    assert lines[0] == "id,name,sku,status" and len(lines) == 4  # only the fields this role may read (+ the key)
    admin = ready_client(app, owner, a, "ada", ["admin"])
    full = admin.get("/export/core.Product").content.decode("utf-8-sig").splitlines()[0].split(",")
    assert {"quantity", "owner", "created_at"} <= set(full)
    with app.state.session_factory() as s:
        events = [e for e in s.scalars(select(AuditEvent)) if e.action in ("export.completed", "records.exported")]
    assert sorted(e.action for e in events) == ["export.completed", "export.completed", "records.exported", "records.exported"]
    assert all(e.meta.get("rows") in (3, None) for e in events if e.action == "export.completed")


def test_export_neutralises_formula_injection(world, app):
    a, owner, ed, _ = world
    post_csv(ed, "core.Product", 'name,sku,quantity,reorder_level\n"=HYPERLINK(""http://evil"")",f-1,1,1\n"+1+1",f-2,1,1\n"@SUM(A1)",f-3,1,1\nnormal,f-4,1,1\n')
    viewer = ready_client(app, owner, a, "vera", ["viewer"])
    body = viewer.get("/export/core.Product").content.decode("utf-8-sig")
    assert "'=HYPERLINK" in body and "'+1+1" in body and "'@SUM" in body
    assert ",normal,f-4," in body
    assert not any(line.startswith(("=", "+", "@")) for line in body.splitlines())


def test_export_row_limit(world, app):
    a, owner, ed, _ = world
    post_csv(ed, "core.Product", GOOD)
    viewer = ready_client(app, owner, a, "vera", ["viewer"])
    app.state.settings.csv_max_export_rows = 2
    r = viewer.get("/export/core.Product")
    assert r.status_code == 422 and "limited" in r.json()["error"]["message"]
    app.state.settings.csv_max_export_rows = 10_000


# --- the Studio preview ------------------------------------------------------------------------


def test_preview_import_and_export_act_as_the_simulated_role(world, app):
    a, owner, _, _ = world
    h = {"X-Preview-User": "x", "X-Preview-Roles": "editor", "Content-Type": "text/csv"}
    r = owner.post(f"/applications/{a['id']}/preview/import/core.Product?dry_run=true", content=GOOD.encode(), headers=h)
    assert r.status_code == 200 and r.json()["imported"] == 3
    assert owner.post(f"/applications/{a['id']}/preview/import/core.Product", content=GOOD.encode(), headers=h).json()["committed"] is True
    denied = owner.get(f"/applications/{a['id']}/preview/export/core.Product", headers={"X-Preview-Roles": "editor"})
    assert denied.status_code == 403
    ok = owner.get(f"/applications/{a['id']}/preview/export/core.Product", headers={"X-Preview-Roles": "viewer"})
    assert ok.status_code == 200 and ok.content.decode("utf-8-sig").splitlines()[0] == "id,name,sku,status"
    with app.state.session_factory() as s:
        assert {"preview.import", "preview.export"} <= {e.action for e in s.scalars(select(AuditEvent))}


# --- the model ---------------------------------------------------------------------------------


def test_granting_export_is_a_permission_expansion(raw_model):
    base = assign_element_ids(AppModel.model_validate(migrate_to_latest(raw_model)))
    m = copy.deepcopy(raw_model)
    next(r for r in m["security"]["access"] if r["role"] == "viewer")["operations"].append("export")
    new = assign_element_ids(AppModel.model_validate(migrate_to_latest(m)), previous=base)
    assert diff_models(base, new).level.name == "A3"


def test_system_set_columns_are_refused_up_front(world, app):
    a, owner, ed, _ = world
    admin = ready_client(app, owner, a, "ada", ["admin"])
    r = post_csv(admin, "core.Product", "name,sku,quantity,reorder_level,owner,created_at\nA,s,1,1,u,2020-01-01\n")
    assert r.status_code == 422 and any("set by the system" in p["message"] for p in r.json()["error"]["details"])
    ok = post_csv(admin, "core.Product", f"name,sku,quantity,reorder_level,owner\nA,s,1,1,{admin.get('/auth/me').json()['id']}\n")
    assert ok.status_code == 200 and ok.json()["imported"] == 1
