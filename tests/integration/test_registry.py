"""Module and template registry: signed semver packages, install as proposal, upgrades, yanking, import, templates."""

import copy
import hashlib
import io
import zipfile

import pytest
from pydantic import SecretStr
from test_tenancy import add_member, make_org

from orchivio.delivery import generate_key
from orchivio.model import AppModel
from orchivio.registry import (
    RegistryError,
    Version,
    VersionError,
    build_package,
    resolve,
    satisfies,
    slice_module,
    verify_package,
)

SEED, PUB = generate_key()


# --- semver ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("text,ok", [("1.2.3", True), ("0.0.0", True), ("1.2.3-beta.1", True), ("1.2", False), ("01.2.3", False), ("1.2.3-01", False),
                                     ("v1.2.3", False), ("1.2.3+build", False), ("", False), ("1.2.3.4", False)])
def test_version_parsing(text, ok):
    if ok:
        assert str(Version.parse(text)) == text
    else:
        with pytest.raises(VersionError):
            Version.parse(text)


def test_version_ordering_follows_semver():
    order = ["1.0.0-alpha", "1.0.0-alpha.1", "1.0.0-alpha.beta", "1.0.0-beta", "1.0.0-beta.2", "1.0.0-beta.11", "1.0.0-rc.1", "1.0.0", "1.0.1", "1.1.0", "2.0.0"]
    parsed = [Version.parse(v) for v in order]
    assert parsed == sorted(parsed)
    assert Version.parse("1.10.0") > Version.parse("1.9.0")


@pytest.mark.parametrize("spec,version,ok", [
    ("^1.2.3", "1.9.0", True), ("^1.2.3", "2.0.0", False), ("^1.2.3", "1.2.2", False), ("^0.2.3", "0.2.9", True), ("^0.2.3", "0.3.0", False), ("^0.0.3", "0.0.4", False),
    ("~1.2.3", "1.2.9", True), ("~1.2.3", "1.3.0", False), (">=1.2.3", "5.0.0", True), (">=1.2.3", "1.2.2", False), ("1.2.3", "1.2.3", True), ("1.2.3", "1.2.4", False),
    ("*", "3.0.0", True), ("latest", "0.0.1", True), ("^1.0.0", "1.5.0-beta.1", False), ("*", "1.0.0-rc.1", False), ("1.0.0-rc.1", "1.0.0-rc.1", True),
])
def test_ranges(spec, version, ok):
    assert satisfies(Version.parse(version), spec) is ok


def test_resolve_picks_the_highest_release():
    available = ["1.0.0", "1.2.0", "1.10.0", "2.0.0", "2.1.0-beta.1", "garbage"]
    assert resolve("^1.0.0", available) == "1.10.0"
    assert resolve("latest", available) == "2.0.0"
    assert resolve("2.1.0-beta.1", available) == "2.1.0-beta.1"
    assert resolve("^3.0.0", available) is None
    with pytest.raises(VersionError):
        resolve("^banana", available)


# --- package format --------------------------------------------------------------------------------------


def test_slicing_and_packaging_are_deterministic_and_verifiable(raw_model):
    model = AppModel.model_validate(raw_model)
    sliced = slice_module(model, "core", name="inventory-core")
    assert [m.key for m in sliced.modules] == ["core"] and sliced.security.access and set(sliced.security.roles) <= set(model.security.roles)
    kw = dict(name="inventory-core", version="1.0.0", kind="module", description="d", license="MIT", runtime_version="0.1.0", signing_seed=SEED)
    a, b = build_package(sliced, **kw), build_package(sliced, **kw)
    assert a.data == b.data and a.digest == b.digest  # reproducible
    v = verify_package(a.data, [PUB])
    assert v.manifest["contents"]["entities"] == ["core.Product"] and v.digest == a.digest


def test_a_module_that_needs_another_module_cannot_be_cut_out(raw_model):
    m = copy.deepcopy(raw_model)
    m["modules"].append({"key": "extra", "entities": [{"name": "Link", "fields": [
        {"name": "id", "type": "uuid", "primary_key": True, "generated": True}, {"name": "product", "type": "ref:core.Product"}]}]})
    model = AppModel.model_validate(m)
    with pytest.raises(RegistryError) as e:
        slice_module(model, "extra", name="extra-mod")
    assert e.value.code == "not_self_contained" and e.value.details


