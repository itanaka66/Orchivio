"""Static validation of Server and Client Flows (docs/FLOW-SPEC.md, FR-052/053).

A flow is a directed acyclic graph. Loops are an explicit, bounded ``loop`` step, never a cycle
in the graph, so every flow terminates. Expressions are type-checked with the flow's variables
(parameters and step outputs) in scope.
"""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Callable
from typing import Any

from orchivio.expr import CheckEnv, ExprError, check
from orchivio.expr.ast import BOOL, DECIMAL, INT, STRING, Type, list_t

from .schema import BUILTIN_ENTITIES, FieldDef, FlowDef, FlowStep, ModuleDef, qualify

Report = Callable[[str, str, str, str], None]  # (code, path, message, severity)

SERVER_STEPS = {"start", "retrieve", "create", "change", "delete", "decision", "loop", "call_flow", "call_integration",
                "call_mcp", "raise_error", "end"}
CLIENT_STEPS = {"start", "decision", "call_flow", "end"}  # client flows cannot touch data (FR-053)
TERMINAL = {"end", "raise_error"}
HANDLES = {"decision": {"true", "false"}, "loop": {"body", "done"}}
MAX_ITERATIONS = 1000
MAX_RETRIEVE = 1000


def handles_for(step_type: str) -> set[str]:
    if step_type in TERMINAL:
        return set()
    return HANDLES.get(step_type, {"out"})


