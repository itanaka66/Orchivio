"""Static model validation (DESIGN.md section 8.4).

``validate_model`` never raises for a structurally valid ``AppModel``; it returns a list
of ``Issue`` objects. Error-severity issues block approval of a proposal.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from orchivio.expr import CheckEnv, ExprError, check
from orchivio.expr.ast import BOOL, DATETIME, DECIMAL, INT, STRING, Type, enum_t, ref_t

from .ids import iter_elements
from .schema import (
    BUILTIN_ENTITIES,
    AppModel,
    EntityDef,
    FieldDef,
    ModuleDef,
    PageDef,
    parse_field_type,
    qualify,
)

Severity = Literal["error", "warning"]

_SCALAR_EXPR_TYPE: dict[str, Type] = {
    "string": STRING,
    "text": STRING,
    "uuid": STRING,
    "integer": INT,
    "decimal": DECIMAL,
    "boolean": BOOL,
    "date": DATETIME,
    "datetime": DATETIME,
}


@dataclass(frozen=True)
class Issue:
    severity: Severity
    code: str
    path: str
    message: str
    element_id: str | None = None

    def as_dict(self) -> dict[str, str | None]:
        return {
            "severity": self.severity,
            "code": self.code,
            "path": self.path,
            "message": self.message,
            "element_id": self.element_id,
        }


class ModelIndex:
    """Lookup tables over a model; also the expression checker's ``SchemaTypes``."""

    def __init__(self, model: AppModel) -> None:
        self.model = model
        self.entities: dict[str, tuple[ModuleDef, EntityDef]] = {}
        self.enums: dict[str, Sequence[str]] = {}
        for mod in model.modules:
            for ent in mod.entities:
                self.entities.setdefault(f"{mod.key}.{ent.name}", (mod, ent))
            for en in mod.enumerations:
                self.enums.setdefault(f"{mod.key}.{en.name}", en.values)

    def resolve_entity(self, module_key: str, name: str) -> str | None:
        q = qualify(module_key, name)
        return q if q in self.entities or q in BUILTIN_ENTITIES else None

    def field(self, qualified_entity: str, name: str) -> FieldDef | None:
        found = self.entities.get(qualified_entity)
        return next((f for f in found[1].fields if f.name == name), None) if found else None

    def types_for(self, module_key: str) -> ModuleTypes:
        return ModuleTypes(self, module_key)


class ModuleTypes:
    """``SchemaTypes`` implementation scoped to one module (for enum name resolution)."""

    def __init__(self, index: ModelIndex, module_key: str) -> None:
        self.index = index
        self.module_key = module_key

    def enum_values(self, name: str) -> Sequence[str] | None:
        return self.index.enums.get(qualify(self.module_key, name))

    def field_type(self, entity: str, field: str) -> Type | None:
        f = self.index.field(entity, field)
        if f is None:
            return None
        return self.expr_type(f, entity.split(".", 1)[0])

    def expr_type(self, f: FieldDef, owner_module: str) -> Type | None:
        parsed = parse_field_type(f.type)
        if parsed is None:
            return None
        kind, target = parsed
        nullable = not (f.required or f.primary_key or f.generated or f.computed)
        if kind == "scalar":
            base = _SCALAR_EXPR_TYPE.get(target or "")
            return None if base is None else Type(base.kind, nullable=nullable)
        assert target is not None
        if kind == "enum":
            q = qualify(owner_module, target)
            if q not in self.index.enums:
                return None
            name = q.split(".", 1)[1] if q.startswith(self.module_key + ".") else q
            return enum_t(name, nullable)
        q = qualify(owner_module, target)
        if q not in self.index.entities and q not in BUILTIN_ENTITIES:
            return None
        return ref_t(q, nullable)


def validate_model(model: AppModel) -> list[Issue]:
    return _Validator(model).run()


def has_errors(issues: Sequence[Issue]) -> bool:
    return any(i.severity == "error" for i in issues)


