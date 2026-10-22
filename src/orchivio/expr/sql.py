"""Compile a type-checked boolean expression to a parameterised SQL predicate.

Used for row-level access constraints (FR-063) and list filters. Every value reaches
the database as a bind parameter; identifiers are validated and quoted. Anything that
cannot be translated faithfully raises ``NotCompilable`` and callers must then deny
access rather than fall back to unfiltered queries.

The compiled predicate must agree with the interpreter; tests/unit/test_expr_sql.py
checks this against SQLite.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from .ast import (
    Binary,
    Call,
    Ident,
    ListLit,
    Literal,
    Member,
    Node,
    ObjLit,
    Ternary,
    Unary,
    Var,
)
from .checker import CheckResult
from .errors import NotCompilable
from .interpreter import UserRef

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class SqlPredicate:
    sql: str
    params: dict[str, Any]


@dataclass(frozen=True)
class SqlContext:
    current_user: UserRef | None = None
    now: datetime | None = None
    env: Mapping[str, Any] | None = None
    table_alias: str | None = None
    dialect: str = "postgresql"  # or "sqlite"
    param_prefix: str = "p"
    # Values of flow variables (record variables are mappings, list variables are lists).
    variables: Mapping[str, Any] | None = None


class _Compiler:
    def __init__(self, result: CheckResult, ctx: SqlContext) -> None:
        self.types = result.types
        self.vars = result.variables
        self.ctx = ctx
        self.params: dict[str, Any] = {}

    def param(self, value: Any) -> str:
        name = f"{self.ctx.param_prefix}{len(self.params)}"
        self.params[name] = value
        return f":{name}"

    def var_value(self, name: str) -> Any:
        if self.ctx.variables is None or name not in self.ctx.variables:
            raise NotCompilable(f"variable '{name}' has no value in this context")
        return self.ctx.variables[name]

    def scalar_param(self, value: Any) -> str:
        if isinstance(value, (Mapping, list, tuple, set)):
            raise NotCompilable("only scalar values can be used as query parameters")
        if isinstance(value, Decimal) and self.ctx.dialect == "sqlite":
            value = float(value)  # sqlite3 cannot bind Decimal
        return self.param(value)

    def nullable(self, node: Node) -> bool:
        return self.types[node].nullable

    def column(self, name: str) -> str:
        if not _IDENT_RE.match(name):
            raise NotCompilable(f"unsafe column name {name!r}")
        col = f'"{name}"'
        return f'"{self.ctx.table_alias}".{col}' if self.ctx.table_alias else col

    def c(self, node: Node) -> str:  # noqa: C901 - one branch per node kind
        match node:
            case Literal(value=v):
                return "NULL" if v is None else self.param(v)
            case Var(name="currentUser"):
                if self.ctx.current_user is None:
                    raise NotCompilable("$currentUser unavailable")
                return self.param(str(self.ctx.current_user))
            case Var(name="now"):
                if self.ctx.now is None:
                    raise NotCompilable("$now unavailable")
                return self.param(self.ctx.now)
            case Ident(name=n):
                if n in self.vars:
                    return self.scalar_param(self.var_value(n))
                if self.types[node].kind == "enumtype" or n == "record":
                    raise NotCompilable(f"'{n}' is not a value")
                return self.column(n)
            case Member(obj=o, name=n):
                return self._member(node, o, n)
            case ListLit(items=items) if not items:
                return "(1 = 0)"  # empty list: x in [] is always false
            case ListLit():
                raise NotCompilable("list used outside 'in'")
            case ObjLit():
                raise NotCompilable("object literals cannot be used in SQL predicates")
            case Unary(op=op, operand=o):
                return f"(NOT {self.c(o)})" if op == "!" else f"(-{self.c(o)})"
            case Binary():
                return self._binary(node)
            case Ternary(cond=c, then=a, otherwise=b):
                return f"(CASE WHEN {self.c(c)} THEN {self.c(a)} ELSE {self.c(b)} END)"
            case Call():
                return self._call(node)
        raise NotCompilable(f"unsupported node {type(node).__name__}")

    def _member(self, node: Member, obj: Node, name: str) -> str:
        ot = self.types[obj]
        if ot.kind == "entity":
            if isinstance(obj, Ident) and obj.name in self.vars:  # a field of a record held in a variable
                held = self.var_value(obj.name)
                return self.scalar_param(None if held is None else held.get(name))
            return self.column(name)
        if ot.kind == "enumtype":
            return self.param(name)
        if ot.kind == "envscope":
            if self.ctx.env is None or name not in self.ctx.env:
                raise NotCompilable(f"$env.{name} unavailable")
            return self.param(self.ctx.env[name])
        if ot.kind == "ref":
            return self.c(obj)  # '.id' on a ref is the ref itself
        raise NotCompilable("unsupported member access")

    def _binary(self, node: Binary) -> str:  # noqa: C901 - one branch per operator
        op, left, right = node.op, node.left, node.right
        if op == "in":
            if isinstance(right, Ident) and right.name in self.vars:  # membership in a list variable
                items = self.var_value(right.name)
                if not isinstance(items, (list, tuple)):
                    raise NotCompilable("'in' needs a list")
                if not items:
                    return "(1 = 0)"
                return f"({self.c(left)} IN ({', '.join(self.scalar_param(i) for i in items)}))"
            if not isinstance(right, ListLit):
                raise NotCompilable("'in' requires a list literal")
            if not right.items:
                return "(1 = 0)"  # x in [] is always false
            items = ", ".join(self.c(i) for i in right.items)
            return f"({self.c(left)} IN ({items}))"
        if op in ("&&", "||"):
            return f"({self.c(left)} {'AND' if op == '&&' else 'OR'} {self.c(right)})"
        if op == "??":
            return f"COALESCE({self.c(left)}, {self.c(right)})"
        if op in ("==", "!="):
            return self._equality(node)
        if op == "/":
            return f"({self.c(left)} / NULLIF({self.c(right)}, 0))"
        if op == "%":
            return f"({self.c(left)} % NULLIF({self.c(right)}, 0))"
        if op == "+" and self.types[left].kind == "string":
            return f"({self.c(left)} || {self.c(right)})"
        sql_op = {"<": "<", "<=": "<=", ">": ">", ">=": ">=", "+": "+", "-": "-", "*": "*"}[op]
        return f"({self.c(left)} {sql_op} {self.c(right)})"

    def _equality(self, node: Binary) -> str:
        eq = node.op == "=="
        left, right = node.left, node.right
        for a, b in ((left, right), (right, left)):
            if isinstance(b, Literal) and b.value is None:
                return f"({self.c(a)} IS {'' if eq else 'NOT '}NULL)"
        if not (self.nullable(left) or self.nullable(right)):
            return f"({self.c(left)} {'=' if eq else '<>'} {self.c(right)})"
        # Null-safe comparison (does not use a plain btree index on PostgreSQL;
        # the model linter warns about nullable fields in constraints).
        if self.ctx.dialect == "sqlite":
            return f"({self.c(left)} IS {'' if eq else 'NOT '}{self.c(right)})"
        return f"({self.c(left)} IS {'NOT ' if eq else ''}DISTINCT FROM {self.c(right)})"

    def _call(self, node: Call) -> str:
        if node.obj is None:
            raise NotCompilable(f"function '{node.name}' is not compiled")
        n = node.name
        if n == "hasRole":
            if self.ctx.current_user is None or not isinstance(node.args[0], Literal):
                raise NotCompilable("hasRole needs a literal role and a current user")
            has = node.args[0].value in self.ctx.current_user.roles
            return self.param(has)
        if n in ("lower", "upper"):
            return f"{n}({self.c(node.obj)})"
        if n in ("startsWith", "endsWith", "contains"):
            arg = node.args[0]
            if not isinstance(arg, Literal) or not isinstance(arg.value, str):
                raise NotCompilable(f"{n} needs a literal argument")
            # LIKE is avoided: it is case-insensitive on SQLite and needs escaping.
            col, size, p = self.c(node.obj), len(arg.value), self.param(arg.value)
            if size == 0:
                return "(1 = 1)"
            sqlite = self.ctx.dialect == "sqlite"
            if n == "contains":
                return f"(instr({col}, {p}) > 0)" if sqlite else f"(strpos({col}, {p}) > 0)"
            if n == "startsWith":
                return f"(substr({col}, 1, {size}) = {p})"
            return f"(substr({col}, -{size}) = {p})" if sqlite else f"(right({col}, {size}) = {p})"
        raise NotCompilable(f"method '{n}' is not compiled")


def compile_predicate(result: CheckResult, ctx: SqlContext | None = None) -> SqlPredicate:
    """Compile a boolean ``CheckResult`` to SQL. Raises ``NotCompilable``."""
    if result.type.kind != "bool":
        raise NotCompilable("only boolean expressions can be predicates")
    comp = _Compiler(result, ctx or SqlContext())
    sql = comp.c(result.ast)
    return SqlPredicate(sql, comp.params)
