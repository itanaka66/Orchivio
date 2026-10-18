"""OpenAPI 3.x -> typed integration definition (FR-081).

A pure function: a parsed specification in, an ``IntegrationDef`` plus the transient entities that type its request
bodies and responses out. Only what the model can express is imported, and everything left out is reported rather
than silently dropped:

* parameters in the path and query with scalar types (header and cookie parameters, arrays and objects are skipped);
* JSON request bodies that are objects, JSON responses that are objects or arrays of objects (success codes only);
* local ``$ref`` only (``#/components/...``). Remote references are never fetched.

Names are normalised to the model's identifier rules; where a name changes, the original is kept as ``json_name`` or
in ``body_map``/``response_map`` so the wire format is unchanged. Specifications are untrusted input: size, depth,
operation and property counts are bounded.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from orchivio.model.schema import (
    EntityDef,
    FieldDef,
    IntegrationDef,
    IntegrationOperation,
    IntegrationParam,
)

MAX_OPERATIONS = 200
MAX_PROPERTIES = 100
MAX_DEPTH = 12
METHODS = ("get", "post", "put", "patch", "delete")


class ImportProblem(Exception):
    """The document cannot be imported at all."""


@dataclass
class ImportResult:
    integration: IntegrationDef
    entities: list[EntityDef]
    warnings: list[str] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)


def snake(name: str, fallback: str = "field") -> str:
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(name))
    s = re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_").lower()
    if not s:
        s = fallback
    if not s[0].isalpha():
        s = f"{fallback[0]}_{s}"
    return s[:63]


def pascal(name: str, fallback: str = "Entity") -> str:
    parts = re.split(r"[^A-Za-z0-9]+", re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(name)))
    s = "".join(p[:1].upper() + p[1:] for p in parts if p)
    if not s:
        s = fallback
    if not s[0].isalpha():
        s = fallback + s
    return s[:63]


def _unique(base: str, taken: set[str]) -> str:
    name, n = base, 2
    while name in taken:
        suffix = f"_{n}" if base[:1].islower() else str(n)
        name = base[: 63 - len(suffix)] + suffix
        n += 1
    taken.add(name)
    return name


class _Importer:
    def __init__(self, doc: dict[str, Any], key: str, destination: str, taken_entities: set[str]) -> None:
        self.doc, self.key, self.destination = doc, key, destination
        self.taken_entities = set(taken_entities)
        self.entities: list[EntityDef] = []
        self.by_ref: dict[str, str] = {}
        self.warnings: list[str] = []
        self.skipped: list[dict[str, str]] = []

    # -- schema helpers -------------------------------------------------------------------------

    def resolve(self, schema: Any, depth: int = 0) -> dict[str, Any]:
        if not isinstance(schema, dict):
            return {}
        if depth > MAX_DEPTH:
            raise ImportProblem("the specification nests references too deeply (or is circular)")
        ref = schema.get("$ref")
        if ref is not None:
            if not isinstance(ref, str) or not ref.startswith("#/"):
                raise ImportProblem(f"only local references are supported, found {str(ref)[:80]!r}")
            node: Any = self.doc
            for part in ref[2:].split("/"):
                node = node.get(part.replace("~1", "/").replace("~0", "~")) if isinstance(node, dict) else None
            if not isinstance(node, dict):
                raise ImportProblem(f"unresolvable reference {ref[:80]!r}")
            return self.resolve(node, depth + 1)
        if "allOf" in schema and isinstance(schema["allOf"], list):
            merged: dict[str, Any] = {k: v for k, v in schema.items() if k != "allOf"}
            props: dict[str, Any] = dict(merged.get("properties") or {})
            required = list(merged.get("required") or [])
            for part in schema["allOf"][:10]:
                r = self.resolve(part, depth + 1)
                props.update(r.get("properties") or {})
                required += r.get("required") or []
                merged.setdefault("type", r.get("type"))
            merged["properties"], merged["required"] = props, required
            return merged
        return schema

    @staticmethod
    def kind(schema: dict[str, Any]) -> str | None:
        t = schema.get("type")
        if isinstance(t, list):  # OpenAPI 3.1: ["string", "null"]
            t = next((x for x in t if x != "null"), None)
        if t is None and "properties" in schema:
            return "object"
        return t if isinstance(t, str) else None

    def scalar_type(self, schema: dict[str, Any]) -> tuple[str, int | None]:
        k = self.kind(schema)
        if k == "integer":
            return "integer", None
        if k == "number":
            return "decimal", None
        if k == "boolean":
            return "boolean", None
        if k == "string":
            fmt = schema.get("format")
            if fmt == "date":
                return "date", None
            if fmt == "date-time":
                return "datetime", None
            if fmt == "uuid":
                return "uuid", None
            ml = schema.get("maxLength")
            return "string", (ml if isinstance(ml, int) and 1 <= ml <= 10_000 else None)
        return "json", None

    # -- entities -------------------------------------------------------------------------------

    def entity_for(self, schema_node: Any, hint: str, where: str) -> tuple[str, dict[str, str]] | None:
        """A transient entity for an object schema, or None when it is not an object."""
        ref = schema_node.get("$ref") if isinstance(schema_node, dict) else None
        schema = self.resolve(schema_node)
        if self.kind(schema) != "object":
            return None
        props = schema.get("properties")
        if not isinstance(props, dict) or not props:
            self.warnings.append(f"{where}: the object has no properties, so it is imported as an empty record")
            props = {}
        if isinstance(ref, str) and ref in self.by_ref:
            name = self.by_ref[ref]
            return name, {k: v for k, v in self._maps[name].items()}
        base = pascal(ref.rsplit("/", 1)[-1] if isinstance(ref, str) else hint)
        name = _unique(base, self.taken_entities)
        required = set(schema.get("required") or [])
        used: set[str] = set()
        fields: list[FieldDef] = []
        mapping: dict[str, str] = {}
        for i, (prop, sub) in enumerate(props.items()):
            if i >= MAX_PROPERTIES:
                self.warnings.append(f"{where}: more than {MAX_PROPERTIES} properties; the rest are ignored")
                break
            fname = _unique(snake(prop), used)
            ftype, max_len = self.scalar_type(self.resolve(sub))
            if ftype == "json" and self.kind(self.resolve(sub)) not in (None, "object", "array"):
                self.warnings.append(f"{where}.{prop}: unsupported schema, imported as free-form json")
            fields.append(FieldDef(name=fname, type=ftype, required=prop in required, **({"max_length": max_len} if max_len else {})))
            if fname != prop:
                mapping[fname] = str(prop)
        self.entities.append(EntityDef(name=name, transient=True, fields=fields))
        if isinstance(ref, str):
            self.by_ref[ref] = name
        self._maps[name] = mapping
        return name, mapping

    _maps: dict[str, dict[str, str]]

    # -- operations -----------------------------------------------------------------------------

    def run(self) -> ImportResult:
        self._maps = {}
        paths = self.doc.get("paths")
        if not isinstance(paths, dict) or not paths:
            raise ImportProblem("the specification has no paths")
        ops: list[IntegrationOperation] = []
        used_ids: set[str] = set()
        for path, item in paths.items():
            if not isinstance(item, dict) or not isinstance(path, str):
                continue
            shared = item.get("parameters") if isinstance(item.get("parameters"), list) else []
            for method in METHODS:
                spec = item.get(method)
                if not isinstance(spec, dict):
                    continue
                if len(ops) >= MAX_OPERATIONS:
                    self.skipped.append({"operation": f"{method.upper()} {path}", "reason": f"more than {MAX_OPERATIONS} operations"})
                    continue
                try:
                    op = self.operation(method, path, spec, shared, used_ids)
                except _Skip as e:
                    self.skipped.append({"operation": f"{method.upper()} {path}", "reason": str(e)})
                    continue
                ops.append(op)
        if not ops:
            raise ImportProblem("no operation could be imported: " + "; ".join(s["reason"] for s in self.skipped[:3]))
        info = self.doc.get("info") if isinstance(self.doc.get("info"), dict) else {}
        desc = str(info.get("title") or "")[:200] or None
        return ImportResult(IntegrationDef(key=self.key, description=desc, destination=self.destination, operations=ops),
                            self.entities, self.warnings, self.skipped)

    def operation(self, method: str, path: str, spec: dict[str, Any], shared: list[Any], used_ids: set[str]) -> IntegrationOperation:
        label = f"{method.upper()} {path}"
        oid = _unique(snake(spec.get("operationId") or f"{method}_{path}", "op"), used_ids)
        raw_params = [self._param_node(p) for p in [*shared, *(spec.get("parameters") or [])]]
        by_key: dict[tuple[str, str], dict[str, Any]] = {}
        for p in raw_params:  # operation-level parameters override path-level ones
            if p is not None:
                by_key[(p.get("in", ""), p.get("name", ""))] = p
        params: list[IntegrationParam] = []
        names: set[str] = set()
        wire_path = path
        for (where, wire), p in by_key.items():
            if where not in ("path", "query"):
                if where in ("header", "cookie"):
                    self.warnings.append(f"{label}: {where} parameter '{wire}' is not imported")
                continue
            schema = self.resolve(p.get("schema") or {})
            if self.kind(schema) in ("array", "object"):
                if where == "path" or p.get("required"):
                    raise _Skip(f"{where} parameter '{wire}' is an {self.kind(schema)}, which is not supported")
                self.warnings.append(f"{label}: optional {where} parameter '{wire}' ({self.kind(schema)}) is not imported")
                continue
            ptype, _ = self.scalar_type(schema)
            ptype = ptype if ptype in ("string", "integer", "decimal", "boolean") else "string"  # dates etc. travel as text
            pname = _unique(snake(wire, "param"), names)
            required = True if where == "path" else bool(p.get("required", False))
            params.append(IntegrationParam(name=pname, location=where, type=ptype, required=required,  # type: ignore[arg-type]
                                           **({"json_name": wire} if pname != wire else {})))
            if where == "path":
                wire_path = wire_path.replace("{" + wire + "}", "{" + pname + "}")
        for placeholder in re.findall(r"\{([^{}]*)\}", wire_path):
            if not any(p.name == placeholder and p.location == "path" for p in params):
                raise _Skip(f"the path uses {{{placeholder}}} without a matching path parameter")
        if not re.fullmatch(r"/[A-Za-z0-9/_\-.{}~%:@!$&'()*+,;=]*", wire_path) or len(wire_path) > 300:
            raise _Skip("the path contains characters or a length the model cannot represent")

        body, body_map = None, {}
        rb = spec.get("requestBody")
        if isinstance(rb, dict):
            rb = self.resolve(rb)
            content = rb.get("content") if isinstance(rb.get("content"), dict) else {}
            media = next((m for m in content if m == "application/json" or m.endswith("+json")), None)
            if media is None:
                if rb.get("required"):
                    raise _Skip("the request body is not JSON, which is not supported")
                self.warnings.append(f"{label}: a non-JSON request body is not imported")
            elif method in ("get", "delete"):
                self.warnings.append(f"{label}: a {method.upper()} request body is not imported")
            else:
                found = self.entity_for((content[media] or {}).get("schema") or {}, f"{oid}_request", f"{label} request")
                if found is None:
                    if rb.get("required"):
                        raise _Skip("the request body is not a JSON object, which is not supported")
                    self.warnings.append(f"{label}: the request body is not an object and is not imported")
                else:
                    body, body_map = found

        response, kind, response_map = None, None, {}
        responses = spec.get("responses") if isinstance(spec.get("responses"), dict) else {}
        code = next((c for c in ("200", "201", "202") if c in responses), None) or next(
            (c for c in sorted(responses) if str(c).startswith("2")), None)
        if code is not None:
            resp = self.resolve(responses[code])
            content = resp.get("content") if isinstance(resp.get("content"), dict) else {}
            media = next((m for m in content if m == "application/json" or m.endswith("+json")), None)
            if media is not None:
                schema_node = (content[media] or {}).get("schema") or {}
                schema = self.resolve(schema_node)
                hint = f"{oid}_response"
                if self.kind(schema) == "array":
                    found = self.entity_for(schema.get("items") or {}, hint, f"{label} response")
                    if found is None:
                        self.warnings.append(f"{label}: the response is a list of non-objects and is not imported")
                    else:
                        response, response_map = found
                        kind = "list"
                else:
                    found = self.entity_for(schema_node, hint, f"{label} response")
                    if found is None:
                        self.warnings.append(f"{label}: the response is not an object or a list of objects and is not imported")
                    else:
                        response, response_map = found
                        kind = "object"
            elif content:
                self.warnings.append(f"{label}: a non-JSON response is not imported")
        return IntegrationOperation(id=oid, method=method.upper(), path=wire_path, summary=(str(spec.get("summary") or "")[:200] or None),  # type: ignore[arg-type]
                                    params=params, body=body, body_map=body_map, response=response, response_kind=kind,  # type: ignore[arg-type]
                                    response_map=response_map)

    def _param_node(self, p: Any) -> dict[str, Any] | None:
        try:
            r = self.resolve(p)
        except ImportProblem:
            raise
        return r if isinstance(r, dict) and isinstance(r.get("name"), str) else None


class _Skip(Exception):
    pass


def import_openapi(doc: Any, *, key: str, destination: str, taken_entities: set[str] | None = None) -> ImportResult:
    """``taken_entities``: entity names of the target module, so new transient entities never collide."""
    if not isinstance(doc, dict):
        raise ImportProblem("the specification must be a JSON or YAML object")
    version = str(doc.get("openapi", ""))
    if not version.startswith("3."):
        raise ImportProblem("only OpenAPI 3.x documents are supported" + (" (Swagger 2.0 must be converted first)" if "swagger" in doc else ""))
    return _Importer(doc, key, destination, taken_entities or set()).run()
