"""Application-plane access resolution (DESIGN.md section 8.7, FR-060..063).

Evaluation order: authenticate -> resolve app roles -> page/action access -> entity
operation access -> row constraint (SQL predicate) -> field filtering -> audit.
This module implements the entity-operation and row-constraint steps.

Union semantics: a user holding several roles gets the union of granted rules; row
constraints from the granting rules are OR-combined. A granting rule without a
constraint makes the operation unrestricted. Everything else is denied.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from orchivio.expr import (
    CheckEnv,
    CheckResult,
    EvalContext,
    SqlContext,
    SqlPredicate,
    check,
    compile_predicate,
    evaluate,
)
from orchivio.expr.ast import BOOL
from orchivio.model import AppModel, ModelIndex
from orchivio.model.schema import ALL_OPERATIONS

DENY_ALL = SqlPredicate("(1 = 0)", {})
ALLOW_ALL = SqlPredicate("(1 = 1)", {})


@dataclass(frozen=True)
class AccessDecision:
    allowed: bool
    unrestricted: bool = False
    constraints: tuple[CheckResult, ...] = ()


DENIED = AccessDecision(False)


def resolve_access(
    model: AppModel, roles: Iterable[str], entity: str, operation: str
) -> AccessDecision:
    """Decide whether any of ``roles`` may perform ``operation`` on qualified ``entity``."""
    if operation not in ALL_OPERATIONS:
        return DENIED
    index = ModelIndex(model)
    found = index.entities.get(entity)
    if found is None:
        return DENIED
    role_set = set(roles)
    granting = [
        r
        for r in model.security.access
        if r.entity == entity and r.role in role_set and operation in r.operations
    ]
    if not granting:
        return DENIED
    if any(r.row_constraint is None for r in granting):
        return AccessDecision(True, True)
    env = CheckEnv(index.types_for(found[0].key), entity)
    # Constraints were type-checked at validation time; re-check to obtain the typed AST.
    return AccessDecision(
        True, False, tuple(check(r.row_constraint, env, BOOL) for r in granting if r.row_constraint)
    )


def resolve_fields(model: AppModel, roles: Iterable[str], entity: str, operation: str) -> frozenset[str] | None:
    """The fields ``roles`` may read (operation ``read``) or write (``create``/``update``) on ``entity``.

    ``None`` means every field. Otherwise the union of the allow-lists of the granting rules: a rule without
    a field list grants everything, so one unrestricted rule makes the whole answer ``None``. Field lists are
    combined across the user's rules regardless of row constraints. Callers must have established that the
    operation itself is allowed (``resolve_access``); with no granting rule the answer is the empty set.
    """
    if operation not in ("read", "create", "update"):
        return None
    role_set = set(roles)
    granting = [r for r in model.security.access if r.entity == entity and r.role in role_set and operation in r.operations]
    allowed: set[str] = set()
    for r in granting:
        names = None if r.fields is None else (r.fields.read if operation == "read" else r.fields.write)
        if names is None:
            return None
        allowed.update(names)
    return frozenset(allowed)


def row_predicate(decision: AccessDecision, ctx: SqlContext) -> SqlPredicate:
    """SQL predicate to AND into every query for this operation.

    Raises ``NotCompilable`` if a constraint cannot be translated; callers must treat
    that as a denial, never as 'no filter'.
    """
    if not decision.allowed:
        return DENY_ALL
    if decision.unrestricted:
        return ALLOW_ALL
    parts: list[str] = []
    params: dict[str, Any] = {}
    for i, res in enumerate(decision.constraints):
        sub = SqlContext(
            current_user=ctx.current_user,
            now=ctx.now,
            env=ctx.env,
            table_alias=ctx.table_alias,
            dialect=ctx.dialect,
            param_prefix=f"{ctx.param_prefix}c{i}_",
        )
        pred = compile_predicate(res, sub)
        parts.append(pred.sql)
        params.update(pred.params)
    return SqlPredicate("(" + " OR ".join(parts) + ")", params)


def record_allowed(decision: AccessDecision, record: Mapping[str, Any], ctx: EvalContext) -> bool:
    """Interpreter-side check for a single record (e.g. before an update or delete)."""
    if not decision.allowed:
        return False
    if decision.unrestricted:
        return True
    ev = EvalContext(
        record=record,
        current_user=ctx.current_user,
        now=ctx.now,
        env=ctx.env,
        enums=ctx.enums,
    )
    return any(bool(evaluate(c.ast, ev)) for c in decision.constraints)
