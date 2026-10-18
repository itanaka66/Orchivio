"""The generated TypeScript client: what it contains, that it follows the permissions, that it compiles, and that it matches the API."""

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest
from test_tenancy import add_member, make_org

from orchivio.cli import main
from orchivio.codegen import build_client
from orchivio.codegen.typescript import ts_type
from orchivio.model import AppModel, assign_element_ids

ROOT = Path(__file__).resolve().parents[2]
TSC = ROOT / "apps" / "web" / "node_modules" / ".bin" / ("tsc.cmd" if sys.platform == "win32" else "tsc")


@pytest.fixture
def model(raw_model):
    return assign_element_ids(AppModel.model_validate(raw_model))


@pytest.mark.parametrize("schema,expected", [
    ({"type": "string"}, "string"), ({"type": "string", "format": "uuid"}, "string"), ({"type": "integer", "format": "int64"}, "number"),
    ({"type": "boolean"}, "boolean"), ({"type": ["string", "null"]}, "string | null"), ({"type": ["number", "string"]}, "number | string"),
    ({"type": "string", "enum": ["a", "b"]}, "'a' | 'b'"), ({"type": "string", "enum": ["it's", "x\\y"]}, "'it\\'s' | 'x\\\\y'"),
    ({"type": ["string", "null"], "enum": ["a", None]}, "'a' | null"), ({"$ref": "#/components/schemas/Product"}, "Product"), ({}, "unknown"),
    ({"description": "Any JSON value."}, "unknown"), ({"type": "array", "items": {"type": "string"}}, "string[]"),
    ({"type": "array", "items": {"type": ["string", "null"]}}, "(string | null)[]"), ({"type": "object"}, "Record<string, unknown>"),
    ({"type": "object", "properties": {"a": {"type": "integer"}, "b-c": {"type": "string"}}, "required": ["a"]}, "{ readonly a: number; 'b-c'?: string }".replace("readonly ", "")),
])
def test_schema_to_type_table(schema, expected):
    assert ts_type(schema) == expected


def test_the_client_has_the_types_and_operations_of_the_model(model):
    ts = build_client(model)
    assert f"({model.application.key}" not in ts and model.application.key in ts
    assert "export interface Product {" in ts and "readonly id: string" in ts and "readonly status: 'in_stock' | 'low' | 'out_of_stock'" in ts
    assert "export interface ProductCreate {\n" in ts and "export interface ProductUpdate {" in ts
    for op in ("list:", "create:", "get:", "update:", "delete:"):
        assert op in ts
    assert "orderBy?: 'id' | 'name'" in ts and "filter?: {" in ts and "sku?: string" in ts
    assert "OrchivioPage<Product>" in ts and "class OrchivioApiError" in ts and "readonly product = {" in ts


def test_output_is_deterministic_and_carries_the_model_hash(model):
    assert build_client(model) == build_client(model)
    assert model_hash_of(model) in build_client(model)
    changed = copy.deepcopy(model)
    changed.modules[0].entities[0].fields[1].max_length = 7
    assert build_client(changed) != build_client(model)


def model_hash_of(model):
    from orchivio.model import model_hash

    return model_hash(model)


def test_only_permitted_operations_are_generated(raw_model):
    raw = copy.deepcopy(raw_model)
    raw["security"]["access"] = [r for r in raw["security"]["access"] if r["role"] == "viewer"]
    raw["security"]["pages"] = {"product-list": ["viewer"]}
    raw["modules"][0]["pages"] = raw["modules"][0]["pages"][:1]
    ts = build_client(AppModel.model_validate(raw))
    assert "list:" in ts and "get:" in ts
    for gone in ("create:", "update:", "delete:", "ProductCreate", "ProductUpdate"):
        assert gone not in ts


def test_an_entity_named_like_a_helper_does_not_clash(raw_model):
    raw = copy.deepcopy(raw_model)
    raw["modules"][0]["entities"].append({"name": "Page", "fields": [{"name": "id", "type": "uuid", "primary_key": True, "generated": True}, {"name": "title", "type": "string"}]})
    raw["security"]["access"].append({"entity": "core.Page", "role": "admin", "operations": ["read", "create"]})
    ts = build_client(AppModel.model_validate(raw))
    assert "export interface Page {" in ts and "export interface OrchivioPage<T>" in ts and "readonly page = {" in ts
    assert ts.count("export interface Page {") == 1


def test_cli_writes_the_client(tmp_path):
    out = tmp_path / "client.ts"
    assert main(["client", str(ROOT / "examples" / "inventory-app" / "model.yaml"), "-o", str(out)]) == 0
    assert "export class OrchivioDataClient" in out.read_text(encoding="utf-8")