class FlowChecker:
    """Validates one flow. ``index`` is a ``ModelIndex``; ``flows`` maps qualified key -> FlowDef."""

    def __init__(self, index, mod: ModuleDef, flow: FlowDef, flows: dict[str, FlowDef], report: Report, base: str,
                 implicit: dict[str, Type] | None = None) -> None:
        self.index, self.mod, self.flow, self.flows = index, mod, flow, flows
        self.implicit = implicit or {}  # variables supplied by the caller, e.g. `record` in a hook flow
        self.report, self.base = report, base
        self.steps = {s.id: s for s in flow.steps}
        self.types = index.types_for(mod.key)
        self.out: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
        self.succ: dict[str, set[str]] = defaultdict(set)
        self.pred: dict[str, set[str]] = defaultdict(set)

    def err(self, code: str, where: str, msg: str) -> None:
        self.report(code, f"{self.base}/{where}" if where else self.base, msg, "error")

    def warn(self, code: str, where: str, msg: str) -> None:
        self.report(code, f"{self.base}/{where}" if where else self.base, msg, "warning")

    # -- structure ------------------------------------------------------------------------

    def run(self) -> None:
        f = self.flow
        self._unique([s.id for s in f.steps], "duplicate-step", "steps", "step id")
        self._unique([e.id for e in f.edges], "duplicate-edge", "edges", "edge id")
        self._unique([p.name for p in f.params], "duplicate-param", "params", "parameter")
        allowed = SERVER_STEPS if f.kind == "server" else CLIENT_STEPS
        for s in f.steps:
            if s.type not in allowed:
                self.err("step-not-allowed", f"steps/{s.id}", f"a {f.kind} flow cannot use a '{s.type}' step")
        self._edges()
        if not self._start():
            return
        self._reachability()
        if self._cycles():
            return  # variable scoping needs an acyclic graph
        self._variables_and_config()
        self._tests()

    def _unique(self, items: list[str], code: str, where: str, what: str) -> None:
        seen: set[str] = set()
        for i in items:
            if i in seen:
                self.err(code, where, f"duplicate {what} '{i}'")
            seen.add(i)

    def _edges(self) -> None:
        for e in self.flow.edges:
            src, dst = self.steps.get(e.source), self.steps.get(e.target)
            if src is None or dst is None:
                self.err("edge-unknown-step", f"edges/{e.id}", "edge refers to a step that does not exist")
                continue
            if e.source == e.target:
                self.err("edge-self-loop", f"edges/{e.id}", "a step cannot connect to itself; use a loop step")
            if e.handle not in handles_for(src.type):
                self.err("edge-bad-handle", f"edges/{e.id}",
                         f"a '{src.type}' step has no '{e.handle}' output (valid: {sorted(handles_for(src.type)) or 'none'})")
                continue
            if dst.type == "start":
                self.err("edge-into-start", f"edges/{e.id}", "nothing may connect into the start step")
            self.out[e.source][e.handle].append(e.target)
            self.succ[e.source].add(e.target)
            self.pred[e.target].add(e.source)
        in_body: set[str] = set()  # a loop body ends by returning to its loop, so it needs no end step
        for s in self.flow.steps:
            if s.type == "loop":
                for target in self.out[s.id]["body"]:
                    in_body |= self._reach(target)
        for s in self.flow.steps:
            for handle in handles_for(s.type):
                n = len(self.out[s.id][handle])
                if n > 1:
                    self.err("multiple-edges", f"steps/{s.id}", f"output '{handle}' has {n} connections; only one is allowed")
                elif n == 0 and s.type in HANDLES:
                    self.err("missing-branch", f"steps/{s.id}", f"a {s.type} step needs its '{handle}' output connected")
                elif n == 0 and s.type != "start" and s.id not in in_body:
                    self.warn("implicit-end", f"steps/{s.id}", "no outgoing connection: the flow ends here (add an end step to make this explicit)")

    def _start(self) -> bool:
        starts = [s for s in self.flow.steps if s.type == "start"]
        if len(starts) != 1:
            self.err("flow-start", "steps", f"a flow needs exactly one start step (found {len(starts)})")
            return False
        if not self.out[starts[0].id]["out"]:
            self.err("start-unconnected", f"steps/{starts[0].id}", "the start step is not connected to anything")
        self.start_id = starts[0].id
        return True

    def _reach(self, origin: str) -> set[str]:
        seen, q = {origin}, deque([origin])
        while q:
            for n in self.succ[q.popleft()]:
                if n not in seen:
                    seen.add(n)
                    q.append(n)
        return seen

    def _reachability(self) -> None:
        reachable = self._reach(self.start_id)
        for s in self.flow.steps:
            if s.id not in reachable:
                self.err("unreachable-step", f"steps/{s.id}", "this step cannot be reached from the start step")

    def _cycles(self) -> bool:
        state: dict[str, int] = {}
        found = False

        def visit(n: str, stack: list[str]) -> None:
            nonlocal found
            if state.get(n) == 2 or found:
                return
            if state.get(n) == 1:
                found = True
                self.err("flow-cycle", "edges", "the flow contains a cycle: " + " -> ".join([*stack[stack.index(n):], n])
                         + ". Repeat work with a loop step instead")
                return
            state[n] = 1
            for m in sorted(self.succ[n]):
                visit(m, [*stack, n])
            state[n] = 2

        for s in self.flow.steps:
            visit(s.id, [])
        return found

    # -- variables and per-step configuration --------------------------------------------------

    def _type_of(self, type_str: str) -> Type | None:
        return self.types.expr_type(FieldDef(name="v", type=type_str, required=True), self.mod.key)

    def _entity_type(self, name: Any, where: str) -> tuple[str, Type] | None:
        if not isinstance(name, str):
            self.err("config", where, "'entity' is required")
            return None
        q = self.index.resolve_entity(self.mod.key, name)
        if q is None or q in BUILTIN_ENTITIES or self.index.entities[q][1].transient:
            self.err("config", where, f"unknown or non-persistent entity '{name}'")
            return None
        return q, Type("entity", q)

    def _variables_and_config(self) -> None:
        f = self.flow
        params: dict[str, Type] = dict(self.implicit)
        for p in f.params:
            if p.name in self.implicit:
                self.err("duplicate-variable", f"params/{p.name}", f"'{p.name}' is provided automatically to hook flows")
                continue
            t = self._type_of(p.type)
            if t is None:
                self.err("bad-type", f"params/{p.name}", f"invalid parameter type '{p.type}'")
            else:
                params[p.name] = t
        # variable definitions: name -> (defining step id, type, scope: None = whole downstream | set of step ids)
        defs: dict[str, tuple[str, Type, set[str] | None]] = {}

        def define(name: Any, step: FlowStep, t: Type, scope: set[str] | None = None) -> None:
            if not isinstance(name, str) or not name or not name.replace("_", "a").isalnum() or not name[0].islower():
                self.err("config", f"steps/{step.id}", "variable names are lower_snake_case")
                return
            if name in params or name in defs:
                self.err("duplicate-variable", f"steps/{step.id}", f"variable '{name}' is already defined")
                return
            defs[name] = (step.id, t, scope)

        ancestors = {s.id: self._ancestors(s.id) for s in f.steps}
        order = self._topological()
        for sid in order:  # definitions are collected in graph order so later steps can use earlier ones
            step = self.steps[sid]
            cfg = step.config
            env = {n: t for n, t in params.items()}
            for vname, (dsid, t, scope) in defs.items():
                visible = dsid in ancestors[sid] and (scope is None or sid in scope)
                if visible:
                    env[vname] = t
            self._check_step(step, cfg, env, define)

        # a step may only use variables defined upstream; checked above through ``env``

    def _tests(self) -> None:
        roles = set(self.index.model.security.roles)
        names: set[str] = set()
        params = {p.name for p in self.flow.params}
        for i, t in enumerate(self.flow.tests):
            w = f"tests/{i}"
            if t.name in names:
                self.err("duplicate-test", w, f"duplicate test name '{t.name}'")
            names.add(t.name)
            for extra in set(t.args) - params:
                self.err("test-config", f"{w}/args/{extra}", f"the flow has no parameter '{extra}'")
            for missing in params - set(t.args):
                self.err("test-config", f"{w}/args", f"missing argument '{missing}'")
            for r in (t.user.roles or []):
                if r not in roles:
                    self.err("test-config", f"{w}/user", f"unknown role '{r}'")
            for mock_key in (t.mocks or {}):
                ig_key, _, op_id = mock_key.partition(".")
                found = self.index.integrations.get(qualify(self.mod.key, ig_key))
                if found is None or not any(o.id == op_id for o in found[1].operations):
                    self.err("test-config", f"{w}/mocks/{mock_key}", "use \"<integration>.<operation>\" of an existing operation")
            for ent, rows in t.fixtures.items():
                q = self.index.resolve_entity(self.mod.key, ent)
                if q is None or q in BUILTIN_ENTITIES or self.index.entities[q][1].transient:
                    self.err("test-config", f"{w}/fixtures/{ent}", f"unknown or non-persistent entity '{ent}'")
                    continue
                known = {f.name for f in self.index.entities[q][1].fields}
                for row in rows:
                    for field_name in set(row) - known:
                        self.err("test-config", f"{w}/fixtures/{ent}", f"entity has no field '{field_name}'")
            step_ids = set(self.steps)
            for sid in [*t.expect.visited, *t.expect.not_visited]:
                if sid not in step_ids:
                    self.err("test-config", f"{w}/expect", f"unknown step '{sid}'")
            if t.expect.error_contains and t.expect.status != "failed":
                self.err("test-config", f"{w}/expect", "error_contains only makes sense with status: failed")
            for j, r in enumerate(t.expect.records):
                q = self.index.resolve_entity(self.mod.key, r.entity)
                if q is None or q in BUILTIN_ENTITIES or self.index.entities[q][1].transient:
                    self.err("test-config", f"{w}/expect/records/{j}", f"unknown or non-persistent entity '{r.entity}'")
                elif r.where:
                    self._expr(r.where, f"{w}/expect/records/{j}/where", {}, BOOL, record_entity=q)

    def _ancestors(self, sid: str) -> set[str]:
        seen, q = set(), deque([sid])
        while q:
            for p in self.pred[q.popleft()]:
                if p not in seen:
                    seen.add(p)
                    q.append(p)
        return seen

    def _topological(self) -> list[str]:
        indeg = {s.id: len(self.pred[s.id]) for s in self.flow.steps}
        q = deque(sorted(i for i, d in indeg.items() if d == 0))
        out = []
        while q:
            n = q.popleft()
            out.append(n)
            for m in sorted(self.succ[n]):
                indeg[m] -= 1
                if indeg[m] == 0:
                    q.append(m)
        return out

    def _expr_result(self, src: Any, where: str, env: dict[str, Type], expected: Type | None, record_entity: str | None = None):
        if not isinstance(src, str) or not src.strip():
            self.err("expression", where, "an expression is required")
            return None
        try:
            return check(src, CheckEnv(self.types, record_entity, variables=env), expected)
        except ExprError as e:
            self.err("expression", where, f"{e.message} (at {e.pos}) in: {src[:120]}")
            return None

    def _expr(self, src: Any, where: str, env: dict[str, Type], expected: Type | None, record_entity: str | None = None) -> Type | None:
        res = self._expr_result(src, where, env, expected, record_entity)
        return None if res is None else res.type

    def _values(self, step: FlowStep, qname: str, values: Any, env: dict[str, Type], *, require_all: bool, key: str = "values") -> None:
        where = f"steps/{step.id}/config/{key}"
        ent = self.index.entities[qname][1]
        if not isinstance(values, dict):
            self.err("config", where, "'values' must map field names to expressions")
            return
        fields = {x.name: x for x in ent.fields}
        for name, src in values.items():
            fld = fields.get(name)
            if fld is None:
                self.err("config", f"{where}/{name}", f"entity {ent.name} has no field '{name}'")
            elif fld.generated or fld.computed or (fld.primary_key and require_all is False):
                self.err("config", f"{where}/{name}", f"field '{name}' is managed by the system")
            else:
                t = self.index.types_for(self.index.entities[qname][0].key).expr_type(fld, self.index.entities[qname][0].key)
                if t is not None:
                    self._expr(src, f"{where}/{name}", env, t)
        if require_all:
            for fld in ent.fields:
                if fld.required and not (fld.generated or fld.computed or fld.default is not None or fld.default_expr) \
                        and fld.name not in values:
                    self.err("config", where, f"required field '{fld.name}' has no value")

    def _check_step(self, step: FlowStep, cfg: dict[str, Any], env: dict[str, Type], define) -> None:  # noqa: C901
        w = f"steps/{step.id}/config"
        t = step.type
        unknown = set(cfg) - {"retrieve": {"entity", "where", "into", "many", "limit"},
                              "create": {"entity", "values", "into"}, "change": {"target", "values"},
                              "delete": {"target"}, "decision": {"expr"},
                              "loop": {"over", "item", "max_iterations"}, "call_flow": {"flow", "args", "into"},
                              "call_integration": {"integration", "operation", "args", "body", "into"},
                              "call_mcp": {"server", "tool", "args", "into"},
                              "raise_error": {"message"}}.get(t, set())
        if unknown:
            self.err("config", w, f"unknown setting(s) for a {t} step: {', '.join(sorted(unknown))}")
        if t == "retrieve":
            ent = self._entity_type(cfg.get("entity"), f"{w}/entity")
            if ent:
                q, et = ent
                if cfg.get("where") is not None:
                    res = self._expr_result(cfg["where"], f"{w}/where", env, BOOL, record_entity=q)
                    if res is not None:  # the condition runs as a query, so it can only use stored columns
                        ent_def = self.index.entities[q][1]
                        virtual = sorted(f.name for f in ent_def.fields if f.computed and f.computed.mode == "virtual" and f.name in res.fields)
                        if virtual:
                            self.err("expression", f"{w}/where", f"cannot filter on computed field(s): {', '.join(virtual)}")
                many = bool(cfg.get("many", False))
                if many:
                    lim = cfg.get("limit", 100)
                    if not isinstance(lim, int) or isinstance(lim, bool) or not 1 <= lim <= MAX_RETRIEVE:
                        self.err("config", f"{w}/limit", f"limit must be an integer from 1 to {MAX_RETRIEVE}")
                    define(cfg.get("into"), step, list_t(et))
                else:
                    define(cfg.get("into"), step, et.as_nullable())
        elif t == "create":
            ent = self._entity_type(cfg.get("entity"), f"{w}/entity")
            if ent:
                self._values(step, ent[0], cfg.get("values", {}), env, require_all=True)
                if cfg.get("into") is not None:
                    define(cfg["into"], step, ent[1])
        elif t in ("change", "delete"):
            target = cfg.get("target")
            vt = env.get(target) if isinstance(target, str) else None
            if vt is None or vt.kind != "entity":
                self.err("config", f"{w}/target", "'target' must name a record variable defined earlier in the flow")
            elif t == "change":
                self._values(step, vt.name or "", cfg.get("values", {}), env, require_all=False)
        elif t == "decision":
            self._expr(cfg.get("expr"), f"{w}/expr", env, BOOL)
        elif t == "loop":
            over = cfg.get("over")
            lt = env.get(over) if isinstance(over, str) else None
            mi = cfg.get("max_iterations")
            if lt is None or lt.kind != "list" or lt.elem is None or lt.elem.kind != "entity":
                self.err("config", f"{w}/over", "'over' must name a list of records defined earlier in the flow")
            if not isinstance(mi, int) or isinstance(mi, bool) or not 1 <= mi <= MAX_ITERATIONS:
                self.err("loop-unbounded", f"{w}/max_iterations", f"a loop needs max_iterations between 1 and {MAX_ITERATIONS}")
            if lt is not None and lt.kind == "list" and lt.elem is not None:
                body_targets = self.out[step.id]["body"]
                scope = self._reach(body_targets[0]) if body_targets else set()
                done = self.out[step.id]["done"]
                if done and scope & self._reach(done[0]):
                    self.err("loop-body-escapes", f"steps/{step.id}", "the loop body must not connect to the steps after the loop")
                define(cfg.get("item"), step, lt.elem, scope)
        elif t == "call_flow":
            target = cfg.get("flow")
            q = qualify(self.mod.key, target) if isinstance(target, str) else ""
            callee = self.flows.get(q)
            if callee is None:
                self.err("config", f"{w}/flow", f"unknown flow '{target}'")
            else:
                if self.flow.kind == "server" and callee.kind != "server":
                    self.err("config", f"{w}/flow", "a server flow can only call server flows")
                args = cfg.get("args", {})
                if not isinstance(args, dict):
                    self.err("config", f"{w}/args", "'args' must map parameter names to expressions")
                    args = {}
                owner = q.split(".", 1)[0]
                for p in callee.params:
                    pt = self.index.types_for(owner).expr_type(FieldDef(name="v", type=p.type, required=True), owner)
                    if p.name not in args:
                        self.err("config", f"{w}/args", f"missing argument '{p.name}'")
                    elif pt is not None:
                        self._expr(args[p.name], f"{w}/args/{p.name}", env, pt)
                for extra in set(args) - {p.name for p in callee.params}:
                    self.err("config", f"{w}/args/{extra}", f"flow '{target}' has no parameter '{extra}'")
        elif t == "call_integration":
            self._check_call_integration(step, cfg, env, define)
        elif t == "call_mcp":
            server = cfg.get("server")
            tool = cfg.get("tool")
            if not isinstance(server, str) or not server:
                self.err("config", f"{w}/server", "'server' must name a registered MCP server")
            if not isinstance(tool, str) or not tool:
                self.err("config", f"{w}/tool", "'tool' must name a tool on that MCP server")
            if cfg.get("into") is not None:
                define(cfg["into"], step, STRING)
        elif t == "raise_error":
            m = cfg.get("message")
            if not isinstance(m, str) or not 1 <= len(m) <= 200:
                self.err("config", f"{w}/message", "message is required (1-200 characters)")
        elif cfg:
            self.err("config", w, f"a {t} step has no settings")


