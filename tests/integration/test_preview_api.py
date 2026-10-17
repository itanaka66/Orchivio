"""Preview runtime over HTTP: view-as-role simulation, isolation and staleness."""

import copy
import re

import pytest
from sqlalchemy import inspect
from test_tenancy import add_member, make_org


@pytest.fixture
def ctx(new_api, raw_model):
    owner = new_api("owner@example.com")
    org = make_org(owner)
    proj = owner.post(f"/organizations/{org['id']}/projects", json={"name": "Inv", "slug": "inventory"}).json()
    people = {"owner": owner}
    for role in ("editor", "viewer"):
        people[role] = new_api(f"{role}@example.com")
        add_member(owner, org, f"{role}@example.com", role)
    people["outsider"] = new_api("outsider@example.com")
    app = owner.post(f"/projects/{proj['id']}/applications", json={"key": "inventory", "name": "Inv", "model": raw_model}).json()
    return proj, app, people


def acting(user, *roles):
    return {"X-Preview-User": user, "X-Preview-Roles": ",".join(roles)}


def base(app):
    return f"/applications/{app['id']}/preview"


PRODUCT = "core.Product"
NEW = {"name": "Widget", "sku": "W-1", "quantity": 5, "reorder_level": 2}


def test_preview_must_be_built_before_use(ctx):
    _, app, p = ctx
    assert p["viewer"].get(f"{base(app)}/status").json() == {"built": False, "stale": False, "built_at": None}
    r = p["viewer"].get(f"{base(app)}/data/{PRODUCT}", headers=acting("u", "admin"))
    assert r.status_code == 409 and r.json()["error"]["code"] == "preview_not_built"


def test_reset_and_seed(ctx):
    _, app, p = ctx
    assert p["viewer"].post(f"{base(app)}/reset", json={}).status_code == 403  # editor+ only
    r = p["editor"].post(f"{base(app)}/reset", json={"seed": {"rows_per_entity": 12, "seed": 7, "users": ["u1", "u2"]}})
    assert r.status_code == 200 and r.json()["seeded"][PRODUCT] == {"inserted": 12, "skipped": 0}
    assert p["viewer"].get(f"{base(app)}/status").json()["built"] is True
    page = p["viewer"].get(f"{base(app)}/data/{PRODUCT}?limit=5", headers=acting("v", "viewer")).json()
    assert page["total"] == 12 and len(page["items"]) == 5 and page["limit"] == 5
    assert p["editor"].post(f"{base(app)}/reset", json={}).json()["seeded"] is None
    assert p["viewer"].get(f"{base(app)}/data/{PRODUCT}", headers=acting("v", "viewer")).json()["total"] == 0


def test_view_as_role_is_a_simulation_not_a_studio_privilege(ctx):
    _, app, p = ctx
    p["editor"].post(f"{base(app)}/reset", json={})
    url = f"{base(app)}/data/{PRODUCT}"
    # A Studio *owner* acting with no app roles (or an unknown one) has no data access.
    r = p["owner"].get(url, headers=acting("o"))
    assert r.status_code == 403 and r.json()["error"]["code"] == "forbidden"
    r = p["owner"].get(url, headers=acting("o", "superuser"))
    assert r.status_code == 422 and r.json()["error"]["code"] == "unknown_role"

    # The same Studio user can simulate different end users and gets different results.
    made = p["editor"].post(url, json=NEW, headers=acting("e1", "editor"))
    assert made.status_code == 201 and made.json()["owner"] == "e1" and made.json()["status"] == "in_stock"
    row = made.json()["id"]
    assert p["editor"].get(f"{url}/{row}", headers=acting("e1", "editor")).status_code == 200
    assert p["editor"].get(f"{url}/{row}", headers=acting("e2", "editor")).status_code == 404  # row-level rule
    assert p["editor"].get(f"{url}/{row}", headers=acting("v", "viewer")).status_code == 200
    assert p["editor"].patch(f"{url}/{row}", json={"quantity": 0}, headers=acting("e1", "editor")).json()["status"] == "out_of_stock"
    assert p["editor"].delete(f"{url}/{row}", headers=acting("e1", "editor")).status_code == 403
    assert p["editor"].delete(f"{url}/{row}", headers=acting("a", "admin")).status_code == 204
    assert p["editor"].get(f"{url}/{row}", headers=acting("a", "admin")).status_code == 404