def test_tampering_is_detected(raw_model):
    sliced = slice_module(AppModel.model_validate(raw_model), "core", name="inventory-core")
    pkg = build_package(sliced, name="inventory-core", version="1.0.0", kind="module", description="", license="", runtime_version="0.1.0", signing_seed=SEED)

    def rebuild(change):
        src = zipfile.ZipFile(io.BytesIO(pkg.data))
        files = {n: src.read(n) for n in src.namelist()}
        change(files)
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as z:
            for n, b in files.items():
                z.writestr(n, b)
        return out.getvalue()

    def flip_model(files):
        files["model.zip"] = files["model.zip"][:-1] + bytes([files["model.zip"][-1] ^ 1])

    def new_version(files):
        files["REGISTRY.json"] = files["REGISTRY.json"].replace(b'"1.0.0"', b'"9.9.9"')

    for change, code in [(flip_model, "bad_signature"), (new_version, "bad_signature")]:
        with pytest.raises(RegistryError) as e:
            verify_package(rebuild(change), [PUB])
        assert e.value.code == code
    with pytest.raises(RegistryError) as e:
        verify_package(pkg.data, [generate_key()[1]])
    assert e.value.code == "untrusted_key"
    with pytest.raises(RegistryError):
        verify_package(b"not a zip", [PUB])
    with pytest.raises(RegistryError) as e:
        verify_package(pkg.data, [])
    assert e.value.code == "no_trusted_key"


# --- the HTTP API ------------------------------------------------------------------------------------------


BILLING = {
    "schema_version": "1.1", "application": {"key": "billing-app", "name": "Billing"},
    "modules": [{"key": "billing", "entities": [{"name": "Invoice", "fields": [
        {"name": "id", "type": "uuid", "primary_key": True, "generated": True}, {"name": "number", "type": "string", "required": True, "max_length": 40}]}]}],
    "security": {"roles": ["viewer"], "access": [{"entity": "billing.Invoice", "role": "viewer", "operations": ["read"]}], "pages": {}, "anonymous": False},
}


@pytest.fixture
def world(new_api, raw_model, app):
    app.state.settings.package_signing_key = SecretStr(SEED)
    owner = new_api("owner@example.com")
    org = make_org(owner, "acme", "Acme")
    proj = owner.post(f"/organizations/{org['id']}/projects", json={"name": "Inv", "slug": "inventory"}).json()
    people = {"owner": owner}
    for role in ("admin", "editor", "viewer"):
        people[role] = new_api(f"{role}@example.com")
        add_member(owner, org, f"{role}@example.com", role)
    src = owner.post(f"/projects/{proj['id']}/applications", json={"key": "inventory", "name": "Inventory", "model": raw_model}).json()
    dst = owner.post(f"/projects/{proj['id']}/applications", json={"key": "billing", "name": "Billing", "model": BILLING}).json()
    return org, proj, src, dst, people


def publish(c, app, **kw):
    body = {"name": "inventory-core", "version": "1.0.0", "kind": "module", "module": "core", "description": "Stock tracking", "visibility": "instance", **kw}
    return c.post(f"/applications/{app['id']}/registry/publish", json=body)


def install(c, app, **kw):
    return c.post(f"/applications/{app['id']}/registry/install", json={"package": "acme/inventory-core", "version": "latest", **kw})


def approve(c, proposal_id):
    return c.post(f"/proposals/{proposal_id}/approval", json={"decision": "approve", "acknowledge_risks": True})


def test_publishing_is_for_admins_and_versions_are_immutable(world):
    org, proj, src, dst, p = world
    assert publish(p["editor"], src).status_code == 403
    r = publish(p["admin"], src)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["package"] == "acme/inventory-core" and body["version"] == "1.0.0" and body["contents"]["entities"] == ["core.Product"] and body["yanked"] is False
    again = publish(p["admin"], src)
    assert again.status_code == 409 and again.json()["error"]["code"] == "version_exists"
    assert publish(p["admin"], src, version="1.0").json()["error"]["code"] in ("invalid_version", "validation_error") or publish(p["admin"], src, version="1.0").status_code == 422
    assert publish(p["admin"], src, module="nope", version="1.0.1").json()["error"]["code"] == "unknown_module"
    assert publish(p["admin"], src, module=None, version="1.0.2").json()["error"]["code"] == "module_required"
    assert publish(p["admin"], src, kind="template", version="1.0.3").json()["error"]["code"] == "kind_mismatch"  # a name keeps its kind


