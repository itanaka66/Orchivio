"""Static validation of process definitions (FR-110, docs/PROCESS-SPEC.md).

A process is a graph of steps that tokens travel along. Unlike a flow it may contain cycles (a rejected request goes
back for rework), so the guarantees are different: every cycle must pass through a step that waits (a user task or a
timer), otherwise it could spin forever without anyone noticing. Expressions see the process parameters, the implicit
``initiator`` (the id of whoever started the instance) and the form fields of user tasks completed earlier (nullable,
because a path may not have visited that task).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from typing import Any

from orchivio.expr import CheckEnv, ExprError, check
from orchivio.expr.ast import BOOL, DECIMAL, INT, STRING, Type

from .schema import FieldDef, FlowDef, ModuleDef, ProcessDef, qualify

Report = Callable[[str, str, str, str], None]  # (code, path, message, severity)

WAITING = {"user_task", "timer"}
MAX_HOURS = 24 * 365
MAX_TIMER_SECONDS = 365 * 24 * 3600
FORM_TYPES = {"string": STRING, "integer": INT, "decimal": DECIMAL, "boolean": BOOL}
CONFIG_KEYS = {
    "user_task": {"title", "assignee_role", "assignee_expr", "outcomes", "form", "due_in_hours", "escalate_after_hours", "escalate_to_role"},
    "system_task": {"flow", "args"},
    "decision": {"expr"},
    "timer": {"wait_seconds"},
}


def process_env(index, mod: ModuleDef, proc: ProcessDef, err: Callable[[str, str, str], None] | None = None) -> dict[str, Type]:
    """Variable types visible to a process's expressions: parameters, ``initiator``, and the (nullable) form fields."""
    report = err or (lambda code, path, msg: None)
    env: dict[str, Type] = {"initiator": STRING}
    seen: set[str] = set()
    for p in proc.params:
        if p.name in seen or p.name == "initiator":
            report("process-variable", "params", f"parameter name '{p.name}' is duplicated or reserved")
            continue
        seen.add(p.name)
        t = index.types_for(mod.key).expr_type(FieldDef(name="v", type=p.type, required=True), mod.key)
        if t is None:
            report("process-variable", f"params/{p.name}", f"unknown type '{p.type}'")
        else:
            env[p.name] = t
    for s in proc.steps:
        if s.type != "user_task":
            continue
        for i, f in enumerate(s.config.get("form") or []):
            if not isinstance(f, dict) or not isinstance(f.get("name"), str):
                continue
            ftype = FORM_TYPES.get(f.get("type", "string"))
            if f["name"] in env and f["name"] in seen | {"initiator"}:
                report("process-variable", f"steps/{s.id}/config/form/{i}", f"'{f['name']}' clashes with a parameter or reserved name")
            elif ftype is not None:
                env.setdefault(f["name"], ftype.as_nullable())
    return env