def test_studio_roles_gate_who_may_use_the_preview(ctx):
    _, app, p = ctx
    p["editor"].post(f"{base(app)}/reset", json={})
    url = f"{base(app)}/data/{PRODUCT}"
    assert p["viewer"].get(url, headers=acting("v", "viewer")).status_code == 200  # reading is model.read
    assert p["viewer"].post(url, json=NEW, headers=acting("a", "admin")).status_code == 403  # writing needs editor
    assert p["outsider"].get(url, headers=acting("a", "admin")).status_code == 404
    assert p["outsider"].post(f"{base(app)}/reset", json={}).status_code == 404


def test_validation_errors_and_filters(ctx):
    _, app, p = ctx
    p["editor"].post(f"{base(app)}/reset", json={})
    url = f"{base(app)}/data/{PRODUCT}"
    admin = acting("a", "admin")
    bad = p["editor"].post(url, json={"sku": "S", "quantity": -1}, headers=admin)
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "invalid_record"
    fields = {d["field"] for d in bad.json()["error"]["details"]}
    assert {"name", "quantity", "reorder_level"} <= fields
    assert p["editor"].post(url, json=NEW, headers=admin).status_code == 201
    dup = p["editor"].post(url, json={**NEW, "name": "Dup"}, headers=admin)
    assert dup.status_code == 409 and dup.json()["error"]["code"] == "conflict"
    assert p["viewer"].get(url + "?f.sku=W-1", headers=acting("v", "viewer")).json()["total"] == 1
    assert p["viewer"].get(url + "?f.sku=nope", headers=acting("v", "viewer")).json()["total"] == 0
    assert p["viewer"].get(url + "?f.bogus=1", headers=acting("v", "viewer")).status_code == 422
    assert p["viewer"].get(url + "?limit=500", headers=acting("v", "viewer")).status_code == 422
    assert p["viewer"].get(f"{base(app)}/data/core.Nope", headers=admin).json()["error"]["code"] == "unknown_entity"


def test_preview_goes_stale_when_the_model_changes(ctx, raw_model):
    _, app, p = ctx
    p["editor"].post(f"{base(app)}/reset", json={})
    changed = copy.deepcopy(raw_model)
    changed["modules"][0]["entities"][0]["fields"].append({"name": "note", "type": "string"})
    assert p["editor"].post(f"/applications/{app['id']}/revisions", json={"model": changed}).status_code == 201
    assert p["viewer"].get(f"{base(app)}/status").json()["stale"] is True
    r = p["viewer"].get(f"{base(app)}/data/{PRODUCT}", headers=acting("v", "viewer"))
    assert r.status_code == 409 and r.json()["error"]["code"] == "preview_stale"
    p["editor"].post(f"{base(app)}/reset", json={})
    assert p["viewer"].get(f"{base(app)}/data/{PRODUCT}", headers=acting("v", "viewer")).status_code == 200
    assert p["viewer"].get(f"{base(app)}/status").json()["stale"] is False


def test_application_data_never_enters_the_platform_database(app, ctx):
    _, a, p = ctx
    p["editor"].post(f"{base(a)}/reset", json={"seed": {"rows_per_entity": 3}})
    platform_tables = set(inspect(app.state.session_factory.kw["bind"]).get_table_names())
    preview_tables = set(inspect(app.state.preview_store.engine).get_table_names())
    generated = re.compile(r"^app_[0-9a-f]{12}__")
    assert not any(generated.match(t) for t in platform_tables)
    assert any(generated.match(t) and "Product" in t for t in preview_tables)


def test_application_plane_audit_is_separate(ctx):
    proj, app, p = ctx
    p["editor"].post(f"{base(app)}/reset", json={})
    p["editor"].post(f"{base(app)}/data/{PRODUCT}", json=NEW, headers=acting("a", "admin"))
    events = p["owner"].get(f"/organizations/{proj['organization_id']}/audit-events?action_prefix=record.").json()
    assert events["total"] == 1
    e = events["items"][0]
    assert (e["plane"], e["action"], e["metadata"]["app_user"]) == ("application", "record.created", "a")
    assert "Widget" not in str(e)


def test_prod_requires_a_separate_preview_database(db_url):
    from orchivio.api import create_app
    from orchivio.api.settings import Settings

    with pytest.raises(RuntimeError):
        create_app(Settings(env="prod", database_url=db_url, preview_database_url=db_url))