def test_publishing_needs_a_signing_key(world, app):
    org, proj, src, dst, p = world
    app.state.settings.package_signing_key = None
    assert publish(p["admin"], src).json()["error"]["code"] == "signing_not_configured"


def test_install_adds_the_module_as_a_proposal_with_its_roles(world):
    org, proj, src, dst, p = world
    publish(p["admin"], src)
    r = install(p["editor"], dst)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["module"] == "core" and body["upgrade"] is False and set(body["added_roles"]) >= {"editor", "admin"}
    prop = body["proposal"]
    assert prop["status"] == "pending" and prop["approval_level"] in ("A2", "A3") and "inventory-core@1.0.0" in prop["title"]
    assert p["editor"].get(f"/applications/{dst['id']}").json()["current_revision_no"] == 1  # nothing applied yet
    assert approve(p["admin"], prop["id"]).status_code == 200
    rev = p["owner"].get(f"/applications/{dst['id']}").json()
    assert rev["current_revision_no"] == 2
    model = p["owner"].get(f"/revisions/{rev['current_revision_id']}").json()["model_json"]
    assert {m["key"] for m in model["modules"]} == {"billing", "core"}
    assert {"editor", "admin"} <= set(model["security"]["roles"]) and any(r["entity"] == "core.Product" for r in model["security"]["access"])
    ids = [e["element_id"] for m in model["modules"] for e in m["entities"]]
    assert len(set(ids)) == len(ids) and all(ids)


def test_installs_report_state_and_upgrades_replace_only_unmodified_modules(world, raw_model):
    org, proj, src, dst, p = world
    publish(p["admin"], src)
    approve(p["admin"], install(p["editor"], dst).json()["proposal"]["id"])
    assert install(p["editor"], dst).json()["error"]["code"] == "already_installed"
    listed = p["editor"].get(f"/applications/{dst['id']}/registry/installs").json()["items"]
    assert len(listed) == 1 and {k: listed[0][k] for k in ("module", "package", "version", "state", "latest_version", "update_available")} == {
        "module": "core", "package": "acme/inventory-core", "version": "1.0.0", "state": "installed", "latest_version": "1.0.0", "update_available": False}
    assert listed[0]["requires"] == [] and listed[0]["required_by"] == [] and listed[0]["renamed_from"] is None
    # the publisher improves the module and releases 1.1.0
    newer = copy.deepcopy(raw_model)
    newer["modules"][0]["entities"][0]["display_name"] = "Stock item"
    p["owner"].post(f"/applications/{src['id']}/revisions", json={"model": newer})
    assert publish(p["admin"], src, version="1.1.0").status_code == 201
    assert p["editor"].get(f"/applications/{dst['id']}/registry/installs").json()["items"][0]["update_available"] is True
    up = install(p["editor"], dst, version="^1.0.0")
    assert up.status_code == 201 and up.json()["upgrade"] is True and "1.1.0" in up.json()["proposal"]["title"]
    pending = p["editor"].get(f"/applications/{dst['id']}/registry/installs").json()["items"][0]
    assert pending["state"] == "installed" and pending["version"] == "1.0.0"  # the open proposal is not applied yet
    approve(p["admin"], up.json()["proposal"]["id"])
    assert p["editor"].get(f"/applications/{dst['id']}/registry/installs").json()["items"][0]["version"] == "1.1.0"
    assert p["editor"].get(f"/applications/{dst['id']}/registry/installs").json()["items"][0]["state"] == "installed"
    # someone edits the installed module locally; the next upgrade refuses to overwrite that unless told to
    rev = p["owner"].get(f"/applications/{dst['id']}").json()["current_revision_id"]
    local = p["owner"].get(f"/revisions/{rev}").json()["model_json"]
    local["modules"][1]["entities"][0]["display_name"] = "Local rename"
    p["owner"].post(f"/applications/{dst['id']}/revisions", json={"model": local})
    assert p["editor"].get(f"/applications/{dst['id']}/registry/installs").json()["items"][0]["state"] == "modified"
    newest = copy.deepcopy(newer)
    newest["modules"][0]["entities"][0]["display_name"] = "Stock item v2"
    p["owner"].post(f"/applications/{src['id']}/revisions", json={"model": newest})
    publish(p["admin"], src, version="1.2.0")
    refused = install(p["editor"], dst)
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "module_modified"
    assert install(p["editor"], dst, overwrite_local_changes=True).status_code == 201


