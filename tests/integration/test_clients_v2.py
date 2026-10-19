"""Generated clients beyond TypeScript: Python and C#, and flow runs in all of them."""

import ast
import copy
import io
import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from test_tenancy import add_member, make_org

from orchivio.cli import main
from orchivio.codegen import build_client, build_csharp_client, build_python_client
from orchivio.codegen.csharp import cs_type, pascal
from orchivio.codegen.python import py_type, snake
from orchivio.model import AppModel, assign_element_ids

ROOT = Path(__file__).resolve().parents[2]
DOTNET = shutil.which("dotnet")


@pytest.fixture
def model(raw_model):
    return assign_element_ids(AppModel.model_validate(raw_model))


def load_client(model):
    ns: dict = {"__name__": "generated_client"}
    exec(compile(build_python_client(model), "generated_client.py", "exec"), ns)  # noqa: S102 - our own generator's output, checked below
    return ns


# --- naming and type tables ----------------------------------------------------------------------------------


@pytest.mark.parametrize("schema,expected", [
    ({"type": "string"}, "str"), ({"type": "integer"}, "int"), ({"type": "number"}, "float"), ({"type": "boolean"}, "bool"), ({"type": ["string", "null"]}, "Optional[str]"),
    ({"type": ["number", "string"]}, "Union[float, str]"), ({"type": "string", "enum": ["a", "b"]}, "Literal['a', 'b']"), ({"type": ["string", "null"], "enum": ["a", None]}, "Optional[Literal['a']]"),
    ({"$ref": "#/components/schemas/Product"}, "Product"), ({}, "Any"), ({"type": "array", "items": {"type": "integer"}}, "List[int]"), ({"type": "object"}, "Dict[str, Any]"),
])
def test_python_type_table(schema, expected):
    assert py_type(schema) == expected


@pytest.mark.parametrize("name,expected", [("ReorderLowStock", "reorder_low_stock"), ("Product", "product"), ("HTTPServer", "http_server"), ("order_line", "order_line"), ("Class", "class_"), ("A1B", "a1_b")])
def test_python_names(name, expected):
    assert snake(name) == expected


@pytest.mark.parametrize("schema,nullable,expected", [
    ({"type": "string"}, False, "string"), ({"type": "string"}, True, "string?"), ({"type": "integer"}, True, "long?"), ({"type": "number"}, False, "double"), ({"type": "boolean"}, False, "bool"),
    ({"type": ["string", "null"]}, False, "string?"), ({"type": ["number", "string"]}, False, "System.Text.Json.JsonElement"), ({"type": "string", "enum": ["a"]}, False, "string"),
    ({"type": "array", "items": {"type": "string"}}, False, "List<string>"), ({"type": "object"}, False, "Dictionary<string, System.Text.Json.JsonElement>"),
    ({"$ref": "#/components/schemas/Product"}, False, "Product"), ({}, True, "System.Text.Json.JsonElement?"),
])
def test_csharp_type_table(schema, nullable, expected):
    assert cs_type(schema, nullable=nullable) == expected


@pytest.mark.parametrize("name,expected", [("reorder_level", "ReorderLevel"), ("id", "Id"), ("a-b c", "ABC"), ("1st", "_1st"), ("", "Value")])
def test_csharp_names(name, expected):
    assert pascal(name) == expected


# --- the Python client -------------------------------------------------------------------------------------------


def test_the_python_client_is_valid_for_old_pythons_and_deterministic(model):
    text = build_python_client(model)
    ast.parse(text, feature_version=(3, 8))  # no syntax newer than 3.8
    assert text == build_python_client(model) and "model_hash" not in text and model.application.key in text
    ns = load_client(model)
    assert {"OrchivioApiError", "OrchivioDataClient", "Product", "ProductCreate", "ProductUpdate", "ProductPage", "FlowRunResult", "ReorderLowStockArgs"} <= set(ns)
    assert "import" not in "\n".join(line for line in text.splitlines() if line.startswith("from ") and "typing" not in line)  # standard library only
    assert "def list_all(" in text and "def pages(" in text