class ProcessChecker:
    def __init__(self, index, mod: ModuleDef, proc: ProcessDef, flows: dict[str, FlowDef], hook_flows: set[str], roles: set[str],
                 report: Report, base: str) -> None:
        self.index, self.mod, self.proc, self.flows, self.hook_flows, self.roles = index, mod, proc, flows, hook_flows, roles
        self.report, self.base = report, base
        self.steps = {s.id: s for s in proc.steps}
        self.out: dict[str, list] = defaultdict(list)
        self.inc: dict[str, list] = defaultdict(list)
        self.env: dict[str, Type] = {}

    def err(self, code: str, path: str, msg: str) -> None:
        self.report(code, f"{self.base}/{path}" if path else self.base, msg, "error")

    def warn(self, code: str, path: str, msg: str) -> None:
        self.report(code, f"{self.base}/{path}" if path else self.base, msg, "warning")

    def run(self) -> None:
        self.params()
        self.structure()
        for s in self.proc.steps:
            self.step(s)
        self.reachability()
        for r in self.proc.start_roles:
            if r not in self.roles:
                self.err("unknown-role", "start_roles", f"unknown role '{r}'")
        if self.proc.run_as_role is not None and self.proc.run_as_role not in self.roles:
            self.err("unknown-role", "run_as_role", f"unknown role '{self.proc.run_as_role}'")

    # -- variables ------------------------------------------------------------------------------

    def params(self) -> None:
        self.env = process_env(self.index, self.mod, self.proc, self.err)

    # -- graph ----------------------------------------------------------------------------------

    def structure(self) -> None:
        ids = [s.id for s in self.proc.steps]
        for sid in {i for i in ids if ids.count(i) > 1}:
            self.err("duplicate-step", f"steps/{sid}", f"step id '{sid}' is used more than once")
        starts = [s for s in self.proc.steps if s.type == "start"]
        if len(starts) != 1:
            self.err("process-start", "steps", "a process needs exactly one start step")
        if not any(s.type == "end" for s in self.proc.steps):
            self.err("process-end", "steps", "a process needs at least one end step")
        eids = [e.id for e in self.proc.edges]
        for eid in {i for i in eids if eids.count(i) > 1}:
            self.err("duplicate-edge", f"edges/{eid}", f"edge id '{eid}' is used more than once")
        for e in self.proc.edges:
            if e.source not in self.steps or e.target not in self.steps:
                self.err("edge-unknown-step", f"edges/{e.id}", "an edge connects a step that does not exist")
                continue
            self.out[e.source].append(e)
            self.inc[e.target].append(e)

    def step(self, s) -> None:
        w = f"steps/{s.id}"
        cfg = s.config
        outs, t = self.out[s.id], s.type
        extra = set(cfg) - CONFIG_KEYS.get(t, set())
        if extra:
            self.err("process-config", f"{w}/config", f"unknown setting(s) for a {t} step: {', '.join(sorted(extra))}")
        if self.inc[s.id] and t == "start":
            self.err("process-edges", w, "nothing may lead into the start step")
        if t == "end":
            if outs:
                self.err("process-edges", w, "an end step has no outgoing edges")
            return
        plain = [e for e in outs if e.outcome is None]
        if t in ("start", "system_task", "timer", "parallel_join"):
            if len(outs) != 1 or plain != outs:
                self.err("process-edges", w, f"a {t.replace('_', ' ')} step needs exactly one outgoing edge without an outcome")
        elif t == "parallel_split":
            if len(outs) < 2 or plain != outs:
                self.err("process-edges", w, "a parallel split needs at least two outgoing edges without an outcome")
        elif t == "decision":
            got = sorted(e.outcome or "" for e in outs)
            if got != ["false", "true"]:
                self.err("process-edges", w, "a decision needs exactly one 'true' and one 'false' outgoing edge")
        if t == "parallel_join" and len(self.inc[s.id]) < 2:
            self.err("process-join", w, "a parallel join needs at least two incoming edges")
        if t == "user_task":
            self.user_task(s, w, cfg, outs)
        elif t == "system_task":
            self.system_task(s, w, cfg)
        elif t == "decision":
            self.expr(cfg.get("expr"), f"{w}/config/expr", BOOL)
        elif t == "timer":
            n = cfg.get("wait_seconds")
            if not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= MAX_TIMER_SECONDS:
                self.err("process-config", f"{w}/config/wait_seconds", f"wait_seconds must be an integer from 1 to {MAX_TIMER_SECONDS}")

    def user_task(self, s, w: str, cfg: dict[str, Any], outs: list) -> None:
        c = f"{w}/config"
        title = cfg.get("title")
        if not isinstance(title, str) or not 1 <= len(title) <= 200:
            self.err("process-config", f"{c}/title", "a title of 1-200 characters is required")
        role, expr = cfg.get("assignee_role"), cfg.get("assignee_expr")
        if (role is None) == (expr is None):
            self.err("process-assignee", c, "give exactly one of assignee_role and assignee_expr")
        if role is not None and role not in self.roles:
            self.err("unknown-role", f"{c}/assignee_role", f"unknown role '{role}'")
        if expr is not None:
            self.expr(expr, f"{c}/assignee_expr", STRING)
        outcomes = cfg.get("outcomes", ["done"])
        if (not isinstance(outcomes, list) or not 1 <= len(outcomes) <= 6 or len(set(map(str, outcomes))) != len(outcomes)
                or not all(isinstance(o, str) and o[:1].isalpha() and o == o.lower() and o.replace("_", "").isalnum() for o in outcomes)):
            self.err("process-config", f"{c}/outcomes", "outcomes are 1-6 distinct lower-case names (letters, digits, _)")
            outcomes = []
        for o in outcomes:
            if not any(e.outcome == o for e in outs):
                self.err("process-edges", w, f"outcome '{o}' has no outgoing edge")
        for e in outs:
            if e.outcome not in outcomes:
                self.err("process-edges", f"edges/{e.id}", f"the edge's outcome '{e.outcome}' is not an outcome of the task")
        form = cfg.get("form", [])
        if not isinstance(form, list) or len(form) > 20:
            self.err("process-config", f"{c}/form", "form must be a list of at most 20 fields")
        else:
            names: set[str] = set()
            for i, f in enumerate(form):
                ok = (isinstance(f, dict) and set(f) <= {"name", "type", "required"} and isinstance(f.get("name"), str)
                      and f["name"][:1].isalpha() and f["name"] == f["name"].lower() and f["name"].replace("_", "").isalnum()
                      and f.get("type", "string") in FORM_TYPES and isinstance(f.get("required", True), bool))
                if not ok:
                    self.err("process-config", f"{c}/form/{i}", "a form field is {name, type: string|integer|decimal|boolean, required}")
                elif f["name"] in names:
                    self.err("process-variable", f"{c}/form/{i}", f"form field '{f['name']}' is repeated")
                else:
                    names.add(f["name"])
        for key in ("due_in_hours", "escalate_after_hours"):
            v = cfg.get(key)
            if v is not None and (not isinstance(v, int) or isinstance(v, bool) or not 1 <= v <= MAX_HOURS):
                self.err("process-config", f"{c}/{key}", f"{key} must be an integer from 1 to {MAX_HOURS}")
        if (cfg.get("escalate_after_hours") is None) != (cfg.get("escalate_to_role") is None):
            self.err("process-escalation", c, "escalate_after_hours and escalate_to_role go together")
        if cfg.get("escalate_to_role") is not None and cfg["escalate_to_role"] not in self.roles:
            self.err("unknown-role", f"{c}/escalate_to_role", f"unknown role '{cfg['escalate_to_role']}'")

    def system_task(self, s, w: str, cfg: dict[str, Any]) -> None:
        c = f"{w}/config"
        target = cfg.get("flow")
        q = qualify(self.mod.key, target) if isinstance(target, str) else ""
        flow = self.flows.get(q)
        if flow is None or flow.kind != "server":
            self.err("process-config", f"{c}/flow", f"'{target}' is not a server flow")
            return
        if q in self.hook_flows:
            self.err("process-config", f"{c}/flow", "an entity hook flow runs on its event and cannot be a system task")
        if self.proc.run_as_role is None:
            self.err("process-run-as", "run_as_role", "a process with system tasks needs run_as_role")
        args = cfg.get("args", {})
        if not isinstance(args, dict):
            self.err("process-config", f"{c}/args", "'args' must map flow parameters to expressions")
            return
        owner = q.split(".", 1)[0]
        for p in flow.params:
            if p.name not in args:
                self.err("process-config", f"{c}/args", f"missing argument '{p.name}'")
                continue
            pt = self.index.types_for(owner).expr_type(FieldDef(name="v", type=p.type, required=True), owner)
            if pt is not None:
                self.expr(args[p.name], f"{c}/args/{p.name}", pt)
        for extra in sorted(set(args) - {p.name for p in flow.params}):
            self.err("process-config", f"{c}/args/{extra}", f"flow '{target}' has no parameter '{extra}'")

    def expr(self, src: Any, where: str, expected: Type) -> None:
        if not isinstance(src, str) or not src.strip():
            self.err("expression", where, "an expression is required")
            return
        try:
            check(src, CheckEnv(self.index.types_for(self.mod.key), None, variables=self.env), expected)
        except ExprError as e:
            self.err("expression", where, f"{e.message} (at {e.pos}) in: {src[:120]}")

    # -- reachability and cycles ------------------------------------------------------------------

    def reachability(self) -> None:
        start = next((s.id for s in self.proc.steps if s.type == "start"), None)
        if start is None:
            return
        seen, stack = set(), [start]
        while stack:
            n = stack.pop()
            if n in seen:
                continue
            seen.add(n)
            stack += [e.target for e in self.out[n] if e.target in self.steps]
        for sid in self.steps:
            if sid not in seen:
                self.err("unreachable-step", f"steps/{sid}", f"step '{sid}' cannot be reached from the start")
        # a cycle that never waits would spin without any person or clock being involved
        cycle = self._cycle({n for n in seen if self.steps[n].type not in WAITING})
        if cycle:
            self.err("process-sync-cycle", "edges", "steps loop back on themselves without a user task or timer: " + " -> ".join(cycle))
        ends = {s.id for s in self.proc.steps if s.type == "end"}
        can_end, changed = set(ends), True
        while changed:
            changed = False
            for sid in seen:
                if sid not in can_end and any(e.target in can_end for e in self.out[sid]):
                    can_end.add(sid)
                    changed = True
        for sid in sorted(seen - can_end):
            self.err("process-dead-end", f"steps/{sid}", f"step '{sid}' can never lead to an end")

    def _cycle(self, nodes: set[str]) -> list[str] | None:
        """Some cycle among ``nodes`` (following only edges between them), or None."""
        state: dict[str, int] = {}
        stack: list[str] = []

        def visit(n: str) -> list[str] | None:
            state[n] = 1
            stack.append(n)
            for e in self.out[n]:
                t = e.target
                if t not in nodes:
                    continue
                if state.get(t) == 1:
                    return [*stack[stack.index(t):], t]
                if t not in state and (found := visit(t)):
                    return found
            stack.pop()
            state[n] = 2
            return None

        for n in sorted(nodes):
            if n not in state and (found := visit(n)):
                return found
        return None