def test_a_module_with_the_same_key_from_elsewhere_is_not_overwritten(world, raw_model):
    org, proj, src, dst, p = world
    publish(p["admin"], src)
    other = p["owner"].post(f"/projects/{proj['id']}/applications", json={"key": "second", "name": "Second", "model": raw_model}).json()
    r = install(p["editor"], other)  # already has its own 'core'
    assert r.status_code == 409 and r.json()["error"]["code"] == "module_exists"


def test_visibility_and_membership(world, new_api):
    org, proj, src, dst, p = world
    publish(p["admin"], src, visibility="org", name="private-core")
    publish(p["admin"], src, visibility="instance", name="public-core")
    stranger = new_api("stranger@example.com")
    make_org(stranger, "other-org", "Other")
    names = lambda c: sorted(i["name"] for i in c.get("/registry/packages").json()["items"])  # noqa: E731
    assert names(p["viewer"]) == ["private-core", "public-core"]
    assert names(stranger) == ["public-core"]
    assert stranger.get("/registry/packages/acme/private-core").status_code == 404
    assert stranger.get("/registry/packages/acme/public-core").json()["latest"] == "1.0.0"
    assert stranger.get("/registry/packages/acme/private-core/1.0.0/download").status_code == 404
    assert stranger.get("/registry/packages/acme/public-core/1.0.0/download").status_code == 200


def test_search_filters_and_lists_the_newest_version(world, raw_model):
    org, proj, src, dst, p = world
    publish(p["admin"], src)
    publish(p["admin"], src, version="1.10.0")
    publish(p["admin"], src, version="1.9.0")
    publish(p["admin"], src, version="2.0.0-beta.1")
    items = p["viewer"].get("/registry/packages", params={"q": "stock"}).json()["items"]
    assert len(items) == 1 and items[0]["version"] == "1.10.0" and items[0]["versions"] == 4
    assert p["viewer"].get("/registry/packages", params={"q": "nothing-like-this"}).json()["items"] == []
    assert p["viewer"].get("/registry/packages", params={"kind": "template"}).json()["items"] == []
    versions = [v["version"] for v in p["viewer"].get("/registry/packages/acme/inventory-core").json()["versions"]]
    assert versions == ["2.0.0-beta.1", "1.10.0", "1.9.0", "1.0.0"]


def test_yanked_versions_cannot_be_chosen_but_installed_copies_stay(world):
    org, proj, src, dst, p = world
    publish(p["admin"], src)
    assert p["editor"].post("/registry/packages/acme/inventory-core/1.0.0/yank", json={"reason": "bug"}).status_code == 403
    y = p["admin"].post("/registry/packages/acme/inventory-core/1.0.0/yank", json={"reason": "security bug"})
    assert y.status_code == 200 and y.json()["yanked"] is True and y.json()["yanked_reason"] == "security bug"
    for spec in ("latest", "1.0.0", "^1.0.0"):
        r = install(p["editor"], dst, version=spec)
        assert r.status_code == 404 and r.json()["error"]["code"] == "no_matching_version", spec
    assert p["viewer"].get("/registry/packages").json()["items"] == []
    assert p["admin"].post("/registry/packages/acme/inventory-core/1.0.0/unyank").json()["yanked"] is False
    assert install(p["editor"], dst).status_code == 201


def test_bad_references_and_ranges(world):
    org, proj, src, dst, p = world
    publish(p["admin"], src)
    assert install(p["editor"], dst, package="no-slash").json()["error"]["code"] == "invalid_package"
    assert p["editor"].post(f"/applications/{dst['id']}/registry/install", json={"package": "nobody/nothing"}).status_code == 404
    assert install(p["editor"], dst, version="^banana").json()["error"]["code"] == "invalid_version"
    assert install(p["editor"], dst, version="7.0.0").json()["error"]["code"] == "no_matching_version"
    assert p["viewer"].post(f"/applications/{dst['id']}/registry/install", json={"package": "acme/inventory-core"}).status_code == 403