def test_the_python_client_only_has_permitted_operations(raw_model):
    raw = copy.deepcopy(raw_model)
    raw["security"]["access"] = [r for r in raw["security"]["access"] if r["role"] == "viewer"]
    raw["security"]["pages"] = {"product-list": ["viewer"]}
    raw["modules"][0]["pages"] = raw["modules"][0]["pages"][:1]
    text = build_python_client(AppModel.model_validate(raw))
    assert "def list(" in text and "def get(" in text and "def list_all(" in text and "def pages(" in text
    for gone in ("def create(", "def update(", "def delete(", "ProductCreate", "ProductUpdate"):
        assert gone not in text


class FakeResponse:
    def __init__(self, status, body=b""):
        self.status, self._body = status, body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def fake_opener(handler):
    calls = []

    def opener(request, timeout=None):
        calls.append(request)
        out = handler(request)
        if isinstance(out, Exception):
            raise out
        return out

    return opener, calls


def test_python_client_builds_requests(model):
    ns = load_client(model)
    opener, calls = fake_opener(lambda r: FakeResponse(200, json.dumps({"items": [], "total": 0, "limit": 5, "offset": 0}).encode()))
    c = ns["OrchivioDataClient"]("https://x.example/api/", headers=lambda: {"X-Preview-User": "u1"}, opener=opener, timeout=7)
    c.product.list(limit=5, offset=10, order_by="name", desc=True, filter={"sku": "A 1", "quantity": 0, "name": None})
    req = calls[0]
    from urllib.parse import parse_qs, urlparse

    url = urlparse(req.full_url)

    assert url.path == "/api/data/core.Product" and req.get_method() == "GET" and req.data is None
    assert {k: v[0] for k, v in parse_qs(url.query).items()} == {"limit": "5", "offset": "10", "order_by": "name", "desc": "true", "f.sku": "A 1", "f.quantity": "0"}
    assert req.get_header("X-preview-user") == "u1" and req.get_header("Accept") == "application/json"


def test_python_client_bodies_ids_and_errors(model):
    ns = load_client(model)
    responses = iter([FakeResponse(201, b'{"id": "p1"}'), FakeResponse(200, b"{}"), FakeResponse(204), FakeResponse(200, b'{"flow": "x"}')])
    opener, calls = fake_opener(lambda r: next(responses))
    c = ns["OrchivioDataClient"]("https://x.example", opener=opener)
    assert c.product.create({"name": "Bolt", "sku": "B", "quantity": 3, "reorder_level": 1}) == {"id": "p1"}
    assert calls[0].get_header("Content-type") == "application/json" and json.loads(calls[0].data) == {"name": "Bolt", "sku": "B", "quantity": 3, "reorder_level": 1}
    c.product.update("a/b c?x", {"quantity": 2})
    assert calls[1].full_url == "https://x.example/data/core.Product/a%2Fb%20c%3Fx" and calls[1].get_method() == "PATCH"
    assert c.product.delete("p1") is None and calls[2].get_method() == "DELETE"
    c.flows.reorder_low_stock({"min_gap": 0})
    assert calls[3].full_url == "https://x.example/flows/core.ReorderLowStock/run" and json.loads(calls[3].data) == {"args": {"min_gap": 0}}


def test_python_client_list_all_and_pages_pagination(model):
    ns = load_client(model)
    multi = [
        {"items": [{"id": "p1"}, {"id": "p2"}], "total": 3, "limit": 2, "offset": 0},
        {"items": [{"id": "p3"}], "total": 3, "limit": 2, "offset": 2},
    ]
    seq = iter(multi)
    opener, _ = fake_opener(lambda r: FakeResponse(200, json.dumps(next(seq)).encode()))
    c = ns["OrchivioDataClient"]("https://x.example", opener=opener)
    all_items = c.product.list_all(limit=2)
    assert all_items == [{"id": "p1"}, {"id": "p2"}, {"id": "p3"}]

    seq = iter(multi)
    opener2, _ = fake_opener(lambda r: FakeResponse(200, json.dumps(next(seq)).encode()))
    c2 = ns["OrchivioDataClient"]("https://x.example", opener=opener2)
    pages = list(c2.product.pages(limit=2))
    assert len(pages) == 2 and pages[0]["items"] == [{"id": "p1"}, {"id": "p2"}] and pages[1]["items"] == [{"id": "p3"}]