@pytest.mark.skipif(not TSC.exists(), reason="node_modules is not installed (run npm install in apps/web)")
def test_the_client_type_checks_and_rejects_wrong_use(model, tmp_path):
    (tmp_path / "client.ts").write_text(build_client(model), encoding="utf-8")
    (tmp_path / "use.ts").write_text(
        "import { OrchivioDataClient, OrchivioApiError } from './client'\n"
        "const c = new OrchivioDataClient({ baseUrl: '/x' })\n"
        "async function ok() {\n"
        "  const page = await c.product.list({ limit: 5, orderBy: 'name', filter: { sku: 'A1' } })\n"
        "  const first: string = page.items[0]!.name\n"
        "  const made = await c.product.create({ name: 'n', sku: 's', quantity: 1, reorder_level: 0 })\n"
        "  await c.product.update(made.id, { quantity: 2 })\n"
        "  await c.product.delete(made.id)\n"
        "  try { await c.product.get('x') } catch (e) { if (e instanceof OrchivioApiError) console.log(e.status, e.code, first) }\n"
        "}\n"
        "// @ts-expect-error quantity is a number\n"
        "c.product.create({ name: 'n', sku: 's', quantity: 'many', reorder_level: 0 })\n"
        "// @ts-expect-error a required field is missing\n"
        "c.product.create({ name: 'n' })\n"
        "// @ts-expect-error unknown field\n"
        "c.product.update('1', { colour: 'red' })\n"
        "// @ts-expect-error status is computed and read-only\n"
        "c.product.create({ name: 'n', sku: 's', quantity: 1, reorder_level: 0, status: 'low' })\n"
        "// @ts-expect-error not an allowed sort field\n"
        "c.product.list({ orderBy: 'colour' })\n"
        "export { ok }\n", encoding="utf-8")
    (tmp_path / "tsconfig.json").write_text(json.dumps({"compilerOptions": {"target": "ES2022", "lib": ["ES2023", "DOM"], "module": "ESNext", "moduleResolution": "bundler",
                                                          "strict": True, "noEmit": True, "skipLibCheck": True, "noUncheckedIndexedAccess": True, "types": []}, "include": ["*.ts"]}), encoding="utf-8")
    p = subprocess.run([str(TSC), "-p", str(tmp_path)], capture_output=True, text=True, cwd=tmp_path, shell=sys.platform == "win32")
    assert p.returncode == 0, p.stdout + p.stderr


@pytest.fixture
def ctx(new_api, raw_model):
    owner = new_api("owner@example.com")
    org = make_org(owner)
    proj = owner.post(f"/organizations/{org['id']}/projects", json={"name": "Inv", "slug": "inventory"}).json()
    editor = new_api("editor@example.com")
    add_member(owner, org, "editor@example.com", "editor")
    outsider = new_api("outsider@example.com")
    app = owner.post(f"/projects/{proj['id']}/applications", json={"key": "inventory", "name": "Inv", "model": raw_model}).json()
    return app, owner, editor, outsider


def test_the_revision_endpoint_serves_the_client(ctx, model):
    app, owner, editor, outsider = ctx
    rev = owner.get(f"/applications/{app['id']}").json()["current_revision_id"]
    r = editor.get(f"/revisions/{rev}/client.ts")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/typescript") and r.headers["x-content-type-options"] == "nosniff"
    assert "inventory-r1-client.ts" in r.headers["content-disposition"] and "export class OrchivioDataClient" in r.text
    assert outsider.get(f"/revisions/{rev}/client.ts").status_code == 404


def test_what_the_client_calls_is_what_the_server_serves(ctx):
    """Replay the client's requests (path, query names, body shape) against the real preview API."""
    app, owner, editor, _ = ctx
    base = f"/applications/{app['id']}/preview"
    editor.post(f"{base}/reset", json={})
    h = {"X-Preview-User": "u1", "X-Preview-Roles": "admin"}
    rev = owner.get(f"/applications/{app['id']}").json()["current_revision_id"]
    ts = build_client(AppModel.model_validate(owner.get(f"/revisions/{rev}").json()["model_json"]))
    for call in ("'/data/core.Product'", "'/data/core.Product/{id}'"):
        assert call in ts
    created = editor.post(f"{base}/data/core.Product", headers=h, json={"name": "Bolt", "sku": "B-1", "quantity": 3, "reorder_level": 1})
    assert created.status_code == 201, created.text
    pid = created.json()["id"]
    listed = editor.get(f"{base}/data/core.Product", headers=h, params={"limit": 5, "offset": 0, "order_by": "name", "desc": "true", "f.sku": "B-1"})
    assert listed.status_code == 200 and listed.json()["total"] == 1
    assert editor.get(f"{base}/data/core.Product/{pid}", headers=h).json()["name"] == "Bolt"
    assert editor.patch(f"{base}/data/core.Product/{pid}", headers=h, json={"quantity": 9}).json()["quantity"] == 9
    assert editor.delete(f"{base}/data/core.Product/{pid}", headers=h).status_code == 204

