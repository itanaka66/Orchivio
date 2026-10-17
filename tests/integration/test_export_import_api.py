"""Archive export and import over HTTP."""

import copy
import io
import zipfile

import pytest
from test_tenancy import add_member, make_org

from orchivio.model import AppModel, build_archive, model_hash, read_archive


@pytest.fixture
def ctx(new_api, raw_model):
    owner = new_api("owner@example.com")
    org = make_org(owner)
    proj = owner.post(f"/organizations/{org['id']}/projects", json={"name": "Inv", "slug": "inventory"}).json()
    people = {"owner": owner}
    for role in ("admin", "editor", "viewer"):
        people[role] = new_api(f"{role}@example.com")
        add_member(owner, org, f"{role}@example.com", role)
    people["outsider"] = new_api("outsider@example.com")
    app = owner.post(f"/projects/{proj['id']}/applications", json={"key": "inventory", "name": "Inv", "model": raw_model}).json()
    other = owner.post(f"/projects/{proj['id']}/applications", json={"key": "empty", "name": "Empty"}).json()
    return proj, app, other, people


def upload(api, app_id, data, title=None):
    path = f"/applications/{app_id}/import" + (f"?title={title}" if title else "")
    return api.request("POST", path, content=data, headers={"Content-Type": "application/zip"})


def test_export_is_admin_only_reproducible_and_audited(ctx):
    proj, app, _, p = ctx
    rev = app["current_revision_id"]
    url = f"/revisions/{rev}/export"
    assert p["editor"].get(url).status_code == 403 and p["viewer"].get(url).status_code == 403
    assert p["outsider"].get(url).status_code == 404
    r = p["admin"].get(url)
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    assert 'filename="inventory-r1.orchivio.zip"' in r.headers["content-disposition"]
    assert p["owner"].get(url).content == r.content  # same revision, same bytes
    model = read_archive(r.content)
    stored = p["viewer"].get(f"/revisions/{rev}").json()["model_json"]
    assert model_hash(model) == model_hash(AppModel.model_validate(stored))
    events = p["admin"].get(f"/organizations/{proj['organization_id']}/audit-events?action_prefix=revision.exported").json()
    assert events["total"] == 2 and events["items"][0]["metadata"]["bytes"] == len(r.content)


def test_import_becomes_a_proposal_and_never_changes_the_model(ctx):
    _, app, other, p = ctx
    archive = p["admin"].get(f"/revisions/{app['current_revision_id']}/export").content
    assert upload(p["viewer"], other["id"], archive).status_code == 403
    r = upload(p["editor"], other["id"], archive, title="Copy of inventory")
    assert r.status_code == 201, r.text
    prop = r.json()
    assert prop["status"] == "pending" and prop["title"] == "Copy of inventory" and prop["base_revision_id"] is None
    assert p["viewer"].get(f"/applications/{other['id']}").json()["current_revision_no"] is None  # not applied yet
    assert p["editor"].post(f"/proposals/{prop['id']}/approval", json={"decision": "approve"}).status_code == 200
    assert p["viewer"].get(f"/applications/{other['id']}").json()["current_revision_no"] == 1


def test_importing_an_identical_model_is_a_no_op(ctx):
    _, app, _, p = ctx
    archive = p["admin"].get(f"/revisions/{app['current_revision_id']}/export").content
    r = upload(p["editor"], app["id"], archive)
    assert r.status_code == 409 and r.json()["error"]["code"] == "no_changes"


def test_a_modified_archive_becomes_a_reviewable_diff(ctx, raw_model):
    _, app, _, p = ctx
    edited = AppModel.model_validate(copy.deepcopy(raw_model))
    edited.modules[0].entities[0].display_name = "Item"
    r = upload(p["editor"], app["id"], build_archive(edited))
    assert r.status_code == 201
    assert [(c["element_type"], c["kind"]) for c in r.json()["diff"]["changes"]] == [("entity", "modified")]


def test_bad_uploads_are_rejected_with_a_reason(ctx):
    _, app, other, p = ctx
    r = upload(p["editor"], other["id"], b"not a zip")
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_archive"
    good = p["admin"].get(f"/revisions/{app['current_revision_id']}/export").content
    tampered = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(good)) as zin, zipfile.ZipFile(tampered, "w") as zout:
        for i in zin.infolist():
            data = zin.read(i)
            zout.writestr(i.filename, data.replace(b"Product", b"Produkt", 1) if i.filename.endswith("Product.yaml") else data)
    r = upload(p["editor"], other["id"], tampered.getvalue())
    assert r.status_code == 422 and "does not match the manifest" in r.json()["error"]["message"]
    assert r.json()["error"]["details"]["path"].endswith("Product.yaml")
    assert upload(p["outsider"], other["id"], good).status_code == 404


def test_size_limits(ctx, app):
    _, _, other, p = ctx
    app.state.settings.max_import_bytes = 1000
    assert upload(p["editor"], other["id"], b"x" * 5000).status_code == 413
    app.state.settings.max_body_bytes = 100  # ordinary endpoints keep their own, smaller limit
    assert p["editor"].post("/auth/login", json={"email": "a@b.co", "password": "x" * 500}).status_code == 413
