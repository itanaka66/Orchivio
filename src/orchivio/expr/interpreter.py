"""Deterministic, bounded tree-walking evaluator.

Evaluation performs no I/O. ``$now`` must be supplied by the caller so results are
reproducible. Termination is guaranteed by a step budget (``Limits.max_steps``).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from .ast import (
    DEFAULT_LIMITS,
    Binary,
    Call,
    Ident,
    Limits,
    ListLit,
    Literal,
    Member,
    Node,
    Ternary,
    Unary,
    Var,
)
from .errors import EvalError, LimitExceeded


class UserRef(str):
    """The current end user: a string id that also carries the user's app roles."""

    roles: frozenset[str]

    def __new__(cls, user_id: str, roles: frozenset[str] | set[str] | list[str] = frozenset()):
        obj = super().__new__(cls, user_id)
        obj.roles = frozenset(roles)
        return obj


@dataclass(frozen=True)
class EnumScope:
    name: str


@dataclass(frozen=True)
class EvalContext:
    record: Mapping[str, Any] | None = None
    current_user: UserRef | None = None
    now: datetime | None = None
    env: Mapping[str, Any] = field(default_factory=dict)
    enums: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    variables: Mapping[str, Any] = field(default_factory=dict)


def _norm(v: Any) -> Any:
    """Normalise host values: floats become Decimals so arithmetic stays exact."""
    return Decimal(str(v)) if isinstance(v, float) else v


class _Evaluator:
    def __init__(self, ctx: EvalContext, limits: Limits) -> None:
        self.ctx = ctx
        self.limits = limits
        self.steps = 0

    def err(self, msg: str, node: Node) -> EvalError:
        return EvalError(msg, node.pos)

    def ev(self, node: Node) -> Any:  # noqa: C901 - one branch per node kind
        self.steps += 1
        if self.steps > self.limits.max_steps:
            raise LimitExceeded("evaluation step budget exhausted", node.pos)
        match node:
            case Literal(value=v):
                return v
            case Var(name=n):
                return self._var(n, node)
            case Ident(name=n):
                return self._ident(n, node)
            case ListLit(items=items):
                return [self.ev(i) for i in items]
            case Member(obj=o, name=n, safe=safe):
                obj = self.ev(o)
                if obj is None:
                    if safe:
                        return None
                    raise self.err(f"'{n}' accessed on null", node)
                return self._member(obj, n, node)
            case Call():
                return self._call(node)
            case Unary(op=op, operand=o):
                v = self.ev(o)
                return (not v) if op == "!" else -v
            case Binary():
                return self._binary(node)
            case Ternary(cond=c, then=a, otherwise=b):
                return self.ev(a) if self.ev(c) else self.ev(b)
        raise self.err(f"unsupported node {type(node).__name__}", node)  # pragma: no cover

    def _var(self, name: str, node: Node) -> Any:
        if name == "now":
            if self.ctx.now is None:
                raise self.err("$now was not provided", node)
            return self.ctx.now
        if name == "currentUser":
            if self.ctx.current_user is None:
                raise self.err("no current user in this context", node)
            return self.ctx.current_user
        if name == "env":
            return self.ctx.env
        raise self.err(f"unknown variable ${name}", node)

    def _ident(self, name: str, node: Node) -> Any:
        rec = self.ctx.record
        if name == "record":
            if rec is None:
                raise self.err("no record in this context", node)
            return rec
        if rec is not None and name in rec:
            return _norm(rec[name])
        if name in self.ctx.variables:
            return _norm(self.ctx.variables[name])
        if name in self.ctx.enums:
            return EnumScope(name)
        raise self.err(f"unknown identifier '{name}'", node)

    def _member(self, obj: Any, name: str, node: Node) -> Any:
        if isinstance(obj, EnumScope):
            return name
        if isinstance(obj, UserRef):
            return str(obj)  # only '.id' passes the type checker
        if isinstance(obj, Mapping):
            if name not in obj:
                raise self.err(f"no field '{name}'", node)
            return _norm(obj[name])
        raise self.err(f"cannot read '{name}'", node)

    def _call(self, node: Call) -> Any:  # noqa: C901 - small method table
        args = [self.ev(a) for a in node.args]
        if node.obj is None:
            return len(args[0])  # only size() passes the type checker
        o = self.ev(node.obj)
        n = node.name
        if o is None:
            raise self.err(f"'{n}' called on null", node)
        if n == "size":
            return len(o)
        if n == "startsWith":
            return o.startswith(args[0])
        if n == "endsWith":
            return o.endswith(args[0])
        if n == "contains":
            return args[0] in o
        if n == "lower":
            return o.lower()
        if n == "upper":
            return o.upper()
        if n == "hasRole":
            return args[0] in o.roles
        raise self.err(f"unknown method '{n}'", node)  # pragma: no cover

    def _binary(self, node: Binary) -> Any:  # noqa: C901 - one branch per operator
        op = node.op
        if op == "&&":
            return bool(self.ev(node.left)) and bool(self.ev(node.right))
        if op == "||":
            return bool(self.ev(node.left)) or bool(self.ev(node.right))
        if op == "??":
            left = self.ev(node.left)
            return left if left is not None else self.ev(node.right)
        a, b = self.ev(node.left), self.ev(node.right)
        if op == "==":
            return a == b
        if op == "!=":
            return a != b
        if op == "in":
            return a in b
        if a is None or b is None:
            raise self.err(f"operator '{op}' applied to null", node)
        if op == "<":
            return a < b
        if op == "<=":
            return a <= b
        if op == ">":
            return a > b
        if op == ">=":
            return a >= b
        if op == "+":
            r = a + b
            if isinstance(r, str) and len(r) > self.limits.max_string_len:
                raise LimitExceeded("string result too large", node.pos)
            return r
        if op == "-":
            return a - b
        if op == "*":
            return a * b
        return self._div(op, a, b, node)

    def _div(self, op: str, a: Any, b: Any, node: Node) -> Any:
        if b == 0:
            raise self.err("division by zero", node)
        if isinstance(a, int) and isinstance(b, int):
            # truncate toward zero (matches SQL and CEL), unlike Python's floor division
            q = abs(a) // abs(b)
            q = q if (a >= 0) == (b >= 0) else -q
            return q if op == "/" else a - b * q
        return Decimal(a) / Decimal(b)


def evaluate(node: Node, ctx: EvalContext, limits: Limits = DEFAULT_LIMITS) -> Any:
    """Evaluate a type-checked AST."""
    return _Evaluator(ctx, limits).ev(node)
