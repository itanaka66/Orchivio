"""Static model validation (DESIGN.md section 8.4).

``validate_model`` never raises for a structurally valid ``AppModel``; it returns a list
of ``Issue`` objects. Error-severity issues block approval of a proposal.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from orchivio.expr import CheckEnv, ExprError, check
from orchivio.expr.ast import BOOL, DATETIME, DECIMAL, INT, STRING, Type, enum_t, ref_t

from .cron import CronError, parse_cron
from .flows import FlowChecker, call_cycles
from .hooks import HOOK_EVENTS
from .ids import iter_elements
from .processes import ProcessChecker
from .schema import (
    BUILTIN_ENTITIES,
    AppModel,
    EntityDef,
    FieldDef,
    FlowDef,
    IntegrationDef,
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
        self.integrations: dict[str, tuple[ModuleDef, IntegrationDef]] = {}
        for mod in model.modules:
            for ent in mod.entities:
                self.entities.setdefault(f"{mod.key}.{ent.name}", (mod, ent))
            for en in mod.enumerations:
                self.enums.setdefault(f"{mod.key}.{en.name}", en.values)
            for ig in mod.integrations or []:
                self.integrations.setdefault(f"{mod.key}.{ig.key}", (mod, ig))

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


def validate_flow(model: AppModel, module_key: str, flow: FlowDef) -> list[Issue]:
    """Validate one (possibly unsaved) flow in the context of ``model``. Used for live editor feedback."""
    mi = next((i for i, m in enumerate(model.modules) if m.key == module_key), None)
    if mi is None:
        raise KeyError(module_key)
    v = _Validator(model)
    flows = {f"{m.key}.{f.key}": f for m in model.modules for f in m.flows}
    module_of = {f"{m.key}.{f.key}": m for m in model.modules for f in m.flows}
    flows[f"{module_key}.{flow.key}"], module_of[f"{module_key}.{flow.key}"] = flow, model.modules[mi]
    bound = v.hook_bindings(flows)
    v.check_flow(model.modules[mi], mi, flow, flows, v.implicit_for(bound, f"{module_key}.{flow.key}"))
    for cycle in call_cycles(flows, module_of):
        if f"{module_key}.{flow.key}" in cycle:
            v.err("flow-call-cycle", f"/modules/{mi}/flows/{flow.key}", "flows call each other in a cycle: " + " -> ".join(cycle), flow.element_id)
    return v.issues


def literal_fits(param_type: str, v: object) -> bool:
    """Is the JSON value ``v`` acceptable for a flow parameter of this type? (Other types travel as strings.)"""
    return {"integer": isinstance(v, int) and not isinstance(v, bool), "boolean": isinstance(v, bool),
            "decimal": isinstance(v, (int, float)) and not isinstance(v, bool)}.get(param_type, v is None or isinstance(v, str))


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
            self.dupes([f.key for f in mod.flows], "duplicate-flow", f"{base}/flows", "flow key")
            self.integrations(mod, base)
        self.dupes(all_page_keys, "duplicate-page", "/modules", "page key")
        self.dupes(all_routes, "duplicate-route", "/modules", "route")
        self.flows()
        self.security()
        known = {el.element_id for _, el in iter_elements(m) if el.element_id}
        for si, sup in enumerate(m.lint_suppressions):
            if sup.element_id not in known:
                self.err("unknown-suppression-element", f"/lint_suppressions/{si}", "suppression targets an unknown element")
        return self.issues

    # -- consumed REST APIs
    def integrations(self, mod: ModuleDef, base: str) -> None:
        self.dupes([i.key for i in mod.integrations or []], "duplicate-integration", f"{base}/integrations", "integration key")
        for ig in mod.integrations or []:
            ipath = f"{base}/integrations/{ig.key}"
            self.dupes([o.id for o in ig.operations], "duplicate-operation", f"{ipath}/operations", "operation id")
            for op in ig.operations:
                p = f"{ipath}/operations/{op.id}"
                eid = ig.element_id
                self.dupes([x.name for x in op.params], "duplicate-param", f"{p}/params", "parameter")
                placeholders = re.findall(r"\{([^{}]*)\}", op.path)
                declared = {x.name for x in op.params if x.location == "path"}
                for name in placeholders:
                    if name not in declared:
                        self.err("integration-path", p, f"the path uses {{{name}}} but no path parameter has that name", eid)
                for name in sorted(declared - set(placeholders)):
                    self.err("integration-path", p, f"path parameter '{name}' does not appear in the path", eid)
                for x in op.params:
                    if x.location == "path" and not x.required:
                        self.err("integration-path", p, f"path parameter '{x.name}' must be required", eid)
                if op.body and op.method in ("GET", "DELETE"):
                    self.err("integration-body", p, f"a {op.method} operation cannot send a body", eid)
                if bool(op.response) != bool(op.response_kind):
                    self.err("integration-response", p, "response and response_kind go together", eid)
                for kind, name, mapping in (("body", op.body, op.body_map), ("response", op.response, op.response_map)):
                    if name is None:
                        if mapping:
                            self.err("integration-map", p, f"{kind}_map needs a {kind} entity", eid)
                        continue
                    q = self.index.resolve_entity(mod.key, name)
                    found = self.index.entities.get(q or "")
                    if found is None or not found[1].transient:
                        self.err("integration-entity", p, f"{kind} entity '{name}' must be a transient entity of the model", eid)
                        continue
                    names = {f.name for f in found[1].fields}
                    for fname in mapping:
                        if fname not in names:
                            self.err("integration-map", p, f"{kind}_map names unknown field '{fname}' of {name}", eid)

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
        if kind == "ref":
            resolved = self.index.resolve_entity(mod.key, target or "")
            if resolved is None:
                self.err("unknown-entity", path, f"unknown entity '{target}'", eid)
            elif resolved in self.index.entities and self.index.entities[resolved][1].transient:
                self.err("ref-to-transient", path, f"'{target}' is transient and has no stored records to reference", eid)
        if f.generated and not (kind == "scalar" and target in ("uuid", "datetime")):
            self.err("bad-generated", path, "only uuid and datetime fields can be generated", eid)
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
            self.page_action_flow(mod, qualified, pg, ai, act, env, path)

    def page_action_flow(self, mod: ModuleDef, entity: str, pg: PageDef, ai: int, act, env: CheckEnv, path: str) -> None:
        p = f"{path}/actions/{ai}"
        if act.flow is None:
            if act.args:
                self.err("page-action-flow", p, "args need a flow", pg.element_id)
            return
        q = qualify(mod.key, act.flow)
        flow = next((f for m in self.model.modules for f in m.flows if f"{m.key}.{f.key}" == q), None)
        if flow is None or flow.kind != "server":
            self.err("page-action-flow", p, f"action '{act.key}' names '{act.flow}', which is not a server flow", pg.element_id)
            return
        if any(q == qualify(m.key, h) for m in self.model.modules for e in m.entities for h in e.hooks.values()):
            self.err("page-action-flow", p, "an entity hook flow runs on its event and cannot be a page action", pg.element_id)
        args = act.args or {}
        owner = q.split(".", 1)[0]
        for prm in flow.params:
            pt = self.index.types_for(owner).expr_type(FieldDef(name="v", type=prm.type, required=True), owner)
            if prm.name not in args:
                self.err("page-action-flow", p, f"missing argument '{prm.name}' for flow '{act.flow}'", pg.element_id)
            elif pt is not None:
                self.expr(args[prm.name], env, pt, f"{p}/args/{prm.name}", pg.element_id)
        for extra in sorted(set(args) - {x.name for x in flow.params}):
            self.err("page-action-flow", f"{p}/args/{extra}", f"flow '{act.flow}' has no parameter '{extra}'", pg.element_id)

    # -- flows
    def hook_bindings(self, flows: dict[str, FlowDef]) -> dict[str, str]:
        """flow (qualified) -> entity (qualified) for flows bound as entity event hooks; reports misuse."""
        bound: dict[str, str] = {}
        for q, (mod, ent) in self.index.entities.items():
            for event, key in ent.hooks.items():
                path = f"/modules/{self.model.modules.index(mod)}/entities/{mod.entities.index(ent)}/hooks/{event}"
                fq = qualify(mod.key, key)
                fl = flows.get(fq)
                if event not in HOOK_EVENTS:
                    self.err("bad-hook", path, f"unknown hook event '{event}' (valid: {', '.join(HOOK_EVENTS)})", ent.element_id)
                elif fl is None:
                    self.err("bad-hook", path, f"hook flow '{key}' does not exist", ent.element_id)
                elif fl.kind != "server" or fl.params:
                    self.err("bad-hook", path, f"hook flow '{key}' must be a server flow without parameters", ent.element_id)
                elif bound.setdefault(fq, q) != q:
                    self.err("bad-hook", path, f"flow '{key}' is already the hook of {bound[fq]}; a hook flow serves one entity", ent.element_id)
        return bound

    def implicit_for(self, bound: dict[str, str], fq: str) -> dict[str, Type] | None:
        q = bound.get(fq)
        if q is None:
            return None
        return {"record": Type("entity", q), "previous": Type("entity", q, nullable=True)}

    def flows(self) -> None:
        flows = {f"{m.key}.{f.key}": f for m in self.model.modules for f in m.flows}
        module_of = {f"{m.key}.{f.key}": m for m in self.model.modules for f in m.flows}
        bound = self.hook_bindings(flows)
        for mi, mod in enumerate(self.model.modules):
            for fl in mod.flows:
                self.check_flow(mod, mi, fl, flows, self.implicit_for(bound, f"{mod.key}.{fl.key}"))
        for cycle in call_cycles(flows, module_of):
            self.err("flow-call-cycle", "/modules", "flows call each other in a cycle: " + " -> ".join(cycle))
        hook_flows = set(bound)
        roles = set(self.model.security.roles)
        for mi, mod in enumerate(self.model.modules):
            self.dupes([p.key for p in mod.processes or []], "duplicate-process", f"/modules/{mi}/processes", "process key")
            for pr in mod.processes or []:
                def report(code: str, path: str, msg: str, sev: str, pr=pr) -> None:
                    self.add(sev, code, path, msg, pr.element_id)  # type: ignore[arg-type]

                ProcessChecker(self.index, mod, pr, flows, hook_flows, roles, report, f"/modules/{mi}/processes/{pr.key}").run()

    def check_flow(self, mod: ModuleDef, mi: int, fl: FlowDef, flows: dict[str, FlowDef],
                   implicit: dict[str, Type] | None = None) -> None:
        def report(code: str, path: str, msg: str, sev: str) -> None:
            self.add(sev, code, path, msg, fl.element_id)  # type: ignore[arg-type]

        FlowChecker(self.index, mod, fl, flows, report, f"/modules/{mi}/flows/{fl.key}", implicit).run()
        if fl.run_roles is not None:
            for r in fl.run_roles:
                if r not in self.model.security.roles:
                    self.err("unknown-role", f"/modules/{mi}/flows/{fl.key}/run_roles", f"unknown role '{r}'", fl.element_id)
            if fl.kind != "server" or implicit is not None:
                self.err("run-roles-not-runnable", f"/modules/{mi}/flows/{fl.key}/run_roles",
                         "only a server flow that is not an entity hook can be run directly", fl.element_id)
        if fl.schedule is not None:
            self.check_schedule(fl, f"/modules/{mi}/flows/{fl.key}/schedule", implicit is not None)

    def check_schedule(self, fl: FlowDef, path: str, is_hook: bool) -> None:
        s = fl.schedule
        assert s is not None
        eid = fl.element_id
        if fl.kind != "server":
            self.err("schedule-not-server", path, "only server flows can be scheduled", eid)
        if is_hook:
            self.err("schedule-hook", path, "an entity hook flow runs on its event and cannot also be scheduled", eid)
        try:
            parse_cron(s.cron)
        except CronError as e:
            self.err("bad-cron", f"{path}/cron", f"invalid cron expression: {e}", eid)
        if s.run_as_role not in self.model.security.roles:
            self.err("unknown-role", f"{path}/run_as_role", f"role '{s.run_as_role}' is not defined in security.roles", eid)
        params = {p.name: p for p in fl.params}
        for name in s.args:
            if name not in params:
                self.err("schedule-args", f"{path}/args/{name}", f"flow has no parameter '{name}'", eid)
        for name, p in params.items():
            if name not in s.args:
                self.err("schedule-args", f"{path}/args", f"parameter '{name}' needs a value", eid)
                continue
            if not literal_fits(p.type, s.args[name]):
                self.err("schedule-args", f"{path}/args/{name}", f"value does not fit parameter type '{p.type}'", eid)

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
            if rule.fields is not None:
                known = {f.name for f in found[1].fields}
                for kind in ("read", "write"):
                    names = getattr(rule.fields, kind)
                    if names is None:
                        continue
                    self.dupes(names, "duplicate-access-field", f"{path}/fields/{kind}", "field")
                    for n in names:
                        if n not in known:
                            self.err("unknown-access-field", f"{path}/fields/{kind}", f"{rule.entity} has no field '{n}'", rule.element_id)
                if rule.fields.read is not None and "read" not in rule.operations:
                    self.err("access-field-unused", f"{path}/fields/read", "fields.read is set but the rule does not grant read", rule.element_id)
                if rule.fields.write is not None and not {"create", "update"} & set(rule.operations):
                    self.err("access-field-unused", f"{path}/fields/write", "fields.write is set but the rule grants neither create nor update", rule.element_id)
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