def test_python_client_errors(model):
    ns = load_client(model)
    err = ns["OrchivioApiError"]

    def http_error(status, body):
        return urllib.error.HTTPError("https://x", status, "err", {}, io.BytesIO(body))

    cases = [(http_error(422, json.dumps({"error": {"code": "validation_failed", "message": "sku is required", "details": [{"field": "sku"}]}}).encode()), 422, "validation_failed", "sku is required"),
             (http_error(502, b"<html>bad gateway</html>"), 502, "http_error", "Request failed (502)"), (FakeResponse(500, b"not json"), 500, "http_error", "Request failed (500)"),
             (FakeResponse(403, json.dumps({"error": {"code": "forbidden", "message": "no"}}).encode()), 403, "forbidden", "no")]
    for outcome, status, code, message in cases:
        opener, _ = fake_opener(lambda r, o=outcome: o)
        with pytest.raises(err) as e:
            ns["OrchivioDataClient"]("https://x", opener=opener).product.get("1")
        assert (e.value.status, e.value.code, e.value.message) == (status, code, message)
    opener, _ = fake_opener(lambda r: http_error(422, json.dumps({"error": {"code": "x", "message": "y", "details": [1]}}).encode()))
    with pytest.raises(err) as e:
        ns["OrchivioDataClient"]("https://x", opener=opener).product.get("1")
    assert e.value.details == [1]


@pytest.fixture
def ctx(new_api, raw_model):
    owner = new_api("owner@example.com")
    org = make_org(owner)
    proj = owner.post(f"/organizations/{org['id']}/projects", json={"name": "Inv", "slug": "inventory"}).json()
    editor = new_api("editor@example.com")
    add_member(owner, org, "editor@example.com", "editor")
    outsider = new_api("outsider@example.com")
    app = owner.post(f"/projects/{proj['id']}/applications", json={"key": "inventory", "name": "Inv", "model": raw_model}).json()
    editor.post(f"/applications/{app['id']}/preview/reset", json={})
    return app, owner, editor, outsider


def through(api):
    """An opener that sends the generated client's requests to the real API (in-process) as ``api``."""
    def opener(request, timeout=None):
        from urllib.parse import urlsplit

        parts = urlsplit(request.full_url)
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        r = api.request(request.get_method(), path, content=request.data, headers={k: v for k, v in request.header_items()})
        return FakeResponse(r.status_code, r.content)

    return opener


def test_the_python_client_works_against_the_real_api(ctx, model):
    app, owner, editor, _ = ctx
    ns = load_client(model)
    c = ns["OrchivioDataClient"](f"https://testserver/api/v1/applications/{app['id']}/preview", headers=lambda: {"X-Preview-User": "u1", "X-Preview-Roles": "admin"}, opener=through(editor))
    made = c.product.create({"name": "Bolt", "sku": "B-1", "quantity": 1, "reorder_level": 5})
    assert made["name"] == "Bolt" and made["status"] == "low"
    page = c.product.list(order_by="name", filter={"sku": "B-1"})
    assert page["total"] == 1 and page["items"][0]["id"] == made["id"]
    all_items = c.product.list_all(filter={"sku": "B-1"})
    assert len(all_items) == 1 and all_items[0]["id"] == made["id"]
    pages = list(c.product.pages(filter={"sku": "B-1"}))
    assert len(pages) == 1 and pages[0]["total"] == 1
    assert c.product.get(made["id"])["sku"] == "B-1"
    assert c.product.update(made["id"], {"quantity": 9})["quantity"] == 9
    ran = c.flows.reorder_low_stock({"min_gap": 0})
    assert ran["flow"] == "core.ReorderLowStock" and ran["status"] in ("succeeded", "failed") and isinstance(ran["trace"], list)
    c.product.delete(made["id"])
    with pytest.raises(ns["OrchivioApiError"]) as e:
        c.product.get(made["id"])
    assert e.value.status == 404 and e.value.code == "not_found"
    with pytest.raises(ns["OrchivioApiError"]) as e:
        c.product.create({"name": "x", "sku": "dup", "quantity": -1, "reorder_level": 0})
    assert e.value.status == 422 and e.value.details


