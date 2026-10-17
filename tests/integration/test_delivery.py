"""Environments, constants, signed packages and gated promotion over HTTP."""

import copy
import io
import zipfile

import pytest
from pydantic import SecretStr
from sqlalchemy import select
from test_tenancy import add_member, make_org

from orchivio.db.models import AuditEvent
from orchivio.delivery import generate_key

SEED, PUB = generate_key()


def product(model):
    return next(e for m in model["modules"] for e in m["entities"] if e["name"] == "Product")


@pytest.fixture
def world(new_api, raw_model, app):
    app.state.settings.package_signing_key = SecretStr(SEED)
    owner = new_api("owner@example.com")
    org = make_org(owner)
    proj = owner.post(f"/organizations/{org['id']}/projects", json={"name": "Inv", "slug": "inventory"}).json()
    people = {"owner": owner}
    for name, role in (("admin2", "admin"), ("editor", "editor"), ("viewer", "viewer")):
        people[name] = new_api(f"{name}@example.com")
        add_member(owner, org, f"{name}@example.com", role)
    people["outsider"] = new_api("outsider@example.com")
    with_note = copy.deepcopy(raw_model)
    product(with_note)["fields"].append({"name": "note", "type": "text"})
    a = owner.post(f"/projects/{proj['id']}/applications", json={"key": "inventory", "name": "Inv", "model": with_note}).json()
    return a, people, raw_model


def make_envs(owner, a, prod_approval=True):
    out = {}
    for name, order, appr in (("dev", 0, False), ("test", 1, False), ("prod", 2, prod_approval)):
        r = owner.post(f"/applications/{a['id']}/environments", json={"name": name, "stage_order": order, "requires_approval": appr})
        assert r.status_code == 201, r.text
        out[name] = r.json()
    return out


def build(api, revision_id, **body):
    return api.post(f"/revisions/{revision_id}/packages", json=body)


def passing_tests(api, revision_id):
    r = api.post(f"/revisions/{revision_id}/test-runs", json={})
    assert r.status_code == 201 and r.json()["status"] == "succeeded", r.text


def promote(api, pkg, env, **body):
    return api.post(f"/packages/{pkg['id']}/promote", json={"environment": env, **body})


# --- packages ----------------------------------------------------------------------------------


def test_signing_must_be_configured(world, app):
    a, p, _ = world
    app.state.settings.package_signing_key = None
    r = build(p["editor"], a["current_revision_id"])
    assert r.status_code == 503 and r.json()["error"]["code"] == "signing_not_configured"


def test_build_is_idempotent_verifiable_and_permissioned(world, app):
    a, p, _ = world
    rid = a["current_revision_id"]
    first = build(p["editor"], rid)
    assert first.status_code == 201
    pkg = first.json()
    again = build(p["editor"], rid)
    assert again.status_code == 200 and again.json()["id"] == pkg["id"] and again.json()["digest"] == pkg["digest"]
    assert p["viewer"].post(f"/revisions/{rid}/packages", json={}).status_code == 403
    assert p["outsider"].post(f"/revisions/{rid}/packages", json={}).status_code == 404

    assert p["viewer"].get(f"/packages/{pkg['id']}/verify").json()["valid"] is True
    detail = p["viewer"].get(f"/packages/{pkg['id']}").json()
    assert detail["migration_plan"]["steps"] and all(s["phase"] == "expand" for s in detail["migration_plan"]["steps"])
    assert "components" in detail["sbom"]
    assert p["viewer"].get(f"/applications/{a['id']}/packages").json()["total"] == 1
    assert p["outsider"].get(f"/packages/{pkg['id']}").status_code == 404

    assert p["editor"].get(f"/packages/{pkg['id']}/download").status_code == 403  # contains the whole model
    got = p["owner"].get(f"/packages/{pkg['id']}/download")
    assert got.status_code == 200 and got.headers["content-type"] == "application/zip"
    assert sorted(zipfile.ZipFile(io.BytesIO(got.content)).namelist())[0] == "PACKAGE.json"


