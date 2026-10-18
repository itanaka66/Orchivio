"""Registry round two: dependencies, renamed modules, uninstall, and other servers' registries."""

import copy
import json
import os
import re

import pytest
from test_registry import BILLING, approve, install, publish, world  # noqa: F401 - fixtures and helpers reused
from test_tenancy import make_org

from orchivio.integrations.transport import Response
from orchivio.model import AppModel, assign_element_ids
from orchivio.registry import RegistryError, remove_module, rename_module, slice_module


def thing(key, requires_core=False):
    fields = [{"name": "id", "type": "uuid", "primary_key": True, "generated": True}, {"name": "label", "type": "string", "required": True, "max_length": 40}]
    if requires_core:
        fields.append({"name": "product", "type": "ref:core.Product"})
    return {"key": key, "entities": [{"name": "Thing", "fields": fields}]}


@pytest.fixture
def suite_world(world, raw_model):  # noqa: F811
    """The source application gets more modules: reports and extras use core; suite uses neither but requires both."""
    org, proj, src, dst, p = world
    m = copy.deepcopy(raw_model)
    m["modules"] += [thing("reports", True), thing("extras", True), thing("suite")]
    for key in ("reports", "extras", "suite"):
        m["security"]["access"].append({"entity": f"{key}.Thing", "role": "viewer", "operations": ["read"]})
    revise(p, src, m)
    return world


def model_of(p, app):
    rev = p["owner"].get(f"/applications/{app['id']}").json()["current_revision_id"]
    return copy.deepcopy(p["owner"].get(f"/revisions/{rev}").json()["model_json"])


def revise(p, app, model):
    r = p["owner"].post(f"/applications/{app['id']}/revisions", json={"model": model, "acknowledge_risks": True})
    assert r.status_code == 201, r.text


def pub(p, src, name, module, version="1.0.0", requires=None, **kw):
    return publish(p["admin"], src, name=name, module=module, version=version, **({"requires": requires} if requires else {}), **kw)


def installed_modules(p, app):
    rev = p["owner"].get(f"/applications/{app['id']}").json()["current_revision_id"]
    return [m["key"] for m in p["owner"].get(f"/revisions/{rev}").json()["model_json"]["modules"]]


# --- dependencies --------------------------------------------------------------------------------------------------


def test_publishing_checks_what_a_module_requires(suite_world):
    org, proj, src, dst, p = suite_world
    assert pub(p, src, "reports", "reports").json()["error"]["code"] == "not_self_contained"  # uses core, which is not declared
    assert pub(p, src, "inventory-core", "core").status_code == 201
    ok = pub(p, src, "reports", "reports", requires=[{"package": "acme/inventory-core", "range": "^1.0.0"}])
    assert ok.status_code == 201, ok.text
    assert ok.json()["contents"]["modules"] == ["reports"]  # the dependency is not copied into the package
    detail = p["viewer"].get("/registry/packages/acme/reports").json()
    assert detail["versions"][0]["version"] == "1.0.0"
    for bad, code in [([{"package": "acme/nope", "range": "^1.0.0"}], "not_found"), ([{"package": "acme/inventory-core", "range": "^9.0.0"}], "no_matching_version"),
                      ([{"package": "acme/inventory-core", "range": "^banana"}], "invalid_version"),
                      ([{"package": "acme/inventory-core", "range": "^1.0.0"}] * 2, "invalid_requires"),
                      ([{"package": "acme/reports", "range": "^1.0.0"}], "invalid_requires")]:
        r = pub(p, src, "reports", "reports", version="1.0.1", requires=bad)
        assert r.status_code in (404, 422) and r.json()["error"]["code"] in (code, "validation_error"), (bad, r.text)
    assert pub(p, src, "inventory-starter", None, version="1.0.0", kind="template", requires=[{"package": "acme/inventory-core"}]).json()["error"]["code"] == "requires_not_supported"
    assert pub(p, src, "solo", "extras", version="1.0.0", requires=[{"package": "acme/inventory-core", "range": "^1.0.0"}]).status_code == 201