# --- flow runs in the TypeScript client ----------------------------------------------------------------------------


def test_the_typescript_client_has_typed_flow_runs(model):
    ts = build_client(model)
    assert "export interface FlowRunResult {" in ts and "export interface ReorderLowStockArgs {" in ts and "min_gap: number" in ts
    assert "readonly flows = {" in ts and "reorderLowStock: (args: ReorderLowStockArgs) => this.request<FlowRunResult>('POST', '/flows/core.ReorderLowStock/run', { body: { args } })" in ts
    assert "readonly status: 'succeeded' | 'failed'" in ts or "status: 'succeeded' | 'failed'" in ts


def test_flows_nobody_may_run_are_not_in_any_client(raw_model):
    raw = copy.deepcopy(raw_model)
    raw["modules"][0]["flows"][0].pop("run_roles")
    m = AppModel.model_validate(raw)
    for text in (build_client(m), build_python_client(m), build_csharp_client(m)):
        assert "ReorderLowStock" not in text and "FlowRunResult" not in text


# --- delivery ----------------------------------------------------------------------------------------------------


def test_cli_and_endpoint_serve_every_language(ctx, tmp_path):
    app, owner, editor, outsider = ctx
    rev = owner.get(f"/applications/{app['id']}").json()["current_revision_id"]
    for ext, marker, media in [("ts", "export class OrchivioDataClient", "text/typescript"), ("py", "class OrchivioDataClient", "text/x-python"), ("cs", "public sealed class OrchivioDataClient", "text/plain")]:
        r = editor.get(f"/revisions/{rev}/client.{ext}")
        assert r.status_code == 200 and marker in r.text and r.headers["content-type"].startswith(media) and r.headers["x-content-type-options"] == "nosniff"
        assert f"inventory-r1-client.{ext}" in r.headers["content-disposition"]
        assert outsider.get(f"/revisions/{rev}/client.{ext}").status_code == 404
    assert editor.get(f"/revisions/{rev}/client.rb").status_code == 404
    assert "namespace Acme.Stock" in editor.get(f"/revisions/{rev}/client.cs", params={"namespace": "Acme.Stock"}).text
    assert editor.get(f"/revisions/{rev}/client.cs", params={"namespace": "bad namespace;"}).status_code == 422
    for lang, ext, marker in [("typescript", "ts", "export class"), ("python", "py", "class OrchivioDataClient"), ("csharp", "cs", "namespace Acme")]:
        out = tmp_path / f"c.{ext}"
        args = ["client", str(ROOT / "examples" / "inventory-app" / "model.yaml"), "--lang", lang, "-o", str(out)] + (["--namespace", "Acme.X"] if lang == "csharp" else [])
        assert main(args, io.StringIO()) == 0 and marker in out.read_text(encoding="utf-8")


# --- the C# client is compiled and run -------------------------------------------------------------------------------

CSPROJ = """<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><TargetFramework>net8.0</TargetFramework><Nullable>enable</Nullable><ImplicitUsings>disable</ImplicitUsings>
<TreatWarningsAsErrors>true</TreatWarningsAsErrors><OutputType>Exe</OutputType><LangVersion>latest</LangVersion></PropertyGroup></Project>"""