def test_tampering_with_the_stored_file_is_caught(world, app):
    a, p, _ = world
    pkg = build(p["editor"], a["current_revision_id"]).json()
    path = app.state.artifacts._path(f"packages/{pkg['digest']}.zip")
    data = bytearray(path.read_bytes())
    data[len(data) // 2] ^= 0xFF
    path.write_bytes(bytes(data))
    for url in (f"/packages/{pkg['id']}/verify", f"/packages/{pkg['id']}"):
        r = p["viewer"].get(url)
        assert r.status_code == 409 and r.json()["error"]["code"] == "package_corrupt"
    envs = make_envs(p["owner"], a)
    assert promote(p["editor"], pkg, "dev").json()["error"]["code"] == "package_corrupt"
    path.unlink()
    assert p["viewer"].get(f"/packages/{pkg['id']}/verify").json()["error"]["code"] == "package_corrupt"
    assert envs


def test_a_package_signed_by_an_unknown_key_is_refused(world, app):
    a, p, _ = world
    pkg = build(p["editor"], a["current_revision_id"]).json()
    app.state.settings.package_signing_key = SecretStr(generate_key()[0])  # the key was rotated away
    assert p["viewer"].get(f"/packages/{pkg['id']}/verify").json()["error"]["code"] == "package_corrupt"
    app.state.settings.package_trusted_public_keys = [PUB]  # trusting the old public key again
    assert p["viewer"].get(f"/packages/{pkg['id']}/verify").json()["valid"] is True


# --- environments and constants ----------------------------------------------------------------


def test_environment_management(world):
    a, p, _ = world
    o = p["owner"]
    envs = make_envs(o, a)
    assert [e["name"] for e in o.get(f"/applications/{a['id']}/environments").json()["items"]] == ["dev", "test", "prod"]
    assert o.post(f"/applications/{a['id']}/environments", json={"name": "dev", "stage_order": 9}).json()["error"]["code"] == "environment_exists"
    assert o.post(f"/applications/{a['id']}/environments", json={"name": "x", "stage_order": 0}).status_code == 409
    for bad in ({"name": "Prod", "stage_order": 5}, {"name": "x y", "stage_order": 5}, {"name": "x", "stage_order": -1}):
        assert o.post(f"/applications/{a['id']}/environments", json=bad).status_code == 422
    assert p["editor"].post(f"/applications/{a['id']}/environments", json={"name": "q", "stage_order": 7}).status_code == 403
    assert p["viewer"].get(f"/applications/{a['id']}/environments").status_code == 200
    assert p["outsider"].get(f"/applications/{a['id']}/environments").status_code == 404
    assert o.patch(f"/environments/{envs['dev']['id']}", json={"require_tests": False}).json()["require_tests"] is False
    assert o.delete(f"/environments/{envs['prod']['id']}").status_code == 204


def test_constants_hold_values_or_secret_references_never_secrets(world, app):
    a, p, _ = world
    env = make_envs(p["owner"], a)["dev"]
    base = f"/environments/{env['id']}/constants"
    o = p["owner"]
    assert o.put(f"{base}/API_URL", json={"value": "https://example.test"}).json()["value"] == "https://example.test"
    assert o.put(f"{base}/DB_PASSWORD", json={"secret_ref": "vault:kv/inventory/db"}).json()["secret_ref"] == "vault:kv/inventory/db"
    assert o.put(f"{base}/DB_PASSWORD", json={"value": "hunter2"}).json()["error"]["code"] == "secret_in_value"
    assert o.put(f"{base}/x", json={"value": "a", "secret_ref": "env:A"}).json()["error"]["code"] == "invalid_constant"
    assert o.put(f"{base}/x", json={}).json()["error"]["code"] == "invalid_constant"
    assert o.put(f"{base}/x", json={"secret_ref": "hunter2"}).json()["error"]["code"] == "invalid_secret_ref"
    assert o.put(f"{base}/1bad", json={"value": "a"}).json()["error"]["code"] == "invalid_key"
    assert o.put(f"{base}/API_URL", json={"value": "https://other.test"}).json()["value"] == "https://other.test"  # update in place
    assert [c["key"] for c in p["viewer"].get(base).json()["items"]] == ["API_URL", "DB_PASSWORD"]
    assert p["editor"].put(f"{base}/Z", json={"value": "1"}).status_code == 403
    assert o.delete(f"{base}/API_URL").status_code == 204 and o.delete(f"{base}/API_URL").status_code == 404
    with app.state.session_factory() as s:
        metas = [e.meta for e in s.scalars(select(AuditEvent).where(AuditEvent.action == "environment.constant_set"))]
    assert metas and all("hunter2" not in str(m) and "value" not in m for m in metas)  # values never reach the audit log


# --- promotion ---------------------------------------------------------------------------------


def test_the_full_pipeline_dev_test_prod_with_gates(world):
    a, p, _ = world
    o, ed, adm2 = p["owner"], p["editor"], p["admin2"]
    envs = make_envs(o, a)
    rid = a["current_revision_id"]
    pkg = build(ed, rid).json()

    assert promote(ed, pkg, "dev").json()["error"]["code"] == "tests_required"
    passing_tests(ed, rid)
    assert promote(p["viewer"], pkg, "dev").status_code == 403
    assert promote(ed, pkg, "nope").status_code == 404
    assert promote(ed, pkg, "prod").json()["error"]["code"] == "stage_skipped"
    assert promote(ed, pkg, "test").json()["error"]["details"]["required_environment"] == "dev"

    dev = promote(ed, pkg, "dev")
    assert dev.status_code == 201 and dev.json()["status"] == "deployed"
    assert promote(ed, pkg, "dev").json()["error"]["code"] == "already_deployed"
    assert promote(ed, pkg, "test").json()["status"] == "deployed"

    pending = promote(o, pkg, "prod", comment="go live")
    assert pending.status_code == 201 and pending.json()["status"] == "pending_approval"
    assert promote(o, pkg, "prod").json()["error"]["code"] == "already_pending"
    dep_id = pending.json()["id"]
    assert o.post(f"/deployments/{dep_id}/approval", json={"decision": "approve"}).json()["error"]["code"] == "separation_of_duties"
    assert ed.post(f"/deployments/{dep_id}/approval", json={"decision": "approve"}).status_code == 403
    assert p["outsider"].post(f"/deployments/{dep_id}/approval", json={"decision": "approve"}).status_code == 404
    done = adm2.post(f"/deployments/{dep_id}/approval", json={"decision": "approve", "comment": "ok"})
    assert done.status_code == 200 and done.json()["status"] == "deployed" and done.json()["decided_by"]
    assert adm2.post(f"/deployments/{dep_id}/approval", json={"decision": "approve"}).json()["error"]["code"] == "not_pending"

    listing = {e["name"]: e for e in o.get(f"/applications/{a['id']}/environments").json()["items"]}
    assert all(listing[n]["current_package"]["id"] == pkg["id"] for n in ("dev", "test", "prod"))
    assert o.get(f"/applications/{a['id']}/deployments?environment=prod&status=deployed").json()["total"] == 1
    assert envs


def test_rejection_leaves_the_environment_untouched(world):
    a, p, _ = world
    make_envs(p["owner"], a)
    rid = a["current_revision_id"]
    pkg = build(p["editor"], rid).json()
    passing_tests(p["editor"], rid)
    promote(p["editor"], pkg, "dev"), promote(p["editor"], pkg, "test")
    dep = promote(p["owner"], pkg, "prod").json()
    r = p["admin2"].post(f"/deployments/{dep['id']}/approval", json={"decision": "reject", "comment": "not now"}).json()
    assert r["status"] == "rejected"
    prod = next(e for e in p["owner"].get(f"/applications/{a['id']}/environments").json()["items"] if e["name"] == "prod")
    assert prod["current"] is None
    assert promote(p["owner"], pkg, "prod").status_code == 201  # may be requested again


def test_destructive_migrations_need_acknowledgement_and_the_right_base(world):
    a, p, raw = world
    o, ed = p["owner"], p["editor"]
    make_envs(o, a, prod_approval=False)
    rev1 = a["current_revision_id"]
    pkg1 = build(ed, rev1).json()
    passing_tests(ed, rev1)
    for env in ("dev", "test", "prod"):
        assert promote(ed, pkg1, env).json()["status"] == "deployed"

    # revision 2 drops the "note" field: a destructive contract step
    r = ed.post(f"/applications/{a['id']}/revisions", json={"model": raw, "acknowledge_risks": True})
    assert r.status_code == 201, r.text
    rev2 = r.json()["id"]
    passing_tests(ed, rev2)

    naive = build(ed, rev2).json()  # built without telling it what is deployed
    err = promote(ed, naive, "dev").json()["error"]
    assert err["code"] == "migration_base_mismatch" and err["details"]["deployed_package_id"] == pkg1["id"]

    pkg2 = build(ed, rev2, previous_package_id=pkg1["id"]).json()
    assert pkg2["id"] != naive["id"] and pkg2["previous_model_hash"] == pkg1["model_hash"]
    plan = ed.get(f"/packages/{pkg2['id']}").json()["migration_plan"]
    assert [(s["op"], s["target"], s["destructive"]) for s in plan["steps"]] == [("drop_column", "core.Product.note", True)]

    blocked = promote(ed, pkg2, "dev")
    assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "approval_required"
    assert blocked.json()["error"]["details"]["destructive_steps"][0]["target"] == "core.Product.note"
    ok = promote(ed, pkg2, "dev", acknowledge_risks=True)
    assert ok.status_code == 201 and ok.json()["status"] == "deployed" and ok.json()["risks_acknowledged"] is True
    superseded = o.get(f"/applications/{a['id']}/deployments?status=superseded").json()
    assert superseded["total"] == 1 and superseded["items"][0]["package_id"] == pkg1["id"]


def test_approval_verifies_the_package_again(world, app):
    a, p, _ = world
    make_envs(p["owner"], a)
    rid = a["current_revision_id"]
    pkg = build(p["editor"], rid).json()
    passing_tests(p["editor"], rid)
    promote(p["editor"], pkg, "dev"), promote(p["editor"], pkg, "test")
    pending = promote(p["owner"], pkg, "prod").json()
    path = app.state.artifacts._path(f"packages/{pkg['digest']}.zip")
    path.write_bytes(path.read_bytes() + b"x")  # changed between request and approval
    r = p["admin2"].post(f"/deployments/{pending['id']}/approval", json={"decision": "approve"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "package_corrupt"
    prod = next(e for e in p["owner"].get(f"/applications/{a['id']}/environments").json()["items"] if e["name"] == "prod")
    assert prod["current"] is None  # nothing went live