def test_installing_a_module_installs_what_it_requires_in_one_proposal(suite_world):
    org, proj, src, dst, p = suite_world
    pub(p, src, "inventory-core", "core")
    pub(p, src, "reports", "reports", requires=[{"package": "acme/inventory-core", "range": "^1.0.0"}])
    r = install(p["editor"], dst, package="acme/reports")
    assert r.status_code == 201, r.text
    body = r.json()
    assert [(s["package"], s["action"], s["dependency"]) for s in body["steps"]] == [("acme/inventory-core", "install", True), ("acme/reports", "install", False)]
    assert body["module"] == "reports" and "(+1 required)" in body["proposal"]["title"]
    assert installed_modules(p, dst) == ["billing"]  # a proposal, nothing applied
    assert approve(p["admin"], body["proposal"]["id"]).status_code == 200
    assert installed_modules(p, dst) == ["billing", "core", "reports"]
    listed = {i["module"]: i for i in p["editor"].get(f"/applications/{dst['id']}/registry/installs").json()["items"]}
    assert listed["core"]["dependency_of"] == "acme/reports" and listed["core"]["required_by"] == ["reports"]
    assert listed["reports"]["requires"][0]["package"] == "acme/inventory-core" and listed["reports"]["state"] == "installed"
    again = install(p["editor"], dst, package="acme/reports")
    assert again.status_code == 409 and again.json()["error"]["code"] == "already_installed"


def test_a_requirement_already_met_is_kept_and_an_unmet_one_is_upgraded(suite_world, raw_model):
    org, proj, src, dst, p = suite_world
    pub(p, src, "inventory-core", "core")
    approve(p["admin"], install(p["editor"], dst, package="acme/inventory-core").json()["proposal"]["id"])
    pub(p, src, "reports", "reports", requires=[{"package": "acme/inventory-core", "range": "^1.0.0"}])
    r = install(p["editor"], dst, package="acme/reports").json()
    assert [(s["package"], s["action"]) for s in r["steps"]] == [("acme/inventory-core", "kept"), ("acme/reports", "install")]
    approve(p["admin"], r["proposal"]["id"])
    # core 2.0.0 and a reports 2.0.0 that needs it
    changed = model_of(p, src)
    changed["modules"][0]["entities"][0]["display_name"] = "Stock item"
    revise(p, src, changed)
    pub(p, src, "inventory-core", "core", version="2.0.0")
    pub(p, src, "reports", "reports", version="2.0.0", requires=[{"package": "acme/inventory-core", "range": "^2.0.0"}])
    up = install(p["editor"], dst, package="acme/reports", version="^2.0.0")
    assert up.status_code == 201, up.text
    assert [(s["package"], s["version"], s["action"]) for s in up.json()["steps"]] == [("acme/inventory-core", "2.0.0", "upgrade"), ("acme/reports", "2.0.0", "upgrade")]
    approve(p["admin"], up.json()["proposal"]["id"])
    assert {i["module"]: i["version"] for i in p["editor"].get(f"/applications/{dst['id']}/registry/installs").json()["items"]} == {"core": "2.0.0", "reports": "2.0.0"}


def test_conflicting_requirements_and_foreign_modules_are_reported(suite_world, raw_model):
    org, proj, src, dst, p = suite_world
    pub(p, src, "inventory-core", "core")
    pub(p, src, "inventory-core", "core", version="2.0.0")
    pub(p, src, "reports", "reports", requires=[{"package": "acme/inventory-core", "range": "^1.0.0"}])
    pub(p, src, "extras", "extras", requires=[{"package": "acme/inventory-core", "range": "^2.0.0"}])
    pub(p, src, "suite", "suite", requires=[{"package": "acme/reports", "range": "^1.0.0"}, {"package": "acme/extras", "range": "^1.0.0"}])
    r = install(p["editor"], dst, package="acme/suite")
    assert r.status_code == 409 and r.json()["error"]["code"] == "dependency_conflict"
    assert r.json()["error"]["details"]["package"] == "acme/inventory-core"
    assert installed_modules(p, dst) == ["billing"]
    # an application that already has its own 'core' module
    other = p["owner"].post(f"/projects/{proj['id']}/applications", json={"key": "own-core", "name": "Own core",
                            "model": raw_model}).json()
    r = install(p["editor"], other, package="acme/reports", version="1.0.0")
    assert r.status_code == 409 and r.json()["error"]["code"] == "dependency_conflict"