PROGRAM = """using System;
using System.Collections.Generic;
using System.Linq;
using System.Net;
using System.Net.Http;
using System.Text;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;
using Acme.Stock;

class Fake : HttpMessageHandler
{
    public List<(HttpRequestMessage Request, string Body)> Seen = new();
    public Func<HttpRequestMessage, HttpResponseMessage> Answer = _ => new HttpResponseMessage(HttpStatusCode.OK);
    protected override async Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken ct)
    {
        Seen.Add((request, request.Content is null ? "" : await request.Content.ReadAsStringAsync(ct)));
        return Answer(request);
    }
}

static class P
{
    static void Check(bool ok, string what) { if (!ok) { Console.WriteLine("FAILED: " + what); Environment.Exit(1); } }
    static HttpResponseMessage Json(HttpStatusCode s, string body) => new(s) { Content = new StringContent(body, Encoding.UTF8, "application/json") };

    static async Task<int> Main()
    {
        var fake = new Fake();
        var client = new OrchivioDataClient(new HttpClient(fake), "https://x.example/api/", ct => Task.FromResult<IDictionary<string, string>>(new Dictionary<string, string> { ["X-Preview-User"] = "u1" }));

        fake.Answer = _ => Json(HttpStatusCode.OK, "{\\"items\\":[{\\"id\\":\\"p1\\",\\"name\\":\\"Bolt\\",\\"sku\\":\\"B\\",\\"quantity\\":3,\\"reorder_level\\":1,\\"status\\":\\"low\\",\\"owner\\":\\"u\\",\\"created_at\\":\\"2026-01-01T00:00:00Z\\"}],\\"total\\":1,\\"limit\\":5,\\"offset\\":0}");
        var page = await client.Product.ListAsync(new ProductListParams { Limit = 5, Offset = 10, OrderBy = "name", Desc = true, SkuEquals = "A 1", QuantityEquals = 0 });
        Check(page.Total == 1 && page.Items[0].Name == "Bolt" && page.Items[0].ReorderLevel == 1 && page.Items[0].Status == ProductStatusValues.Low, "list deserialises");
        var url = fake.Seen[0].Request.RequestUri!;
        Check(url.AbsolutePath == "/api/data/core.Product", "path " + url.AbsolutePath);
        var q = System.Web.HttpUtility.ParseQueryString(url.Query);
        Check(q["limit"] == "5" && q["offset"] == "10" && q["order_by"] == "name" && q["desc"] == "true" && q["f.sku"] == "A 1" && q["f.quantity"] == "0", "query " + url.Query);
        Check(fake.Seen[0].Request.Headers.GetValues("X-Preview-User").Single() == "u1", "headers");

        fake.Answer = _ => Json(HttpStatusCode.Created, "{\\"id\\":\\"p2\\",\\"name\\":\\"Nut\\",\\"sku\\":\\"N\\",\\"quantity\\":1,\\"reorder_level\\":0,\\"status\\":\\"in_stock\\",\\"owner\\":\\"u\\",\\"created_at\\":\\"x\\"}");
        var made = await client.Product.CreateAsync(new ProductCreate { Name = "Nut", Sku = "N", Quantity = 1, ReorderLevel = 0 });
        Check(made.Id == "p2", "create result");
        using var sent = JsonDocument.Parse(fake.Seen[1].Body);
        Check(sent.RootElement.GetProperty("name").GetString() == "Nut" && sent.RootElement.GetProperty("reorder_level").GetInt64() == 0 && !sent.RootElement.TryGetProperty("owner", out _), "create body omits nulls");

        await client.Product.UpdateAsync("a/b c", new ProductUpdate { Quantity = 2 });
        Check(fake.Seen[2].Request.Method.Method == "PATCH" && fake.Seen[2].Request.RequestUri!.AbsolutePath == "/api/data/core.Product/a%2Fb%20c", "patch path");
        await client.Product.UpdateAsync("p1", new Dictionary<string, object?> { ["owner"] = null });
        Check(fake.Seen[3].Body == "{\\"owner\\":null}", "explicit null " + fake.Seen[3].Body);

        fake.Answer = _ => new HttpResponseMessage(HttpStatusCode.NoContent);
        await client.Product.DeleteAsync("p1");
        Check(fake.Seen[4].Request.Method == HttpMethod.Delete, "delete");

        fake.Answer = _ => Json(HttpStatusCode.OK, "{\\"flow\\":\\"core.ReorderLowStock\\",\\"status\\":\\"succeeded\\",\\"error\\":null,\\"error_step\\":null,\\"steps_executed\\":3,\\"trace\\":[{\\"step\\":\\"s\\",\\"status\\":\\"ok\\"}]}");
        var ran = await client.Flows.ReorderLowStockAsync(new ReorderLowStockArgs { MinGap = 0 });
        Check(ran.Status == "succeeded" && ran.StepsExecuted == 3 && ran.Error is null && ran.Trace.Count == 1, "flow result");
        Check(fake.Seen[5].Body == "{\\"args\\":{\\"min_gap\\":0}}" && fake.Seen[5].Request.RequestUri!.AbsolutePath == "/api/flows/core.ReorderLowStock/run", "flow body " + fake.Seen[5].Body);

        fake.Answer = _ => Json(HttpStatusCode.OK, "{\\"items\\":[{\\"id\\":\\"p1\\",\\"name\\":\\"Bolt\\",\\"sku\\":\\"B\\",\\"quantity\\":3,\\"reorder_level\\":1,\\"status\\":\\"low\\",\\"owner\\":\\"u\\",\\"created_at\\":\\"2026-01-01T00:00:00Z\\"}],\\"total\\":1,\\"limit\\":100,\\"offset\\":0}");
        var allPs = await client.Product.ListAllAsync(new ProductListParams { OrderBy = "name" });
        Check(allPs.Count == 1 && allPs[0].Name == "Bolt", "listAll single page");

        fake.Answer = _ => Json((HttpStatusCode)422, "{\\"error\\":{\\"code\\":\\"validation_failed\\",\\"message\\":\\"sku is required\\",\\"details\\":[{\\"field\\":\\"sku\\"}]}}");
        try { await client.Product.CreateAsync(new ProductCreate { Name = "x", Sku = "", Quantity = 0, ReorderLevel = 0 }); Check(false, "should throw"); }
        catch (OrchivioApiException e) { Check(e.Status == 422 && e.Code == "validation_failed" && e.Message.Contains("sku is required") && e.Details!.Value.GetArrayLength() == 1, "typed error"); }
        fake.Answer = _ => new HttpResponseMessage(HttpStatusCode.BadGateway) { Content = new StringContent("<html>bad gateway</html>") };
        try { await client.Product.GetAsync("1"); Check(false, "should throw"); }
        catch (OrchivioApiException e) { Check(e.Status == 502 && e.Code == "http_error", "non-JSON error"); }

        Console.WriteLine("OK");
        return 0;
    }
}
"""


