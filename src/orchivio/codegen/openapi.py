"""OpenAPI 3.1 document for the data API of a generated application (FR-032).

The document describes the operations the runtime implements (list, create, read, update, delete
per persistent entity) with schemas derived from the model. Only operations that at least one role
may perform are included: everything else is denied by default and would always answer 403. Which
roles may do what, and under which row constraints, is documented in ``x-orchivio-*`` extensions.

``tests/integration/test_openapi.py`` validates real API responses against these schemas, so the
document cannot silently drift from the implementation.
"""

from __future__ import annotations

from typing import Any

from orchivio.model import AppModel, ModelIndex
from orchivio.model.schema import FieldDef, parse_field_type, qualify
from orchivio.security import resolve_access, resolve_fields

OPS = (("list", "read"), ("create", "create"), ("read", "read"), ("update", "update"), ("delete", "delete"))
ERROR_REF = {"$ref": "#/components/schemas/Error"}


def _nullable(schema: dict[str, Any]) -> dict[str, Any]:
    if "$ref" in schema or schema == {}:
        return schema
    out = dict(schema)
    t = out.get("type")
    out["type"] = [*t, "null"] if isinstance(t, list) else [t, "null"]
    if "enum" in out:
        out["enum"] = [*out["enum"], None]
    return out


