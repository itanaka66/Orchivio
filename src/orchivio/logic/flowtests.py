"""Declarative flow tests (FR-058): fixtures in, run the flow, assert on outcome, path and data.

Each test gets its own throw-away runtime, so tests cannot affect each other. Fixtures are
inserted directly (like synthetic data); the flow itself runs under the test user's real
application security.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import Engine, select

from orchivio.expr import CheckEnv, EvalContext, ExprError, UserRef, check, evaluate
from orchivio.expr.ast import BOOL
from orchivio.model import AppModel
from orchivio.model.schema import FlowDef, FlowTestCase
from orchivio.ports import MockIntegrations
from orchivio.runtime import AppUser, DataError, PreviewStore

from .engine import FlowResult, FlowRunner, Limits
from .hooks import FlowHooks


@dataclass
class FlowTestOutcome:
    name: str
    passed: bool
    messages: list[str] = field(default_factory=list)
    result: FlowResult | None = None


def run_flow_test(model: AppModel, model_hash: str, engine: Engine, flow_key: str, case: FlowTestCase,
                  limits: Limits = Limits()) -> FlowTestOutcome:
    """Run one test. ``flow_key`` is the qualified key (``module.Flow``)."""
    app_id = uuid.uuid4()
    store = PreviewStore(engine)
    schema, data = store.reset(app_id, model, model_hash, hooks=FlowHooks(model, limits))
    try:
        for entity, rows in case.fixtures.items():
            q = schema.index.resolve_entity(flow_key.split(".", 1)[0], entity)
            for row in rows:
                try:
                    data.seed(q or entity, row)
                except DataError as e:
                    return FlowTestOutcome(case.name, False, [f"fixture for {entity} was rejected: {e.message} {e.details or ''}".strip()])
        roles = frozenset(case.user.roles if case.user.roles is not None else model.security.roles)
        user = AppUser(case.user.id, roles)
        data.integrations = MockIntegrations(case.mocks)
        result = FlowRunner(model, data, limits=limits).run(flow_key, case.args, user)
        messages = _assert(case, result, data, user, flow_key)
        return FlowTestOutcome(case.name, not messages, messages, result)
    finally:
        store.drop(app_id)


def _assert(case: FlowTestCase, result: FlowResult, data, user: AppUser, flow_key: str) -> list[str]:
    exp, msgs = case.expect, []
    if result.status != exp.status:
        msgs.append(f"expected the flow to {exp.status[:-2] if exp.status == 'succeeded' else 'fail'} "
                    f"but it {result.status}" + (f": {result.error}" if result.error else ""))
    if exp.error_contains and exp.error_contains.lower() not in (result.error or "").lower():
        msgs.append(f"expected the error to contain {exp.error_contains!r}, got {result.error!r}")
    ran = {s.step for s in result.trace}
    msgs += [f"step '{s}' should have run but did not" for s in exp.visited if s not in ran]
    msgs += [f"step '{s}' should not have run but did" for s in exp.not_visited if s in ran]
    module_key = flow_key.split(".", 1)[0]
    for r in exp.records:
        q = data.schema.index.resolve_entity(module_key, r.entity)
        info = data.schema.entities.get(q or "")
        if info is None:
            msgs.append(f"unknown entity {r.entity!r} in a records expectation")
            continue
        with data.engine.connect() as conn:
            rows = [dict(x) for x in conn.execute(select(info.table)).mappings()]
        count = len(rows)
        if r.where:
            try:
                res = check(r.where, CheckEnv(data.schema.index.types_for(info.module.key), q), BOOL)
                enums = {e.name: tuple(e.values) for e in info.module.enumerations}
                ctx_user = UserRef(user.id, user.roles)
                count = sum(
                    1 for row in rows
                    if evaluate(res.ast, EvalContext(record=data.eval_record(q, row, user), current_user=ctx_user,
                                                     now=data.clock(), enums=enums)))
            except ExprError as e:
                msgs.append(f"cannot evaluate {r.where!r}: {e.message}")
                continue
        if count != r.count:
            msgs.append(f"expected {r.count} {r.entity} record(s){f' where {r.where}' if r.where else ''}, found {count}")
    return msgs


def tests_of(model: AppModel) -> list[tuple[str, FlowDef, int, FlowTestCase]]:
    """Every flow test in the model as (qualified flow key, flow, index, test)."""
    return [(f"{m.key}.{f.key}", f, i, t) for m in model.modules for f in m.flows for i, t in enumerate(f.tests)]