def dotnet(args, cwd, timeout=240):
    env = {**os.environ, "DOTNET_NOLOGO": "1", "DOTNET_CLI_TELEMETRY_OPTOUT": "1", "DOTNET_SKIP_FIRST_TIME_EXPERIENCE": "1", "NUGET_XMLDOC_MODE": "skip"}
    env["DOTNET_CLI_UI_LANGUAGE"] = "en"
    return subprocess.run([DOTNET, *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, env=env)


@pytest.mark.skipif(DOTNET is None, reason="the .NET SDK is not installed")
def test_the_csharp_client_compiles_warning_free_and_behaves(model, tmp_path):
    (tmp_path / "t.csproj").write_text(CSPROJ, encoding="utf-8")
    (tmp_path / "Client.cs").write_text(build_csharp_client(model, namespace="Acme.Stock"), encoding="utf-8")
    (tmp_path / "Program.cs").write_text(PROGRAM, encoding="utf-8")
    build = dotnet(["build", "-nologo", "-v", "q", "-c", "Release"], tmp_path)
    assert build.returncode == 0, build.stdout + build.stderr
    run = dotnet(["run", "--no-build", "-c", "Release"], tmp_path)
    assert run.returncode == 0 and run.stdout.strip().endswith("OK"), run.stdout + run.stderr


@pytest.mark.skipif(DOTNET is None, reason="the .NET SDK is not installed")
def test_the_csharp_client_rejects_a_missing_required_property_at_compile_time(model, tmp_path):
    (tmp_path / "t.csproj").write_text(CSPROJ, encoding="utf-8")
    (tmp_path / "Client.cs").write_text(build_csharp_client(model, namespace="Acme.Stock"), encoding="utf-8")
    (tmp_path / "Program.cs").write_text("using Acme.Stock;\nclass P { static void Main() { var x = new ProductCreate { Name = \"only a name\" }; } }\n", encoding="utf-8")
    build = dotnet(["build", "-nologo", "-v", "q"], tmp_path)
    assert build.returncode != 0 and "CS9035" in build.stdout + build.stderr  # required member 'Sku' must be set