def test_a_package_changed_in_storage_is_refused(world, app):
    org, proj, src, dst, p = world
    entry = publish(p["admin"], src).json()
    key = f"registry/{entry['digest']}.zip"
    data = bytearray(app.state.artifacts.get(key))
    data[200] ^= 0xFF
    app.state.artifacts.put(key, bytes(data))
    r = install(p["editor"], dst)
    assert r.status_code == 409 and r.json()["error"]["code"] in ("package_corrupt", "bad_signature", "package_invalid")


def test_a_package_needing_a_newer_runtime_is_refused(world, monkeypatch):
    from orchivio.api.routes import registry

    org, proj, src, dst, p = world
    publish(p["admin"], src)
    monkeypatch.setattr(registry, "__version__", "0.0.1")
    r = install(p["editor"], dst)
    assert r.status_code == 409 and r.json()["error"]["code"] == "runtime_too_old"


def test_export_import_between_registries(world, new_api):
    org, proj, src, dst, p = world
    publish(p["admin"], src)
    blob = p["viewer"].get("/registry/packages/acme/inventory-core/1.0.0/download").content
    other = new_api("o2@example.com")
    org2 = make_org(other, "partner", "Partner")
    url = f"/organizations/{org2['id']}/registry/import"
    ok = other.post(url, content=blob, headers={"Content-Type": "application/zip"})
    assert ok.status_code == 201, ok.text
    assert ok.json()["imported"] is True and ok.json()["package"] == "partner/inventory-core"
    assert other.post(url, content=blob, headers={"Content-Type": "application/zip"}).json()["error"]["code"] == "version_exists"
    assert p["editor"].post(f"/organizations/{org['id']}/registry/import", content=blob, headers={"Content-Type": "application/zip"}).status_code == 403
    tampered = bytearray(blob)
    tampered[300] ^= 0xFF
    r = other.post(f"/organizations/{org2['id']}/registry/import", content=bytes(tampered), headers={"Content-Type": "application/zip"})
    assert r.status_code == 422
    foreign = build_package(slice_module(AppModel.model_validate(BILLING), "billing", name="billing-mod"), name="billing-mod", version="1.0.0", kind="module",
                            description="", license="", runtime_version="0.1.0", signing_seed=generate_key()[0])
    r = other.post(f"/organizations/{org2['id']}/registry/import", content=foreign.data, headers={"Content-Type": "application/zip"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "untrusted_key"
    assert hashlib.sha256(blob).hexdigest() == ok.json()["digest"]


def test_templates_create_applications(world):
    org, proj, src, dst, p = world
    r = publish(p["admin"], src, kind="template", module=None, name="inventory-starter", description="A starter")
    assert r.status_code == 201 and r.json()["kind"] == "template"
    assert install(p["editor"], dst, package="acme/inventory-starter").json()["error"]["code"] == "wrong_kind"
    made = p["editor"].post(f"/projects/{proj['id']}/registry/instantiate", json={"package": "acme/inventory-starter", "key": "fresh", "name": "Fresh stock"})
    assert made.status_code == 201, made.text
    app_id = made.json()["application_id"]
    got = p["owner"].get(f"/applications/{app_id}").json()
    assert got["key"] == "fresh" and got["current_revision_no"] == 1
    model = p["owner"].get(f"/revisions/{got['current_revision_id']}").json()["model_json"]
    assert model["application"]["key"] == "fresh" and model["application"]["name"] == "Fresh stock"
    dup = p["editor"].post(f"/projects/{proj['id']}/registry/instantiate", json={"package": "acme/inventory-starter", "key": "fresh", "name": "x"})
    assert dup.status_code == 409
    assert p["viewer"].post(f"/projects/{proj['id']}/registry/instantiate", json={"package": "acme/inventory-starter", "key": "v", "name": "x"}).status_code == 403
    assert p["editor"].post(f"/projects/{proj['id']}/registry/instantiate", json={"package": "acme/inventory-core", "key": "m", "name": "x"}).status_code == 404


def test_policy_packs_apply_to_installed_modules(world):
    org, proj, src, dst, p = world
    publish(p["admin"], src)
    pack = {"name": "no-export", "rules": [{"rule": "limit-flow-run-roles", "params": {"roles": ["admin"]}}]}
    assert p["owner"].post(f"/organizations/{org['id']}/policy-packs", json=pack).status_code == 201
    r = install(p["editor"], dst)
    assert r.status_code == 422 and any(i["code"].startswith("policy:") for i in r.json()["error"]["details"])
