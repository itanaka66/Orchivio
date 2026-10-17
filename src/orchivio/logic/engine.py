"""Logic Engine: executes validated Server Flows (FR-052, DESIGN.md sections 6.4 and 8.6).

The engine interprets the flow graph; it never runs generated code. A run is one database
transaction under the *caller's* application security: every data step goes through ``AppData``,
so roles, row constraints, validation and hooks apply, and any failure rolls everything back.
Execution is bounded by a step budget, a wall-clock limit, a call-depth limit and per-loop caps.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from orchivio.expr import (
    CheckEnv,
    CheckResult,
    EvalContext,
    ExprError,
    NotCompilable,
    SqlContext,
    Type,
    UserRef,
    check,
    compile_predicate,
    evaluate,
)
from orchivio.expr.ast import BOOL, list_t
from orchivio.model import AppModel
from orchivio.model.schema import FieldDef, FlowDef, FlowStep, ModuleDef, qualify
from orchivio.runtime import AppData, AppUser, DataError, HookFailed
from orchivio.runtime.service import coerce_value, to_eval

END = "<end>"  # the flow finished (end step)
RETURN = "<return>"  # a path ran out of steps (a loop body returns to its loop)


@dataclass(frozen=True)
class Limits:
    max_steps: int = 10_000
    max_call_depth: int = 10
    max_seconds: float = 30.0


class FlowError(Exception):
    """The flow stopped (raise_error, a failed step, or a limit). The transaction is rolled back."""

    def __init__(self, message: str, step: str | None = None, flow: str | None = None) -> None:
        super().__init__(message)
        self.message, self.step, self.flow = message, step, flow


@dataclass
class Span:
    flow: str
    step: str
    type: str
    status: str  # ok | error
    ms: float
    iteration: list[int] = field(default_factory=list)
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"flow": self.flow, "step": self.step, "type": self.type, "status": self.status, "ms": self.ms,
                "iteration": self.iteration, "detail": self.detail}


@dataclass
class FlowResult:
    flow: str
    status: str  # succeeded | failed
    error: str | None = None
    error_step: str | None = None
    steps_executed: int = 0
    trace: list[Span] = field(default_factory=list)

    @property
    def visited(self) -> set[str]:
        return {s.step for s in self.trace if s.status == "ok"} | {s.step for s in self.trace}

    def as_dict(self) -> dict[str, Any]:
        return {"flow": self.flow, "status": self.status, "error": self.error, "error_step": self.error_step,
                "steps_executed": self.steps_executed, "trace": [s.as_dict() for s in self.trace]}


@dataclass
class _Run:
    user: AppUser | None
    limits: Limits
    started: float = field(default_factory=time.monotonic)
    steps: int = 0
    trace: list[Span] = field(default_factory=list)

    def tick(self, flow: str, step: str) -> None:
        self.steps += 1
        if self.steps > self.limits.max_steps:
            raise FlowError(f"the flow executed more than {self.limits.max_steps} steps", step, flow)
        if time.monotonic() - self.started > self.limits.max_seconds:
            raise FlowError(f"the flow ran longer than {self.limits.max_seconds:g} seconds", step, flow)


@dataclass
class _Scope:
    flow: FlowDef
    module: ModuleDef
    qualified: str
    values: dict[str, Any]
    types: dict[str, Type]
    out: dict[tuple[str, str], str]
    steps: dict[str, FlowStep]
    iteration: list[int] = field(default_factory=list)


class FlowRunner:
    def __init__(self, model: AppModel, data: AppData, *, limits: Limits = Limits(),
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self.model, self.data, self.limits, self.clock = model, data, limits, clock
        self.schema = data.schema
        self.flows: dict[str, tuple[ModuleDef, FlowDef]] = {
            f"{m.key}.{f.key}": (m, f) for m in model.modules for f in m.flows
        }

    # -- public ---------------------------------------------------------------------------

    def run(self, flow: str, args: Mapping[str, Any], user: AppUser | None) -> FlowResult:
        """Run a flow atomically. Returns a result in every case; a failed run changed nothing."""
        mod, fl = self._find(flow, None)
        run = _Run(user, self.limits)
        result = FlowResult(f"{mod.key}.{fl.key}", "succeeded")
        try:
            with self.data.transaction():
                self._invoke(run, mod, fl, dict(args), user, depth=0)
        except FlowError as e:
            result.status, result.error, result.error_step = "failed", e.message, e.step
        except DataError as e:
            result.status, result.error = "failed", f"{e.message}" + (f" {e.details}" if e.details else "")
            result.error_step = run.trace[-1].step if run.trace and run.trace[-1].status == "error" else None
        result.steps_executed, result.trace = run.steps, run.trace
        return result

    def run_hook(self, flow: str, user: AppUser | None, record: dict[str, Any], previous: dict[str, Any] | None,
                 entity: str) -> None:
        """Run a hook flow inside the caller's open transaction; failures raise ``HookFailed``."""
        mod, fl = self._find(flow, None)
        run = _Run(user, self.limits)
        try:
            et = Type("entity", entity)
            self._invoke(run, mod, fl, {}, user, depth=0, implicit={"record": record, "previous": previous},
                         implicit_types={"record": et, "previous": et.as_nullable()})
        except FlowError as e:
            raise HookFailed(e.message) from e

    # -- internals ------------------------------------------------------------------------

    def _find(self, key: str, relative_to: ModuleDef | None) -> tuple[ModuleDef, FlowDef]:
        q = qualify(relative_to.key, key) if relative_to else (key if "." in key else f"{self.model.modules[0].key}.{key}")
        found = self.flows.get(q)
        if found is None:
            raise FlowError(f"unknown flow '{key}'")
        return found

    def _type_of(self, mod: ModuleDef, type_str: str) -> Type | None:
        return self.schema.index.types_for(mod.key).expr_type(FieldDef(name="v", type=type_str, required=True), mod.key)

    def _invoke(self, run: _Run, mod: ModuleDef, fl: FlowDef, args: dict[str, Any], user: AppUser | None, *, depth: int,
                implicit: dict[str, Any] | None = None, implicit_types: dict[str, Type] | None = None) -> None:
        if depth > run.limits.max_call_depth:
            raise FlowError(f"flows are called more than {run.limits.max_call_depth} levels deep", None, fl.key)
        values: dict[str, Any] = {}
        types: dict[str, Type] = {}
        unknown = set(args) - {p.name for p in fl.params}
        if unknown:
            raise FlowError(f"unknown argument(s): {', '.join(sorted(unknown))}", None, fl.key)
        for p in fl.params:
            if p.name not in args:
                raise FlowError(f"missing argument '{p.name}'", None, fl.key)
            try:
                values[p.name] = to_eval(coerce_value(self.schema, mod.key, FieldDef(name=p.name, type=p.type, required=True), args[p.name]))
            except ValueError as e:
                raise FlowError(f"argument '{p.name}' {e}", None, fl.key) from e
            t = self._type_of(mod, p.type)
            if t is not None:
                types[p.name] = t
        for name, v in (implicit or {}).items():
            values[name] = v
        types.update(implicit_types or {})
        scope = _Scope(fl, mod, f"{mod.key}.{fl.key}", values, types,
                       {(e.source, e.handle): e.target for e in fl.edges}, {s.id: s for s in fl.steps})
        start = next(s for s in fl.steps if s.type == "start")
        self._walk(run, scope, start.id, depth)

    def _walk(self, run: _Run, sc: _Scope, step_id: str | None, depth: int) -> str:
        while step_id is not None:
            step = sc.steps[step_id]
            run.tick(sc.qualified, step.id)
            t0 = time.perf_counter()
            span = Span(sc.qualified, step.id, step.type, "ok", 0.0, list(sc.iteration))
            run.trace.append(span)
            try:
                nxt = self._step(run, sc, step, span, depth)
            except FlowError:
                span.status = "error"
                raise
            except (DataError, ExprError) as e:
                span.status = "error"
                span.detail = e.message if hasattr(e, "message") else str(e)
                if isinstance(e, ExprError):
                    raise FlowError(e.message, step.id, sc.qualified) from e
                raise
            finally:
                span.ms = round((time.perf_counter() - t0) * 1000, 3)
            if nxt == END:
                return END
            step_id = nxt
        return RETURN

    # -- expressions ----------------------------------------------------------------------

    def _check(self, sc: _Scope, source: str, record_entity: str | None = None, expected: Type | None = None) -> CheckResult:
        return check(source, CheckEnv(self.schema.index.types_for(sc.module.key), record_entity, variables=sc.types), expected)

    def _eval(self, run: _Run, sc: _Scope, res: CheckResult) -> Any:
        user = run.user
        ctx = EvalContext(
            current_user=UserRef(user.id, user.roles) if user else None, now=self.clock(),
            enums={e.name: tuple(e.values) for e in sc.module.enumerations}, variables=sc.values)
        return evaluate(res.ast, ctx)

    def _values(self, run: _Run, sc: _Scope, step: FlowStep, mapping: Any) -> dict[str, Any]:
        out = {}
        for name, source in (mapping or {}).items():
            v = self._eval(run, sc, self._check(sc, source))
            out[name] = str(v) if isinstance(v, str) else v  # UserRef -> plain str
        return out

    # -- steps ----------------------------------------------------------------------------

    def _next(self, sc: _Scope, step: FlowStep, handle: str = "out") -> str | None:
        return sc.out.get((step.id, handle))

    def _entity(self, sc: _Scope, step: FlowStep, name: Any) -> str:
        q = self.schema.index.resolve_entity(sc.module.key, str(name))
        if q is None or q not in self.schema.entities:
            raise FlowError(f"unknown entity '{name}'", step.id, sc.qualified)
        return q

    def _step(self, run: _Run, sc: _Scope, step: FlowStep, span: Span, depth: int) -> str | None:  # noqa: C901
        cfg, t = step.config, step.type
        user = run.user
        if t == "start":
            return self._next(sc, step)
        if t == "end":
            return END
        if t == "raise_error":
            raise FlowError(str(cfg.get("message", "")), step.id, sc.qualified)
        if t == "decision":
            truth = bool(self._eval(run, sc, self._check(sc, cfg["expr"], expected=BOOL)))
            span.detail = str(truth).lower()
            return self._next(sc, step, "true" if truth else "false")
        if t in ("retrieve", "create", "change", "delete"):
            if user is None and t != "retrieve":
                raise FlowError("this flow writes data and needs a user", step.id, sc.qualified)
            return self._data_step(run, sc, step, span)
        if t == "loop":
            return self._loop(run, sc, step, span, depth)
        if t == "call_flow":
            mod, callee = self._find(str(cfg["flow"]), sc.module)
            args = self._values(run, sc, step, cfg.get("args"))
            span.detail = f"{mod.key}.{callee.key}"
            self._invoke(run, mod, callee, args, user, depth=depth + 1)
            return self._next(sc, step)
        raise FlowError(f"unsupported step type '{t}'", step.id, sc.qualified)

    def _data_step(self, run: _Run, sc: _Scope, step: FlowStep, span: Span) -> str | None:
        cfg, t, user = step.config, step.type, run.user
        assert user is not None or t == "retrieve"
        actor = user or AppUser("system", frozenset())
        if t == "retrieve":
            q = self._entity(sc, step, cfg.get("entity"))
            many = bool(cfg.get("many", False))
            where = None
            if cfg.get("where"):
                res = self._check(sc, cfg["where"], record_entity=q, expected=BOOL)
                visible = self.data.readable_fields(q, actor)
                if visible is not None and not res.fields <= visible:
                    raise FlowError("this condition uses fields your roles may not read: "
                                    + ", ".join(sorted(res.fields - visible)), step.id, sc.qualified)
                try:
                    pred = compile_predicate(res, SqlContext(current_user=UserRef(actor.id, actor.roles), now=self.clock(),
                                                             dialect=self.data.engine.dialect.name, variables=sc.values))
                except NotCompilable as e:
                    raise FlowError(f"this condition cannot be run as a query: {e.message}", step.id, sc.qualified) from e
                where = (pred.sql, pred.params)
            rows = self.data.select_rows(q, actor, where=where, limit=int(cfg.get("limit", 100)) if many else 1)
            records = [self.data.eval_record(q, r, actor) for r in rows]
            name = str(cfg["into"])
            sc.values[name] = records if many else (records[0] if records else None)
            et = Type("entity", q)
            sc.types[name] = list_t(et) if many else et.as_nullable()
            span.detail = f"{len(records)} record(s)"
            return self._next(sc, step)
        if t == "create":
            q = self._entity(sc, step, cfg.get("entity"))
            row = self.data.create_record(q, actor, self._values(run, sc, step, cfg.get("values")))
            span.detail = f"created {q}"
            if cfg.get("into"):
                name = str(cfg["into"])
                sc.values[name] = self.data.eval_record(q, row, actor)
                sc.types[name] = Type("entity", q)
            return self._next(sc, step)
        # change / delete act on a record held in a variable
        name = str(cfg.get("target"))
        held = sc.values.get(name)
        vt = sc.types.get(name)
        if held is None or vt is None or vt.kind != "entity" or vt.name is None:
            raise FlowError(f"variable '{name}' does not hold a record", step.id, sc.qualified)
        info = self.schema.entities[vt.name]
        pk = held[info.pk.name]
        if t == "change":
            row = self.data.update_record(vt.name, actor, pk, self._values(run, sc, step, cfg.get("values")))
            sc.values[name] = self.data.eval_record(vt.name, row, actor)
            span.detail = f"updated {vt.name}"
        else:
            self.data.delete(vt.name, actor, pk)
            sc.values[name] = None
            span.detail = f"deleted {vt.name}"
        return self._next(sc, step)

    def _loop(self, run: _Run, sc: _Scope, step: FlowStep, span: Span, depth: int) -> str | None:
        cfg = step.config
        over, item = str(cfg["over"]), str(cfg["item"])
        items = sc.values.get(over)
        vt = sc.types.get(over)
        if not isinstance(items, list) or vt is None or vt.elem is None:
            raise FlowError(f"variable '{over}' is not a list of records", step.id, sc.qualified)
        cap = int(cfg["max_iterations"])
        if len(items) > cap:
            raise FlowError(f"the loop would run {len(items)} times but is limited to {cap}", step.id, sc.qualified)
        span.detail = f"{len(items)} item(s)"
        body = self._next(sc, step, "body")
        sc.types[item] = vt.elem
        for i, rec in enumerate(items):
            sc.values[item] = rec
            sc.iteration.append(i)
            try:
                if self._walk(run, sc, body, depth) == END:
                    return END
            finally:
                sc.iteration.pop()
        sc.values.pop(item, None)
        sc.types.pop(item, None)
        return self._next(sc, step, "done")
