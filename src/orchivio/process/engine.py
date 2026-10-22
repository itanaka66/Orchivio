"""The process state machine (FR-110): pure, deterministic, no I/O.

An instance is a set of *tokens* travelling along the process graph. ``advance`` runs every ready token until each one
waits (for a person, for a timer) or finishes; everything with side effects (creating a task, running a flow) goes
through ``Hooks`` so the engine can be tested without a database and the service can make those effects atomic with
the state change. All state is plain JSON (``State.to_json``), so an instance survives restarts and is pinned to the
process definition it started with.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from orchivio.expr import CheckEnv, EvalContext, ExprError, UserRef, check, evaluate
from orchivio.expr.ast import BOOL, STRING
from orchivio.model import AppModel, ModelIndex, enum_scope
from orchivio.model.processes import process_env
from orchivio.model.schema import FieldDef, ModuleDef, ProcessDef, ProcessStep, qualify

MAX_STEPS_PER_ADVANCE = 1000
MAX_STEPS_PER_INSTANCE = 10_000


class ProcessError(Exception):
    pass


@dataclass
class Token:
    id: int
    step: str
    status: str = "ready"  # ready | waiting_task | waiting_timer | waiting_message | failed
    wake_at: str | None = None
    task_id: str | None = None
    error: str | None = None


@dataclass
class State:
    variables: dict[str, Any] = field(default_factory=dict)
    tokens: list[Token] = field(default_factory=list)
    arrivals: dict[str, int] = field(default_factory=dict)  # parallel join -> tokens that have arrived
    next_token: int = 1
    steps_run: int = 0
    status: str = "running"  # running | completed | failed | cancelled
    error: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {"variables": self.variables, "tokens": [vars(t) for t in self.tokens], "arrivals": self.arrivals,
                "next_token": self.next_token, "steps_run": self.steps_run, "status": self.status, "error": self.error}

    @staticmethod
    def from_json(d: dict[str, Any]) -> State:
        return State(dict(d["variables"]), [Token(**t) for t in d["tokens"]], dict(d["arrivals"]), d["next_token"],
                     d["steps_run"], d["status"], d.get("error"))

    def token(self, token_id: int) -> Token | None:
        return next((t for t in self.tokens if t.id == token_id), None)

    @property
    def next_wake_at(self) -> str | None:
        wakes = [t.wake_at for t in self.tokens if t.status == "waiting_timer" and t.wake_at]
        return min(wakes) if wakes else None


@dataclass(frozen=True)
class TaskSpec:
    token_id: int
    step_id: str
    title: str
    assignee_role: str | None
    assignee_user: str | None
    outcomes: tuple[str, ...]
    form: tuple[dict[str, Any], ...]
    due_at: datetime | None
    escalate_at: datetime | None
    escalate_to_role: str | None


class Hooks(Protocol):
    def create_task(self, spec: TaskSpec) -> str: ...
    def send_message(self, message_type: str, payload: dict[str, Any], target: str) -> None: ...
    def run_flow(self, flow: str, args: dict[str, Any]) -> str | None:
        """Run the server flow ``module.Flow``; return an error message, or None on success."""
        ...
    def run_process(self, process: str, args: dict[str, Any]) -> str | None:
        """Start a sub-process instance; return an error message, or None on success."""
        ...
    def event(self, type_: str, step_id: str | None, data: dict[str, Any]) -> None: ...


class Machine:
    def __init__(self, model: AppModel, module: ModuleDef, proc: ProcessDef, hooks: Hooks,
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self.model, self.module, self.proc, self.hooks, self.clock = model, module, proc, hooks, clock
        self.index = ModelIndex(model)
        self.steps = {s.id: s for s in proc.steps}
        self.env = process_env(self.index, module, proc)
        self.form_names = [f["name"] for s in proc.steps if s.type == "user_task" for f in s.config.get("form", [])]
        self.enums = enum_scope(self.index, module.key)

    # -- public operations ----------------------------------------------------------------------

    def start(self, args: dict[str, Any], initiator: str) -> State:
        start = next(s for s in self.proc.steps if s.type == "start")
        st = State(variables={**{n: None for n in self.form_names}, **args, "initiator": initiator})
        st.tokens.append(Token(self._new_id(st), start.id))
        self.hooks.event("started", start.id, {"initiator": initiator})
        self.advance(st)
        return st

    def complete_task(self, st: State, token_id: int, outcome: str, values: dict[str, Any], task_id: str | None = None) -> State:
        tok = st.token(token_id)
        if tok is None or tok.status != "waiting_task" or (task_id is not None and tok.task_id != task_id):
            raise ProcessError("this task is not waiting")  # includes a task whose token has since moved on
        step = self.steps[tok.step]
        if outcome not in step.config.get("outcomes", ["done"]):
            raise ProcessError(f"'{outcome}' is not an outcome of this task")
        st.variables.update(values)
        edge = self._edge(tok.step, outcome)
        tok.step, tok.status, tok.task_id = edge.target, "ready", None
        self.advance(st)
        return st

    def fire_timer(self, st: State, token_id: int) -> State:
        tok = st.token(token_id)
        if tok is None or tok.status != "waiting_timer":
            raise ProcessError("this timer is not waiting")
        edge = self._edge(tok.step, None)
        self.hooks.event("timer_fired", tok.step, {})
        tok.step, tok.status, tok.wake_at = edge.target, "ready", None
        self.advance(st)
        return st

    def retry(self, st: State, token_id: int) -> State:
        tok = st.token(token_id)
        if tok is None or tok.status != "failed":
            raise ProcessError("only a failed step can be retried")
        tok.status, tok.error = "ready", None
        st.status, st.error = "running", None
        self.hooks.event("retried", tok.step, {})
        self.advance(st)
        return st

    def signal(self, st: State, message: str, data: dict[str, Any]) -> State:
        """Deliver an external message to any waiting catch_message tokens that match."""
        matched = [
            tok for tok in st.tokens
            if tok.status == "waiting_message"
            and self.steps.get(tok.step) is not None
            and self.steps[tok.step].config.get("message") == message
        ]
        if not matched:
            raise ProcessError(f"no step is waiting for message '{message}'")
        for tok in matched:
            st.variables.update(data)
            edge = self._edge(tok.step, None)
            self.hooks.event("message_received", tok.step, {"message": message})
            tok.step, tok.status = edge.target, "ready"
        self.advance(st)
        return st

    def advance(self, st: State) -> None:
        run = 0
        while True:
            ready = [t for t in st.tokens if t.status == "ready"]
            if not ready:
                break
            for tok in ready:
                run += 1
                st.steps_run += 1
                if run > MAX_STEPS_PER_ADVANCE or st.steps_run > MAX_STEPS_PER_INSTANCE:
                    self._fail(st, tok, "the process ran too many steps without waiting for anyone")
                    self._settle(st)
                    return
                self._exec(st, tok)
        self._settle(st)

    # -- execution ------------------------------------------------------------------------------

    def _exec(self, st: State, tok: Token) -> None:
        step = self.steps[tok.step]
        t = step.type
        try:
            if t == "start":
                self._go(tok, self._edge(step.id, None))
            elif t == "decision":
                truth = bool(self._eval(st, step.config["expr"], BOOL))
                self.hooks.event("decision", step.id, {"result": truth})
                self._go(tok, self._edge(step.id, "true" if truth else "false"))
            elif t == "user_task":
                self._user_task(st, tok, step)
            elif t == "system_task":
                self._system_task(st, tok, step)
            elif t == "parallel_split":
                edges = [e for e in self.proc.edges if e.source == step.id]
                st.tokens.remove(tok)
                for e in edges:
                    st.tokens.append(Token(self._new_id(st), e.target))
            elif t == "parallel_join":
                needed = sum(1 for e in self.proc.edges if e.target == step.id)
                st.tokens.remove(tok)
                st.arrivals[step.id] = st.arrivals.get(step.id, 0) + 1
                if st.arrivals[step.id] >= needed:
                    st.arrivals[step.id] = 0
                    st.tokens.append(Token(self._new_id(st), self._edge(step.id, None).target))
            elif t == "timer":
                wake = self.clock() + timedelta(seconds=int(step.config["wait_seconds"]))
                tok.status, tok.wake_at = "waiting_timer", wake.isoformat()
                self.hooks.event("timer_set", step.id, {"wake_at": tok.wake_at})
            elif t == "call_process":
                self._call_process(st, tok, step)
            elif t == "catch_message":
                tok.status = "waiting_message"
                self.hooks.event("waiting_message", step.id, {"message": step.config.get("message", "")})
            elif t == "send_message":
                cfg = step.config
                payload = {k: self._eval(st, v, None) for k, v in (cfg.get("payload_expr") or {}).items()}
                target = cfg.get("target", "broadcast")
                self.hooks.send_message(cfg["message_type"], payload, target)
                self.hooks.event("message_sent", step.id, {"message_type": cfg["message_type"], "target": target})
                self._go(tok, self._edge(step.id, None))
            elif t == "end":
                st.tokens.remove(tok)
        except (ExprError, ProcessError) as e:
            self._fail(st, tok, getattr(e, "message", str(e)))

    def _user_task(self, st: State, tok: Token, step: ProcessStep) -> None:
        cfg = step.config
        assignee_user = None
        if cfg.get("assignee_expr"):
            value = self._eval(st, cfg["assignee_expr"], STRING)
            value = str(value) if value is not None else ""
            if not 1 <= len(value) <= 64:
                raise ProcessError("the assignee expression did not produce a user id")
            assignee_user = value
        now = self.clock()
        due = now + timedelta(hours=cfg["due_in_hours"]) if cfg.get("due_in_hours") else None
        esc = now + timedelta(hours=cfg["escalate_after_hours"]) if cfg.get("escalate_after_hours") else None
        spec = TaskSpec(tok.id, step.id, str(cfg["title"]), cfg.get("assignee_role"), assignee_user,
                        tuple(cfg.get("outcomes", ["done"])), tuple(cfg.get("form", [])), due, esc, cfg.get("escalate_to_role"))
        tok.task_id = self.hooks.create_task(spec)
        tok.status = "waiting_task"

    def _call_process(self, st: State, tok: Token, step: ProcessStep) -> None:
        q = qualify(self.module.key, step.config["process"])
        args = {name: self._eval(st, src, None) for name, src in (step.config.get("args") or {}).items()}
        error = self.hooks.run_process(q, args)
        self.hooks.event("subprocess_failed" if error else "subprocess_started", step.id, {"process": q, **({"error": error[:300]} if error else {})})
        if error:
            self._fail(st, tok, f"subprocess {q} failed: {error}"[:500])
        else:
            self._go(tok, self._edge(step.id, None))

    def _system_task(self, st: State, tok: Token, step: ProcessStep) -> None:
        q = qualify(self.module.key, step.config["flow"])
        args = {name: self._eval(st, src, None) for name, src in (step.config.get("args") or {}).items()}
        args = {k: (str(v) if isinstance(v, str) else v) for k, v in args.items()}
        error = self.hooks.run_flow(q, args)
        self.hooks.event("system_task_failed" if error else "system_task_ok", step.id, {"flow": q, **({"error": error[:300]} if error else {})})
        if error:
            self._fail(st, tok, f"flow {q} failed: {error}"[:500])
        else:
            self._go(tok, self._edge(step.id, None))

    # -- helpers --------------------------------------------------------------------------------

    def _eval(self, st: State, source: str, expected) -> Any:
        res = check(source, CheckEnv(self.index.types_for(self.module.key), None, variables=self.env), expected)
        user = st.variables.get("initiator")
        ctx = EvalContext(current_user=UserRef(str(user), frozenset()) if user is not None else None, now=self.clock(),
                          enums=self.enums, variables=st.variables)
        return evaluate(res.ast, ctx)

    def _edge(self, step_id: str, outcome: str | None):
        for e in self.proc.edges:
            if e.source == step_id and e.outcome == outcome:
                return e
        raise ProcessError(f"step '{step_id}' has no way forward for outcome {outcome!r}")

    @staticmethod
    def _go(tok: Token, edge) -> None:
        tok.step, tok.status = edge.target, "ready"

    @staticmethod
    def _new_id(st: State) -> int:
        st.next_token += 1
        return st.next_token - 1

    def _fail(self, st: State, tok: Token, message: str) -> None:
        tok.status, tok.error = "failed", message[:500]
        self.hooks.event("step_failed", tok.step, {"error": tok.error})

    def _settle(self, st: State) -> None:
        failed = next((t for t in st.tokens if t.status == "failed"), None)
        if failed is not None:
            st.status, st.error = "failed", f"step '{failed.step}': {failed.error}"
        elif not st.tokens:
            st.status, st.error = "completed", None
            self.hooks.event("completed", None, {})
        else:
            st.status, st.error = "running", None


def resolve_definition(model: AppModel, process: str) -> tuple[ModuleDef, ProcessDef] | None:
    """``module.Key`` (or just ``Key``) -> the process in the model."""
    for m in model.modules:
        for p in m.processes or []:
            if process in (p.key, f"{m.key}.{p.key}"):
                return m, p
    return None


__all__ = ["Hooks", "Machine", "ProcessError", "State", "TaskSpec", "Token", "resolve_definition", "FieldDef"]
