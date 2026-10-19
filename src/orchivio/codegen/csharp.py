"""A typed C# client for the data API of an application (docs/CLIENTS.md).

Generated from the same OpenAPI document as the other clients: one file for .NET 6+ with nullable reference types, ``System.Text.Json`` and
``HttpClient`` only. Classes per entity with the model's field types (``long``, ``double``, ``bool``, ``string``; enumerations are strings with a
constants class), create/update inputs (``null`` properties are not sent; a dictionary overload sends explicit nulls), typed list parameters, one method per
permitted operation and per runnable flow, and a typed exception. Deterministic for a given model; the header carries the model hash.
"""

from __future__ import annotations

import re
from typing import Any

from orchivio.model import AppModel, model_hash

from .openapi import build_openapi
from .typescript import _referenced

_WORDS = re.compile(r"[A-Za-z0-9]+")


def _str(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def pascal(name: str) -> str:
    """``reorder_level`` -> ``ReorderLevel``; always a valid identifier."""
    out = "".join(w[:1].upper() + w[1:] for w in _WORDS.findall(name)) or "Value"
    return "_" + out if out[0].isdigit() else out


def cs_type(schema: dict[str, Any], *, nullable: bool = False) -> str:
    """The C# type of a JSON-Schema fragment as produced by ``build_openapi``. ``nullable`` adds ``?`` (reference types and value types alike)."""
    if "$ref" in schema:
        base = schema["$ref"].rsplit("/", 1)[-1]
    elif "enum" in schema:
        base, nullable = "string", nullable or None in schema["enum"]
    else:
        t = schema.get("type")
        if isinstance(t, list):
            kinds = [k for k in t if k != "null"]
            if "null" in t:
                nullable = True
            if len(kinds) == 1:
                return cs_type({**schema, "type": kinds[0]}, nullable=nullable)
            return "System.Text.Json.JsonElement" + ("?" if nullable else "")  # a number or a string: decimals
        base = {"string": "string", "integer": "long", "number": "double", "boolean": "bool", "null": "object"}.get(t or "", "")
        if t == "array":
            base = f"List<{cs_type(schema.get('items', {}))}>"
        elif t == "object":
            base = "Dictionary<string, System.Text.Json.JsonElement>"
        elif not base:
            base = "System.Text.Json.JsonElement"
    return base + ("?" if nullable else "")


def _doc(p: dict[str, Any]) -> str:
    bits = []
    if p.get("readOnly"):
        bits.append("read-only")
    if p.get("format") in ("uuid", "date", "date-time"):
        bits.append(str(p["format"]))
    if p.get("maxLength"):
        bits.append(f"max {p['maxLength']} characters")
    if "minimum" in p:
        bits.append(f"min {p['minimum']}")
    if "maximum" in p:
        bits.append(f"max {p['maximum']}")
    if p.get("x-orchivio-computed"):
        bits.append(f"computed ({p['x-orchivio-computed']})")
    if "enum" in p:
        bits.append("one of " + ", ".join(str(v) for v in p["enum"] if v is not None))
    text = "; ".join(bits).replace("&", "&amp;").replace("<", "&lt;")
    return f"        /// <summary>{text}</summary>\n" if text else ""


def _class(name: str, schema: dict[str, Any], *, output: bool, constants: list[str]) -> str:
    props = schema.get("properties", {})
    req = set(schema.get("required", []))
    lines = [f"    public sealed class {name}", "    {"]
    for key, p in props.items():
        prop = pascal(key)
        if prop == name:
            prop += "_"
        required = key in req
        t = cs_type(p, nullable=not required)
        init = ""
        if required and output and not t.endswith("?"):
            init = ' = "";' if t == "string" else " = new();" if t.startswith(("List<", "Dictionary<")) else ""
        keyword = "required " if required and not output and not init and not t.endswith("?") else ""
        lines.append(_doc(p).rstrip("\n"))
        lines.append(f'        [JsonPropertyName({_str(key)})]')
        lines.append(f"        public {keyword}{t} {prop} {{ get; set; }}{init}")
        if "enum" in p:
            vals = [v for v in p["enum"] if v is not None]
            consts = "\n".join(f"        public const string {pascal(str(v))} = {_str(str(v))};" for v in vals)
            constants.append(f"    /// <summary>Allowed values of {name}.{prop}.</summary>\n    public static class {name}{prop}Values\n    {{\n{consts}\n    }}\n")
    lines.append("    }")
    return "\n".join(x for x in lines if x != "") + "\n"


def build_csharp_client(model: AppModel, *, namespace: str = "Orchivio.Client") -> str:
    doc = build_openapi(model)
    schemas: dict[str, Any] = doc["components"]["schemas"]
    used = _referenced(doc["paths"], schemas)
    constants: list[str] = []
    types: list[str] = []
    for name, schema in schemas.items():
        props = schema.get("properties", {})
        if name not in used or name == "Error" or {"items", "total", "limit", "offset"} <= set(props):
            continue
        output = not name.endswith(("Create", "Update", "Args"))
        types.append(_class(name, schema, output=output, constants=constants))

    entities: dict[str, dict[str, Any]] = {}
    for path, ops in doc["paths"].items():
        if path.startswith("/data/"):
            for verb, op in ops.items():
                entities.setdefault(path.split("/")[2], {})[op["operationId"].split("_", 1)[0]] = (path, verb, op)

    api_classes, members, list_params = [], [], []
    for ops in entities.values():
        tag = next(iter(ops.values()))[2]["tags"][0]
        read = ops["read"][2]["responses"]["200"]["content"]["application/json"]["schema"] if "read" in ops else ops["list"][2]["responses"]["200"]["content"]["application/json"]["schema"]
        entity = cs_type(read).removesuffix("Page") if "read" not in ops else cs_type(read)
        cls = f"{entity}Api"
        lines = [f"    /// <summary>{tag}</summary>", f"    public sealed class {cls}", "    {", "        private readonly OrchivioDataClient _c;", f"        internal {cls}(OrchivioDataClient c) => _c = c;", ""]
        if "list" in ops:
            params = {p["name"]: p for p in ops["list"][2].get("parameters", []) if "name" in p}
            pname = f"{entity}ListParams"
            order = ", ".join(_str(v) for v in params["order_by"]["schema"]["enum"]) if "order_by" in params else ""
            filt = [(n[2:], cs_type(p["schema"], nullable=True)) for n, p in params.items() if n.startswith("f.")]
            body = "".join(f'        /// <summary>Equals filter on {k}.</summary>\n        public {t} {pascal(k)}Equals {{ get; set; }}\n' for k, t in filt)
            q = "".join(f'            if (p.{pascal(k)}Equals is not null) q["f.{k}"] = OrchivioDataClient.Str(p.{pascal(k)}Equals);\n' for k, _t in filt)
            list_params.append(f"    public sealed class {pname}\n    {{\n        public int? Limit {{ get; set; }}\n        public int? Offset {{ get; set; }}\n"
                               f"        /// <summary>A stored field: {order}.</summary>\n        public string? OrderBy {{ get; set; }}\n        public bool? Desc {{ get; set; }}\n{body}"
                               f"        internal Dictionary<string, string> ToQuery()\n        {{\n            var p = this;\n            var q = new Dictionary<string, string>();\n"
                               f'            if (p.Limit is not null) q["limit"] = p.Limit.Value.ToString(System.Globalization.CultureInfo.InvariantCulture);\n'
                               f'            if (p.Offset is not null) q["offset"] = p.Offset.Value.ToString(System.Globalization.CultureInfo.InvariantCulture);\n'
                               f'            if (!string.IsNullOrEmpty(p.OrderBy)) q["order_by"] = p.OrderBy!;\n            if (p.Desc == true) q["desc"] = "true";\n{q}            return q;\n        }}\n    }}\n')
            lines += [f"        public Task<OrchivioPage<{entity}>> ListAsync({pname}? p = null, CancellationToken ct = default) =>",
                      f'            _c.SendAsync<OrchivioPage<{entity}>>(HttpMethod.Get, {_str(ops["list"][0])}, (p ?? new {pname}()).ToQuery(), null, ct);', "",
                      f"        public async Task<List<{entity}>> ListAllAsync({pname}? p = null, CancellationToken ct = default)",
                      "        {",
                      f"            var src = p ?? new {pname}();",
                      '            var q = src.ToQuery();',
                      '            q["limit"] = (src.Limit ?? 100).ToString(System.Globalization.CultureInfo.InvariantCulture);',
                      f"            var all = new List<{entity}>(); long offset = 0;",
                      "            while (true)",
                      "            {",
                      '                q["offset"] = offset.ToString(System.Globalization.CultureInfo.InvariantCulture);',
                      f"                var page = await _c.SendAsync<OrchivioPage<{entity}>>(HttpMethod.Get, {_str(ops['list'][0])},",
                      "                    new Dictionary<string, string>(q), null, ct).ConfigureAwait(false);",
                      "                all.AddRange(page.Items);",
                      "                if (page.Items.Count == 0 || all.Count >= page.Total) break;",
                      "                offset += page.Items.Count;",
                      "            }",
                      "            return all;",
                      "        }", ""]
        if "create" in ops:
            body_t = cs_type(ops["create"][2]["requestBody"]["content"]["application/json"]["schema"])
            lines += [f"        public Task<{entity}> CreateAsync({body_t} body, CancellationToken ct = default) =>",
                      f'            _c.SendAsync<{entity}>(HttpMethod.Post, {_str(ops["create"][0])}, null, body, ct);', ""]
        if "read" in ops:
            lines += [f"        public Task<{entity}> GetAsync(string id, CancellationToken ct = default) =>",
                      f'            _c.SendAsync<{entity}>(HttpMethod.Get, {_str(ops["read"][0].replace("{id}", ""))} + Uri.EscapeDataString(id), null, null, ct);', ""]
        if "update" in ops:
            body_t = cs_type(ops["update"][2]["requestBody"]["content"]["application/json"]["schema"])
            path = _str(ops["update"][0].replace("{id}", ""))
            lines += [f"        public Task<{entity}> UpdateAsync(string id, {body_t} patch, CancellationToken ct = default) =>",
                      f"            _c.SendAsync<{entity}>(HttpMethod.Patch, {path} + Uri.EscapeDataString(id), null, patch, ct);",
                      "        /// <summary>Sends exactly these properties, including explicit nulls to clear a field.</summary>",
                      f"        public Task<{entity}> UpdateAsync(string id, IDictionary<string, object?> patch, CancellationToken ct = default) =>",
                      f"            _c.SendAsync<{entity}>(HttpMethod.Patch, {path} + Uri.EscapeDataString(id), null, patch, ct);", ""]
        if "delete" in ops:
            lines += ["        public Task DeleteAsync(string id, CancellationToken ct = default) =>",
                      f'            _c.SendAsync<object>(HttpMethod.Delete, {_str(ops["delete"][0].replace("{id}", ""))} + Uri.EscapeDataString(id), null, null, ct);']
        lines.append("    }\n")
        api_classes.append("\n".join(lines))
        members.append((entity, cls))

    flow_paths = {p: ops["post"] for p, ops in doc["paths"].items() if p.startswith("/flows/")}
    if flow_paths:
        keys = [p.split("/")[2].split(".", 1)[1] for p in flow_paths]
        lines = ["    /// <summary>Server flows the caller's roles may run directly.</summary>", "    public sealed class FlowsApi", "    {", "        private readonly OrchivioDataClient _c;",
                 "        internal FlowsApi(OrchivioDataClient c) => _c = c;", ""]
        for path, op in flow_paths.items():
            module, key = path.split("/")[2].split(".", 1)
            args = cs_type(op["requestBody"]["content"]["application/json"]["schema"]["properties"]["args"])
            member = pascal(key) if keys.count(key) == 1 else pascal(f"{module}_{key}")
            summary = op["summary"].replace("&", "&amp;").replace("<", "&lt;")
            lines += [f"        /// <summary>{summary} (roles: {', '.join(op['x-orchivio-roles'])})</summary>", f"        public Task<FlowRunResult> {member}Async({args} args, CancellationToken ct = default) =>",
                      f'            _c.SendAsync<FlowRunResult>(HttpMethod.Post, {_str(path)}, null, new Dictionary<string, object?> {{ ["args"] = args }}, ct);', ""]
        lines.append("    }\n")
        api_classes.append("\n".join(lines))
        members.append(("Flows" if all(e != "Flows" for e, _ in members) else "FlowRuns", "FlowsApi"))

    header = (f"// Generated by Orchivio from the model of \"{model.application.key}\" ({model_hash(model)}). Do not edit; regenerate it.\n"
              "// Only operations that at least one role may perform are included. Which roles may do what is up to the server.\n"
              "#nullable enable\n"
              "using System;\nusing System.Collections.Generic;\nusing System.Linq;\nusing System.Net.Http;\nusing System.Text;\nusing System.Text.Json;\nusing System.Text.Json.Serialization;\n"
              "using System.Threading;\nusing System.Threading.Tasks;\n\n"
              f"namespace {namespace}\n{{\n")
    exception = '''    /// <summary>The server answered with an error; Code, Message and Details come from its error envelope.</summary>
    public sealed class OrchivioApiException : Exception
    {
        public int Status { get; }
        public string Code { get; }
        public JsonElement? Details { get; }
        public OrchivioApiException(int status, string code, string message, JsonElement? details) : base(code + ": " + message) { Status = status; Code = code; Details = details; }
    }

    public sealed class OrchivioPage<T>
    {
        [JsonPropertyName("items")] public List<T> Items { get; set; } = new();
        [JsonPropertyName("total")] public long Total { get; set; }
        [JsonPropertyName("limit")] public long Limit { get; set; }
        [JsonPropertyName("offset")] public long Offset { get; set; }
    }
'''
    client = ['    /// <summary>new OrchivioDataClient(httpClient, "https://host/api/v1/applications/ID/preview", headers: ct => ...)</summary>',
              "    public sealed class OrchivioDataClient", "    {", "        private static readonly JsonSerializerOptions Json = new() { DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull };",
              "        private readonly HttpClient _http;", "        private readonly string _base;", "        private readonly Func<CancellationToken, Task<IDictionary<string, string>>>? _headers;"]
    client += [f"        public {c} {m} {{ get; }}" for m, c in members]
    client += ["", "        public OrchivioDataClient(HttpClient http, string baseUrl, Func<CancellationToken, Task<IDictionary<string, string>>>? headers = null)", "        {",
               "            _http = http; _base = baseUrl.TrimEnd('/'); _headers = headers;"]
    client += [f"            {m} = new {c}(this);" for m, c in members]
    client += ["        }", "",
               "        internal static string Str(object v) => v switch { bool b => b ? \"true\" : \"false\", IFormattable f => f.ToString(null, System.Globalization.CultureInfo.InvariantCulture), _ => v.ToString() ?? \"\" };", "",
               "        internal async Task<T> SendAsync<T>(HttpMethod method, string path, Dictionary<string, string>? query, object? body, CancellationToken ct)",
               "        {",
               "            var url = _base + path;",
               "            if (query is { Count: > 0 }) url += \"?\" + string.Join(\"&\", query.Select(kv => Uri.EscapeDataString(kv.Key) + \"=\" + Uri.EscapeDataString(kv.Value)));",
               "            using var request = new HttpRequestMessage(method, url);",
               "            request.Headers.Accept.ParseAdd(\"application/json\");",
               "            if (_headers is not null) foreach (var kv in await _headers(ct).ConfigureAwait(false)) request.Headers.TryAddWithoutValidation(kv.Key, kv.Value);",
               "            if (body is not null) request.Content = new StringContent(JsonSerializer.Serialize(body, body.GetType(), Json), Encoding.UTF8, \"application/json\");",
               "            using var response = await _http.SendAsync(request, ct).ConfigureAwait(false);",
               "            var text = await response.Content.ReadAsStringAsync(ct).ConfigureAwait(false);",
               "            if (!response.IsSuccessStatusCode)",
               "            {",
               "                string code = \"http_error\", message = $\"Request failed ({(int)response.StatusCode})\"; JsonElement? details = null;",
               "                try",
               "                {",
               "                    using var doc = JsonDocument.Parse(text);",
               "                    if (doc.RootElement.TryGetProperty(\"error\", out var e) && e.ValueKind == JsonValueKind.Object)",
               "                    {",
               "                        if (e.TryGetProperty(\"code\", out var c) && c.ValueKind == JsonValueKind.String) code = c.GetString()!;",
               "                        if (e.TryGetProperty(\"message\", out var m) && m.ValueKind == JsonValueKind.String) message = m.GetString()!;",
               "                        if (e.TryGetProperty(\"details\", out var d)) details = d.Clone();",
               "                    }",
               "                }",
               "                catch (JsonException) { /* not JSON */ }",
               "                throw new OrchivioApiException((int)response.StatusCode, code, message, details);",
               "            }",
               "            if (string.IsNullOrEmpty(text) || typeof(T) == typeof(object)) return default!;",
               "            return JsonSerializer.Deserialize<T>(text, Json)!;",
               "        }", "    }", ""]
    body = "\n".join(["", *types, *constants, *list_params, *api_classes, exception, *client])
    return header + body.rstrip() + "\n}\n"


__all__ = ["build_csharp_client", "cs_type", "pascal"]