_PARAM_TYPES = {"string": STRING, "integer": INT, "decimal": DECIMAL, "boolean": BOOL}


def _check_call_integration(self, step: FlowStep, cfg: dict[str, Any], env: dict[str, Type], define) -> None:
    w = f"steps/{step.id}/config"
    key, op_id = cfg.get("integration"), cfg.get("operation")
    found = self.index.integrations.get(qualify(self.mod.key, key)) if isinstance(key, str) else None
    if found is None:
        self.err("config", f"{w}/integration", f"unknown integration '{key}'")
        return
    mod, ig = found
    op = next((o for o in ig.operations if o.id == op_id), None)
    if op is None:
        self.err("config", f"{w}/operation", f"integration '{key}' has no operation '{op_id}'")
        return
    args = cfg.get("args", {})
    if not isinstance(args, dict):
        self.err("config", f"{w}/args", "'args' must map parameter names to expressions")
        args = {}
    for p in op.params:
        if p.name in args:
            self._expr(args[p.name], f"{w}/args/{p.name}", env, _PARAM_TYPES[p.type])
        elif p.required:
            self.err("config", f"{w}/args", f"missing argument '{p.name}'")
    for extra in sorted(set(args) - {p.name for p in op.params}):
        self.err("config", f"{w}/args/{extra}", f"operation '{op_id}' has no parameter '{extra}'")
    if op.body:
        q = self.index.resolve_entity(mod.key, op.body)
        if q in self.index.entities:
            self._values(step, q, cfg.get("body", {}), env, require_all=True, key="body")
    elif cfg.get("body"):
        self.err("config", f"{w}/body", f"operation '{op_id}' sends no body")
    if op.response:
        q = self.index.resolve_entity(mod.key, op.response)
        if q in self.index.entities:
            et = Type("entity", q)
            into = cfg.get("into")
            if into is None:
                self.err("config", f"{w}/into", "'into' names the variable that receives the response")
            else:
                define(into, step, list_t(et) if op.response_kind == "list" else et)
    elif cfg.get("into") is not None:
        self.err("config", f"{w}/into", f"operation '{op_id}' returns nothing")