def test_a_package_cannot_be_installed_over_its_own_dependents_removal(suite_world):
    org, proj, src, dst, p = suite_world
    pub(p, src, "inventory-core", "core")
    pub(p, src, "reports", "reports", requires=[{"package": "acme/inventory-core", "range": "^1.0.0"}])
    approve(p["admin"], install(p["editor"], dst, package="acme/reports").json()["proposal"]["id"])
    r = p["editor"].post(f"/applications/{dst['id']}/registry/uninstall", json={"module": "core"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "required_by" and r.json()["error"]["details"]["required_by"][0]["module"] == "reports"


# --- renaming ------------------------------------------------------------------------------------------------------


def test_rename_rewrites_qualified_references_and_only_those(raw_model):
    model = AppModel.model_validate(raw_model)
    pkg = slice_module(model, "core", name="inventory-core")
    renamed = rename_module(pkg, "stock")
    dumped = json.dumps(renamed.model_dump(mode="json", exclude_none=True))
    assert renamed.modules[0].key == "stock" and all(r.entity.startswith("stock.") for r in renamed.security.access)
    assert "core." not in dumped.replace("score.", "")  # nothing qualified with the old key is left
    assert rename_module(pkg, "core") is pkg
    for bad in ("Stock", "1stock", "st-ock", ""):
        with pytest.raises(RegistryError) as e:
            rename_module(pkg, bad)
        assert e.value.code == "invalid_module_key"
    # text that merely contains the old key is left alone: needs a real element name after the dot
    data = pkg.model_dump(mode="json", exclude_none=True)
    data["modules"][0]["entities"][0]["display_name"] = "a core.Thing and core.Product and score.Product and $core.Product and more.core.Product"
    odd = rename_module(AppModel.model_validate(data), "stock").modules[0].entities[0].display_name
    assert odd == "a core.Thing and stock.Product and score.Product and $core.Product and more.core.Product"


def test_a_renamed_module_behaves_like_the_original(raw_model, tmp_path):
    """Run the generated tests (CRUD, validation, access, flow tests) for the original and for the renamed module: same cases, same outcomes."""
    from orchivio.db import make_engine
    from orchivio.model import model_hash
    from orchivio.testgen import run_tests

    model = AppModel.model_validate(raw_model)
    original = slice_module(model, "core", name="inventory-core")
    renamed = rename_module(original, "stock")
    outcomes = []
    for m in (assign_element_ids(original), assign_element_ids(renamed)):
        engine = make_engine(f"sqlite:///{(tmp_path / f'{len(outcomes)}.db').as_posix()}")
        report = run_tests(m, model_hash(m), engine)
        outcomes.append({re.sub(r":[0-9a-f]{8}$", "", re.sub(r"\bstock\.", "core.", r.id)): r.status for r in report.results})
    assert outcomes[0] == outcomes[1] and len(outcomes[0]) > 10
    assert "passed" in set(outcomes[0].values())


def test_install_under_another_key_and_upgrades_keep_it(suite_world):
    org, proj, src, dst, p = suite_world
    pub(p, src, "inventory-core", "core")
    r = install(p["editor"], dst, package="acme/inventory-core", as_module="stock")
    assert r.status_code == 201, r.text
    assert r.json()["module"] == "stock" and r.json()["steps"][0]["renamed_from"] == "core"
    approve(p["admin"], r.json()["proposal"]["id"])
    assert installed_modules(p, dst) == ["billing", "stock"]
    rev = p["owner"].get(f"/applications/{dst['id']}").json()["current_revision_id"]
    model = p["owner"].get(f"/revisions/{rev}").json()["model_json"]
    assert any(a["entity"] == "stock.Product" for a in model["security"]["access"]) and "core.Product" not in json.dumps(model)
    item = p["editor"].get(f"/applications/{dst['id']}/registry/installs").json()["items"][0]
    assert item["module"] == "stock" and item["renamed_from"] == "core" and item["state"] == "installed"
    changed = model_of(p, src)
    changed["modules"][0]["entities"][0]["display_name"] = "Stock item"
    revise(p, src, changed)
    pub(p, src, "inventory-core", "core", version="1.1.0")
    wrong = install(p["editor"], dst, package="acme/inventory-core", as_module="other")
    assert wrong.status_code == 409 and wrong.json()["error"]["code"] == "module_renamed"
    up = install(p["editor"], dst, package="acme/inventory-core")  # no as_module: the upgrade finds its module
    assert up.status_code == 201 and up.json()["module"] == "stock" and up.json()["upgrade"] is True
    approve(p["admin"], up.json()["proposal"]["id"])
    assert installed_modules(p, dst) == ["billing", "stock"]
    assert p["editor"].get(f"/applications/{dst['id']}/registry/installs").json()["items"][0]["state"] == "installed"
    assert install(p["editor"], dst, package="acme/inventory-core", as_module="Bad-Key").status_code == 422


def test_a_second_copy_next_to_the_original_is_refused_by_validation(suite_world, raw_model):
    org, proj, src, dst, p = suite_world
    pub(p, src, "inventory-core", "core")
    other = p["owner"].post(f"/projects/{proj['id']}/applications", json={"key": "copy", "name": "Copy", "model": raw_model}).json()
    r = install(p["editor"], other, package="acme/inventory-core", as_module="stock")
    assert r.status_code == 422 and {i["code"] for i in r.json()["error"]["details"]} & {"duplicate-page", "duplicate-route"}  # page keys and routes must stay unique


# --- uninstall -----------------------------------------------------------------------------------------------------


def test_uninstall_proposes_removing_the_module_and_what_came_with_it(suite_world):
    org, proj, src, dst, p = suite_world
    pub(p, src, "inventory-core", "core")
    approve(p["admin"], install(p["editor"], dst, package="acme/inventory-core").json()["proposal"]["id"])
    assert p["viewer"].post(f"/applications/{dst['id']}/registry/uninstall", json={"module": "core"}).status_code == 403
    r = p["editor"].post(f"/applications/{dst['id']}/registry/uninstall", json={"module": "core"})
    assert r.status_code == 201, r.text
    assert r.json()["proposal"]["title"].startswith("Remove module core") and installed_modules(p, dst) == ["billing", "core"]  # a proposal only
    assert p["editor"].get(f"/applications/{dst['id']}/registry/installs").json()["items"][0]["state"] == "removal_pending"
    approve(p["admin"], r.json()["proposal"]["id"])
    assert installed_modules(p, dst) == ["billing"]
    rev = p["owner"].get(f"/applications/{dst['id']}").json()["current_revision_id"]
    model = p["owner"].get(f"/revisions/{rev}").json()["model_json"]
    assert all(not a["entity"].startswith("core.") for a in model["security"]["access"])
    assert {"editor", "admin"} <= set(model["security"]["roles"])  # roles stay: people may hold them
    assert p["editor"].get(f"/applications/{dst['id']}/registry/installs").json()["items"] == []
    again = install(p["editor"], dst, package="acme/inventory-core")  # and it can come back
    assert again.status_code == 201


def test_uninstall_refuses_what_it_should(suite_world):
    org, proj, src, dst, p = suite_world
    url = f"/applications/{dst['id']}/registry/uninstall"
    assert p["editor"].post(url, json={"module": "billing"}).json()["error"]["code"] == "not_installed"  # not from the registry
    assert p["editor"].post(url, json={"module": "Bad Key"}).status_code == 422
    pub(p, src, "inventory-core", "core")
    approve(p["admin"], install(p["editor"], dst, package="acme/inventory-core").json()["proposal"]["id"])
    local = model_of(p, dst)
    local["modules"][1]["entities"][0]["display_name"] = "Local change"
    revise(p, dst, local)
    r = p["editor"].post(url, json={"module": "core"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "module_modified"
    assert p["editor"].post(url, json={"module": "core", "overwrite_local_changes": True}).status_code == 201


def test_removal_that_would_break_something_else_is_refused(suite_world):
    org, proj, src, dst, p = suite_world
    pub(p, src, "inventory-core", "core")
    approve(p["admin"], install(p["editor"], dst, package="acme/inventory-core").json()["proposal"]["id"])
    model = model_of(p, dst)
    model["modules"][0]["entities"][0]["fields"].append({"name": "product", "type": "ref:core.Product"})  # billing now uses core
    model["security"]["access"].append({"entity": "billing.Invoice", "role": "editor", "operations": ["read"]})
    revise(p, dst, model)
    r = p["editor"].post(f"/applications/{dst['id']}/registry/uninstall", json={"module": "core", "overwrite_local_changes": True})
    assert r.status_code == 422 and r.json()["error"]["code"] == "model_invalid"


def test_remove_module_on_the_model(raw_model):
    model = AppModel.model_validate(raw_model)
    gone = remove_module(model, "core")
    assert gone.modules == [] and gone.security.access == [] and gone.security.pages == {} and gone.security.roles == model.security.roles
    with pytest.raises(RegistryError):
        remove_module(gone, "core")


# --- other servers' registries ---------------------------------------------------------------------------------------


class Remote:
    """Stands in for another server: the registry API of this very server, reached as a different user, through the pinned transport's interface."""

    def __init__(self, client, reachable=True):
        self.client, self.calls, self.reachable, self.status_override = client, [], reachable, None

    def request(self, method, url, headers, body=None, *, timeout=None, max_bytes=0):
        self.calls.append((method, url, dict(headers)))
        if not self.reachable:
            raise ConnectionError("down")
        path = url.split("https://registry.example.org", 1)[1].removeprefix("/api/v1")
        if self.status_override:
            return Response(self.status_override, b"{}", "application/json", {})
        kw = {"content": body, "headers": {"Content-Type": headers.get("Content-Type", "application/json")}} if body is not None else {}
        r = self.client.request(method, path, **kw)
        assert len(r.content) <= max_bytes or max_bytes == 0
        return Response(r.status_code, r.content, r.headers.get("content-type", ""), {})


@pytest.fixture
def federation(world, new_api, app, monkeypatch):  # noqa: F811
    org, proj, src, dst, p = world
    pub(p, src, "inventory-core", "core", visibility="org")
    partner = new_api("partner-admin@example.com")
    porg = make_org(partner, "partner", "Partner")
    remote = Remote(p["admin"])  # the "other server" sees us as acme's admin
    app.state.mcp_transport = remote
    monkeypatch.setenv("ORCHIVIO_TEST_REMOTE_TOKEN", "remote-secret-token")
    return org, proj, src, dst, p, partner, porg, remote


def put_remote(c, org, name="acme-server", **kw):
    return c.put(f"/organizations/{org['id']}/registry/remotes/{name}", json={"base_url": "https://registry.example.org", "token_ref": "env:ORCHIVIO_TEST_REMOTE_TOKEN", **kw})


def test_remote_configuration_is_validated_and_admin_only(federation):
    org, proj, src, dst, p, partner, porg, remote = federation
    assert put_remote(p["editor"], org).status_code == 403
    assert put_remote(partner, porg).status_code == 200
    got = partner.get(f"/organizations/{porg['id']}/registry/remotes").json()["items"]
    assert got == [{"name": "acme-server", "base_url": "https://registry.example.org", "token_ref": "env:ORCHIVIO_TEST_REMOTE_TOKEN", "remote_organization_id": None}]
    for body, code in [({"base_url": "http://registry.example.org"}, "destination_refused"), ({"base_url": "https://127.0.0.1"}, "destination_refused"),
                       ({"base_url": "https://registry.example.org/api/v1"}, "invalid_remote"), ({"base_url": "https://registry.example.org?x=1"}, "invalid_remote"),
                       ({"base_url": "https://user:pw@registry.example.org"}, "destination_refused"), ({"token_ref": "vault:x"}, "invalid_secret_ref"),
                       ({"token_ref": "env:DOES_NOT_EXIST_ANYWHERE"}, "secret_unavailable")]:
        r = partner.put(f"/organizations/{porg['id']}/registry/remotes/acme-server", json={"base_url": "https://registry.example.org", **body})
        assert r.status_code == 422 and r.json()["error"]["code"] == code, (body, r.text)
    assert partner.put(f"/organizations/{porg['id']}/registry/remotes/Bad_Name", json={"base_url": "https://registry.example.org"}).status_code == 422
    assert partner.delete(f"/organizations/{porg['id']}/registry/remotes/acme-server").status_code == 204
    assert partner.get(f"/organizations/{porg['id']}/registry/remotes").json()["items"] == []


def test_search_and_pull_from_a_remote_registry(federation):
    org, proj, src, dst, p, partner, porg, remote = federation
    put_remote(partner, porg)
    base = f"/organizations/{porg['id']}/registry/remotes/acme-server"
    found = partner.get(f"{base}/packages", params={"q": "stock"}).json()["items"]
    assert [(i["package"], i["version"]) for i in found] == [("acme/inventory-core", "1.0.0")] and "digest" not in found[0]
    assert remote.calls[0][2]["Authorization"] == "Bearer remote-secret-token"
    got = partner.post(f"{base}/pull", json={"package": "acme/inventory-core"})
    assert got.status_code == 201, got.text
    assert got.json()["package"] == "partner/inventory-core" and got.json()["imported"] is True and got.json()["version"] == "1.0.0"
    assert partner.post(f"{base}/pull", json={"package": "acme/inventory-core"}).json()["error"]["code"] == "version_exists"
    assert partner.post(f"{base}/pull", json={"package": "acme/inventory-core", "version": "^7.0.0"}).json()["error"]["code"] == "no_matching_version"
    assert partner.post(f"{base}/pull", json={"package": "acme/missing"}).status_code == 404
    assert partner.post(f"{base}/pull", json={"package": "nonsense"}).status_code == 422
    assert "remote-secret-token" not in json.dumps(partner.get(f"/organizations/{porg['id']}/registry/remotes").json())
    # and it is a normal package here now: installable by partner's own applications
    pa = partner.post(f"/organizations/{porg['id']}/projects", json={"name": "Partner project", "slug": "partner-project"}).json()
    app2 = partner.post(f"/projects/{pa['id']}/applications", json={"key": "billing", "name": "Billing", "model": BILLING}).json()
    r = partner.post(f"/applications/{app2['id']}/registry/install", json={"package": "partner/inventory-core"})
    assert r.status_code == 201, r.text


def test_a_remote_package_signed_by_an_unknown_key_is_not_stored(federation, monkeypatch):
    org, proj, src, dst, p, partner, porg, remote = federation
    from orchivio.delivery import generate_key

    put_remote(partner, porg)
    # this server trusts only some other key, so what the remote signed is foreign to it
    monkeypatch.setattr("orchivio.api.routes.registry._trusted", lambda request: [generate_key()[1]])
    r = partner.post(f"/organizations/{porg['id']}/registry/remotes/acme-server/pull", json={"package": "acme/inventory-core"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "untrusted_key"
    assert partner.get("/registry/packages").json()["items"] == []


def test_remote_failures_never_leak_the_remote_text(federation):
    org, proj, src, dst, p, partner, porg, remote = federation
    put_remote(partner, porg)
    base = f"/organizations/{porg['id']}/registry/remotes/acme-server"
    remote.reachable = False
    r = partner.get(f"{base}/packages")
    assert r.status_code == 502 and r.json()["error"]["code"] == "remote_unreachable"
    remote.reachable = True
    for status, code in [(401, "remote_refused"), (403, "remote_refused"), (500, "remote_error")]:
        remote.status_override = status
        assert partner.get(f"{base}/packages").json()["error"]["code"] == code
    remote.status_override = None
    partner.put(f"{base}", json={"base_url": "https://registry.example.org", "token_ref": "env:ORCHIVIO_TEST_REMOTE_TOKEN"})
    os.environ.pop("ORCHIVIO_TEST_REMOTE_TOKEN", None)
    assert partner.get(f"{base}/packages").json()["error"]["code"] == "token_unavailable"
    assert partner.get(f"/organizations/{porg['id']}/registry/remotes/nope/packages").status_code == 404


def test_push_to_a_remote_registry(federation, new_api):
    org, proj, src, dst, p, partner, porg, remote = federation
    # acme pushes its package to partner's registry; the "remote" is now partner (a different user)
    remote.client = partner
    base = f"/organizations/{org['id']}/registry/remotes/partner-server"
    assert p["admin"].put(base, json={"base_url": "https://registry.example.org"}).status_code == 200
    assert p["admin"].post(f"{base}/push", json={"package": "acme/inventory-core", "version": "1.0.0"}).json()["error"]["code"] == "remote_organization_required"
    assert p["admin"].put(base, json={"base_url": "https://registry.example.org", "remote_organization_id": porg["id"]}).status_code == 200
    assert p["editor"].post(f"{base}/push", json={"package": "acme/inventory-core", "version": "1.0.0"}).status_code == 403
    r = p["admin"].post(f"{base}/push", json={"package": "acme/inventory-core", "version": "1.0.0"})
    assert r.status_code == 200 and r.json()["pushed"] is True, r.text
    assert [i["package"] for i in partner.get("/registry/packages").json()["items"]] == ["partner/inventory-core"]
    assert p["admin"].post(f"{base}/push", json={"package": "acme/inventory-core", "version": "1.0.0"}).json()["error"]["code"] == "remote_has_it"
    assert p["admin"].post(f"{base}/push", json={"package": "partner/inventory-core", "version": "1.0.0"}).json()["error"]["code"] == "invalid_package"  # only your own
    assert p["admin"].post(f"{base}/push", json={"package": "acme/inventory-core", "version": "9.9.9"}).status_code == 404
    remote.status_override = 422
    assert p["admin"].post(f"{base}/push", json={"package": "acme/inventory-core", "version": "1.0.0"}).json()["error"]["code"] == "remote_rejected"