class _Gen:
    def __init__(self, model: AppModel) -> None:
        self.model = model
        self.index = ModelIndex(model)
        self.entities = {q: (m, e) for q, (m, e) in self.index.entities.items() if not e.transient}
        names = [e.name for _, e in self.entities.values()]
        # Entity name when it is unique across modules, otherwise module_Entity
        self.name = {q: (e.name if names.count(e.name) == 1 else f"{m.key}_{e.name}") for q, (m, e) in self.entities.items()}

    # -- field schemas ------------------------------------------------------------------------------

    def value_schema(self, mod_key: str, f: FieldDef, *, for_input: bool) -> dict[str, Any]:
        kind, target = parse_field_type(f.type) or ("scalar", "string")
        if kind == "enum":
            return {"type": "string", "enum": list(self.index.enums[qualify(mod_key, target or "")])}
        if kind == "ref":
            q = self.index.resolve_entity(mod_key, target or "")
            if q in self.entities:
                m, ent = self.entities[q]
                pk = next(x for x in ent.fields if x.primary_key)
                return self.value_schema(m.key, pk, for_input=for_input)
            return {"type": "string", "minLength": 1, "maxLength": 64}  # system.User: an opaque end-user id
        s: dict[str, Any]
        match target:
            case "string":
                s = {"type": "string", "maxLength": f.max_length or 255}
            case "text":
                s = {"type": "string", **({"maxLength": f.max_length} if f.max_length else {})}
            case "integer":
                s = {"type": "integer", "format": "int64"}
            case "decimal":
                # The API returns decimals as strings to keep precision; it accepts numbers or numeric strings.
                s = ({"type": ["number", "string"], "description": "A decimal number; a string preserves precision."}
                     if for_input else {"type": "string", "pattern": r"^-?\d+(\.\d+)?$"})
            case "boolean":
                s = {"type": "boolean"}
            case "date":
                s = {"type": "string", "format": "date"}
            case "datetime":
                s = {"type": "string", "format": "date-time"}
            case "uuid":
                s = {"type": "string", "format": "uuid"}
            case _:  # json
                return {"description": "Any JSON value."}
        if for_input and target in ("integer", "decimal"):
            if f.minimum is not None:
                s["minimum"] = f.minimum
            if f.maximum is not None:
                s["maximum"] = f.maximum
        return s

    def entity_schemas(self, q: str) -> dict[str, dict[str, Any]]:
        mod, ent = self.entities[q]
        name = self.name[q]
        resp_props, create_props, update_props, create_required = {}, {}, {}, []
        for f in ent.fields:
            system = f.generated or bool(f.computed)
            nullable_out = not (f.required or f.primary_key or f.generated or f.computed)
            out = self.value_schema(mod.key, f, for_input=False)
            out = _nullable(out) if nullable_out else out
            if f.display_name:
                out = {**out, "title": f.display_name}
            if system:
                out = {**out, "readOnly": True}
            if f.computed:
                out = {**out, "x-orchivio-computed": f.computed.mode}
            resp_props[f.name] = out
            if system:
                continue
            inp = self.value_schema(mod.key, f, for_input=True)
            if not f.required:
                inp = _nullable(inp)
            create_props[f.name] = update_props[f.name] = inp
            if f.required and f.default is None and not f.default_expr:
                create_required.append(f.name)
        return {
            name: {"type": "object", "properties": resp_props, "required": self.always_present(q, ent),
                   "additionalProperties": False, **({"title": ent.display_name} if ent.display_name else {})},
            f"{name}Create": {"type": "object", "properties": create_props, "additionalProperties": False,
                              **({"required": create_required} if create_required else {})},
            f"{name}Update": {"type": "object", "properties": update_props, "additionalProperties": False},
            f"{name}Page": {"type": "object", "required": ["items", "total", "limit", "offset"], "properties": {
                "items": {"type": "array", "items": {"$ref": f"#/components/schemas/{name}"}},
                "total": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                "offset": {"type": "integer", "minimum": 0}}, "additionalProperties": False},
        }

    # -- operations ---------------------------------------------------------------------------------

    def roles_for(self, q: str, operation: str) -> list[str]:
        return [r for r in self.model.security.roles if resolve_access(self.model, [r], q, operation).allowed]

    def always_present(self, q: str, ent) -> list[str]:
        """Fields every role that can read the entity receives; field-level access removes the others."""
        sets = [resolve_fields(self.model, [r], q, "read") for r in self.roles_for(q, "read")]
        return [f.name for f in ent.fields if all(s is None or f.name in s or f.primary_key for s in sets)]

    def field_access(self, q: str, access: str) -> dict[str, list[str]]:
        key = "read" if access == "read" else "write"
        out: dict[str, list[str]] = {}
        for r in self.model.security.access:
            if r.entity == q and access in r.operations and r.fields is not None and getattr(r.fields, key) is not None:
                out[r.role] = sorted(getattr(r.fields, key))
        return out

    def constraints(self, q: str, operation: str) -> dict[str, str]:
        return {r.role: r.row_constraint for r in self.model.security.access
                if r.entity == q and operation in r.operations and r.row_constraint}

    def operation(self, q: str, kind: str, access: str, roles: list[str]) -> dict[str, Any]:
        _, ent = self.entities[q]
        name = self.name[q]
        label = ent.display_name or ent.name
        op: dict[str, Any] = {
            "operationId": f"{kind}_{q.replace('.', '_')}",
            "tags": [q],
            "summary": {"list": f"List {label} records", "create": f"Create a {label}", "read": f"Read a {label}",
                        "update": f"Update a {label}", "delete": f"Delete a {label}"}[kind],
            "x-orchivio-roles": roles,
            "responses": {},
        }
        if (c := self.constraints(q, access)):
            op["x-orchivio-row-constraints"] = c
        if (fa := self.field_access(q, access)):
            op["x-orchivio-field-access"] = {"restricted_roles": fa, "applies_to": "response fields" if access == "read" else "writable fields"}
        r = op["responses"]
        err = lambda desc: {"description": desc, "content": {"application/json": {"schema": ERROR_REF}}}  # noqa: E731
        r["403"] = err("The caller's roles do not allow this operation.")
        if kind == "list":
            op["parameters"] = self.list_parameters(q)
            r["200"] = {"description": "A page of records the caller may read.", "content": {"application/json": {
                "schema": {"$ref": f"#/components/schemas/{name}Page"}}}}
            r["422"] = err("Unknown filter or sort field, or an invalid filter value.")
        elif kind == "create":
            op["requestBody"] = {"required": True, "content": {"application/json": {"schema": {"$ref": f"#/components/schemas/{name}Create"}}}}
            r["201"] = {"description": "The created record.", "content": {"application/json": {"schema": {"$ref": f"#/components/schemas/{name}"}}}}
            r["409"] = err("A unique value is taken, or an entity hook refused the change.")
            r["422"] = err("The record is invalid (every problem is listed in error.details).")
        else:
            op["parameters"] = [{"$ref": "#/components/parameters/id"}]
            r["404"] = err("No such record, or the caller may not see it.")
            if kind == "read":
                r["200"] = {"description": "The record.", "content": {"application/json": {"schema": {"$ref": f"#/components/schemas/{name}"}}}}
            elif kind == "update":
                op["requestBody"] = {"required": True, "content": {"application/json": {"schema": {"$ref": f"#/components/schemas/{name}Update"}}}}
                r["200"] = {"description": "The updated record.", "content": {"application/json": {"schema": {"$ref": f"#/components/schemas/{name}"}}}}
                r["409"] = err("A unique value is taken, or an entity hook refused the change.")
                r["422"] = err("The change makes the record invalid.")
            else:
                r["204"] = {"description": "Deleted."}
                r["409"] = err("Other records still reference this one, or an entity hook refused the change.")
        return op

    def list_parameters(self, q: str) -> list[dict[str, Any]]:
        mod, ent = self.entities[q]
        stored = [f for f in ent.fields if not (f.computed and f.computed.mode == "virtual")]
        params: list[dict[str, Any]] = [
            {"name": "limit", "in": "query", "schema": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50}},
            {"name": "offset", "in": "query", "schema": {"type": "integer", "minimum": 0, "default": 0}},
            {"name": "order_by", "in": "query", "description": "A stored field; default is the primary key.",
             "schema": {"type": "string", "enum": [f.name for f in stored]}},
            {"name": "desc", "in": "query", "schema": {"type": "boolean", "default": False}},
        ]
        for f in stored:
            kind, target = parse_field_type(f.type) or ("scalar", "string")
            if target in ("json", "decimal", "date", "datetime") and kind == "scalar":
                continue  # equality filters are offered for simple scalar, enum and reference fields
            params.append({"name": f"f.{f.name}", "in": "query", "description": f"Records whose {f.name} equals this value.",
                           "schema": {k: v for k, v in self.value_schema(mod.key, f, for_input=True).items() if k not in ("minimum", "maximum")}})
        return params

    def build(self, title: str | None, version: str | None) -> dict[str, Any]:
        app = self.model.application
        paths: dict[str, Any] = {}
        schemas: dict[str, Any] = {"Error": {
            "type": "object", "required": ["error"], "additionalProperties": False, "properties": {"error": {
                "type": "object", "required": ["code", "message"], "properties": {
                    "code": {"type": "string"}, "message": {"type": "string"}, "details": {}}}}}}
        tags = []
        for q in sorted(self.entities):
            allowed = {kind: self.roles_for(q, access) for kind, access in OPS}
            if not any(allowed.values()):
                continue
            schemas.update(self.entity_schemas(q))
            tags.append({"name": q, "description": self.entities[q][1].display_name or self.entities[q][1].name})
            base, item = f"/data/{q}", f"/data/{q}/{{id}}"
            for kind, access in OPS:
                if not allowed[kind]:
                    continue
                path = base if kind in ("list", "create") else item
                method = {"list": "get", "create": "post", "read": "get", "update": "patch", "delete": "delete"}[kind]
                paths.setdefault(path, {})[method] = self.operation(q, kind, access, allowed[kind])
        return {
            "openapi": "3.1.0",
            "info": {"title": title or app.name, "version": version or app.version,
                     "description": "Data API of the application. Operations that no role may perform are omitted (deny by default). "
                                    "In the Studio preview these paths are served under /api/v1/applications/{id}/preview.",
                     "x-orchivio-application": app.key},
            "tags": tags,
            "paths": paths,
            "components": {"schemas": schemas, "parameters": {"id": {
                "name": "id", "in": "path", "required": True, "description": "The record's primary key.", "schema": {"type": "string"}}}},
        }


def build_openapi(model: AppModel, *, title: str | None = None, version: str | None = None) -> dict[str, Any]:
    """The OpenAPI 3.1 document for ``model``'s data API."""
    return _Gen(model).build(title, version)