class _Validator:
    def __init__(self, model: AppModel) -> None:
        self.model = model
        self.index = ModelIndex(model)
        self.issues: list[Issue] = []

    def add(self, sev: Severity, code: str, path: str, msg: str, eid: str | None = None) -> None:
        self.issues.append(Issue(sev, code, path, msg, eid))

    def err(self, code: str, path: str, msg: str, eid: str | None = None) -> None:
        self.add("error", code, path, msg, eid)

    def dupes(self, names: Sequence[str], code: str, path: str, what: str) -> None:
        for name, n in Counter(names).items():
            if n > 1:
                self.err(code, path, f"duplicate {what} '{name}'")

    def run(self) -> list[Issue]:
        m = self.model
        self.dupes([x.key for x in m.modules], "duplicate-module", "/modules", "module")
        all_page_keys: list[str] = []
        all_routes: list[str] = []
        for mi, mod in enumerate(m.modules):
            base = f"/modules/{mi}"
            self.dupes([e.name for e in mod.enumerations], "duplicate-enum", f"{base}/enumerations", "enumeration")
            self.dupes([e.name for e in mod.entities], "duplicate-entity", f"{base}/entities", "entity")
            for xi, en in enumerate(mod.enumerations):
                self.dupes(en.values, "duplicate-enum-value", f"{base}/enumerations/{xi}", "enum value")
            for ei, ent in enumerate(mod.entities):
                self.entity(mod, ent, f"{base}/entities/{ei}")
            for pi, pg in enumerate(mod.pages):
                self.page(mod, pg, f"{base}/pages/{pi}")
                all_page_keys.append(pg.key)
                all_routes.append(pg.path)
        self.dupes(all_page_keys, "duplicate-page", "/modules", "page key")
        self.dupes(all_routes, "duplicate-route", "/modules", "route")
        self.security()
        known = {el.element_id for _, el in iter_elements(m) if el.element_id}
        for si, sup in enumerate(m.lint_suppressions):
            if sup.element_id not in known:
                self.err("unknown-suppression-element", f"/lint_suppressions/{si}", "suppression targets an unknown element")
        return self.issues

    # -- entities and fields
    def entity(self, mod: ModuleDef, ent: EntityDef, path: str) -> None:
        self.dupes([f.name for f in ent.fields], "duplicate-field", f"{path}/fields", "field")
        pks = [f for f in ent.fields if f.primary_key]
        if not ent.transient and len(pks) != 1:
            code = "no-primary-key" if not pks else "multiple-primary-keys"
            self.err(code, path, f"entity {ent.name} needs exactly one primary key", ent.element_id)
        names = {f.name for f in ent.fields}
        for ix, idx in enumerate(ent.indexes):
            for fname in idx.fields:
                if fname not in names:
                    self.err("unknown-index-field", f"{path}/indexes/{ix}", f"unknown field '{fname}'")
        if ent.hooks:
            self.add("warning", "hooks-not-supported", path, "entity hooks need Server Flows (not yet available)")
        qualified = f"{mod.key}.{ent.name}"
        types = self.index.types_for(mod.key)
        computed: dict[str, set[str]] = {}
        for fi, f in enumerate(ent.fields):
            deps = self.field(mod, ent, qualified, f, f"{path}/fields/{fi}", types)
            if deps is not None:
                computed[f.name] = deps
        self.computed_cycles(computed, path)

    def field(
        self, mod: ModuleDef, ent: EntityDef, qualified: str, f: FieldDef, path: str, types: ModuleTypes
    ) -> set[str] | None:
        eid = f.element_id
        parsed = parse_field_type(f.type)
        if parsed is None:
            self.err("bad-type", path, f"invalid type '{f.type}'", eid)
            return None
        kind, target = parsed
        if kind == "enum" and qualify(mod.key, target or "") not in self.index.enums:
            self.err("unknown-enum", path, f"unknown enumeration '{target}'", eid)
        if kind == "ref" and self.index.resolve_entity(mod.key, target or "") is None:
            self.err("unknown-entity", path, f"unknown entity '{target}'", eid)
        scalar = target if kind == "scalar" else None
        if f.max_length is not None and scalar not in ("string", "text"):
            self.err("bad-constraint", path, "max_length applies to string/text only", eid)
        if (f.minimum is not None or f.maximum is not None) and scalar not in ("integer", "decimal"):
            self.err("bad-constraint", path, "minimum/maximum apply to integer/decimal only", eid)
        if f.minimum is not None and f.maximum is not None and f.minimum > f.maximum:
            self.err("bad-constraint", path, "minimum exceeds maximum", eid)
        if f.unique and scalar == "json":
            self.err("bad-constraint", path, "json fields cannot be unique", eid)
        if f.default is not None:
            self.check_default(mod, f, kind, scalar, path)
        env = CheckEnv(types, None)
        field_t = types.expr_type(f, mod.key)
        if f.default_expr and field_t is not None:
            self.expr(f.default_expr, env, field_t, f"{path}/default_expr", eid)
        if f.computed is None:
            return None
        if f.primary_key or f.unique or f.default is not None or f.default_expr or f.generated:
            self.err("bad-computed", path, "computed fields cannot be keys, unique, generated or have defaults", eid)
        if field_t is None:
            return None
        res = self.expr(f.computed.expr, CheckEnv(types, qualified), field_t, f"{path}/computed/expr", eid)
        if res is None:
            return set()
        peers = {x.name for x in ent.fields if x.computed}
        return set(res.fields) & peers

    def check_default(self, mod: ModuleDef, f: FieldDef, kind: str, scalar: str | None, path: str) -> None:
        d = f.default
        ok = {
            "integer": isinstance(d, int) and not isinstance(d, bool),
            "decimal": isinstance(d, (int, float)) and not isinstance(d, bool),
            "boolean": isinstance(d, bool),
            "string": isinstance(d, str),
            "text": isinstance(d, str),
            "date": isinstance(d, str),
            "datetime": isinstance(d, str),
            "uuid": isinstance(d, str),
        }
        if kind == "scalar" and not ok.get(scalar or "", False):
            self.err("bad-default", path, f"default {d!r} does not match type {f.type}", f.element_id)
        if kind == "enum":
            values = self.index.enums.get(qualify(mod.key, parse_field_type(f.type)[1] or ""))  # type: ignore[index]
            if values is not None and d not in values:
                self.err("bad-default", path, f"default {d!r} is not a value of {f.type}", f.element_id)
        if kind == "ref":
            self.err("bad-default", path, "references cannot have literal defaults", f.element_id)

    def computed_cycles(self, graph: dict[str, set[str]], path: str) -> None:
        state: dict[str, int] = {}

        def visit(n: str, stack: list[str]) -> None:
            if state.get(n) == 2:
                return
            if state.get(n) == 1:
                cycle = " -> ".join([*stack[stack.index(n):], n])
                self.err("computed-cycle", path, f"computed fields form a cycle: {cycle}")
                return
            state[n] = 1
            for dep in sorted(graph.get(n, ())):
                visit(dep, [*stack, n])
            state[n] = 2

        for n in sorted(graph):
            visit(n, [])

    def expr(self, src, env: CheckEnv, expected: Type, path: str, eid: str | None):
        try:
            return check(src, env, expected)
        except ExprError as e:
            self.err("expression", path, f"{e.message} (at {e.pos}) in: {src}", eid)
            return None

    # -- pages
    def page(self, mod: ModuleDef, pg: PageDef, path: str) -> None:
        qualified = self.index.resolve_entity(mod.key, pg.entity)
        if qualified is None or qualified == "system.User":
            self.err("unknown-entity", path, f"page '{pg.key}' uses unknown entity '{pg.entity}'", pg.element_id)
            return
        if pg.type == "form" and pg.mode is None:
            self.err("missing-mode", path, "form pages need mode create|edit", pg.element_id)
        for col in pg.columns:
            if self.index.field(qualified, col) is None:
                self.err("unknown-column", path, f"unknown column '{col}'", pg.element_id)
        types = self.index.types_for(qualified.split(".", 1)[0])
        env = CheckEnv(types, qualified)
        for ai, act in enumerate(pg.actions):
            if isinstance(act, str):
                continue
            for attr in ("visible_expr", "editable_expr"):
                src = getattr(act, attr)
                if src:
                    self.expr(src, env, BOOL, f"{path}/actions/{ai}/{attr}", pg.element_id)

    # -- security
    def security(self) -> None:
        sec = self.model.security
        self.dupes(sec.roles, "duplicate-role", "/security/roles", "role")
        roles = set(sec.roles)
        seen: Counter[tuple[str, str]] = Counter()
        covered: set[str] = set()
        for ai, rule in enumerate(sec.access):
            path = f"/security/access/{ai}"
            seen[(rule.entity, rule.role)] += 1
            if seen[(rule.entity, rule.role)] == 2:
                self.err("duplicate-access", path, f"several rules for {rule.entity} / {rule.role}", rule.element_id)
            if rule.role not in roles:
                self.err("unknown-role", path, f"unknown role '{rule.role}'", rule.element_id)
            found = self.index.entities.get(rule.entity)
            if found is None:
                self.err("unknown-entity", path, f"unknown entity '{rule.entity}'", rule.element_id)
                continue
            covered.add(rule.entity)
            if rule.row_constraint:
                env = CheckEnv(self.index.types_for(found[0].key), rule.entity)
                self.expr(rule.row_constraint, env, BOOL, f"{path}/row_constraint", rule.element_id)
        for q, (_, ent) in self.index.entities.items():
            if not ent.transient and q not in covered:
                self.err("no-access-rule", "/security/access", f"entity {q} has no access rule (deny-by-default must be explicit)", ent.element_id)
        page_keys = {p.key for mod in self.model.modules for p in mod.pages}
        for key, page_roles in sec.pages.items():
            if key not in page_keys:
                self.err("unknown-page", f"/security/pages/{key}", f"unknown page '{key}'")
            for r in page_roles:
                if r not in roles:
                    self.err("unknown-role", f"/security/pages/{key}", f"unknown role '{r}'")