FlowChecker._check_call_integration = _check_call_integration  # type: ignore[attr-defined]


def flow_contract() -> dict[str, Any]:
    """What the flow editor needs to know, exported to apps/web/src/flow/contract.json so the
    UI cannot drift from the validator (a test fails if the file is stale)."""
    from typing import get_args

    from .schema import FLOW_KEY_PATTERN, STEP_ID_PATTERN, FlowDef, FlowEdge, FlowStep

    step_types = list(get_args(FlowStep.model_fields["type"].annotation))
    return {
        "stepTypes": step_types,
        "serverSteps": [t for t in step_types if t in SERVER_STEPS],
        "clientSteps": [t for t in step_types if t in CLIENT_STEPS],
        "handles": {t: sorted(handles_for(t), key=["out", "true", "false", "body", "done"].index) for t in step_types},
        "handleNames": list(get_args(FlowEdge.model_fields["handle"].annotation)),
        "limits": {
            "maxIterations": MAX_ITERATIONS,
            "maxRetrieve": MAX_RETRIEVE,
            "maxSteps": FlowDef.model_fields["steps"].metadata[0].max_length,
            "maxEdges": FlowDef.model_fields["edges"].metadata[0].max_length,
        },
        "patterns": {"stepId": STEP_ID_PATTERN, "flowKey": FLOW_KEY_PATTERN},
    }


def call_cycles(flows: dict[str, FlowDef], module_of: dict[str, ModuleDef]) -> list[list[str]]:
    """Cycles in the flow call graph (recursion has no bound, so it is rejected)."""
    graph: dict[str, set[str]] = {}
    for q, fl in flows.items():
        mod = module_of[q]
        targets = set()
        for s in fl.steps:
            if s.type == "call_flow" and isinstance(s.config.get("flow"), str):
                t = qualify(mod.key, s.config["flow"])
                if t in flows:
                    targets.add(t)
        graph[q] = targets
    cycles, state = [], {}

    def visit(n: str, stack: list[str]) -> None:
        if state.get(n) == 2:
            return
        if state.get(n) == 1:
            cycles.append([*stack[stack.index(n):], n])
            return
        state[n] = 1
        for m in sorted(graph[n]):
            visit(m, [*stack, n])
        state[n] = 2

    for q in sorted(graph):
        visit(q, [])
    return cycles

