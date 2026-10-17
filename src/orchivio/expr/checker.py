"""Static type checker for expressions.

The checker is independent of the application model: it consumes a ``SchemaTypes``
protocol so that ``orchivio.model`` can depend on ``orchivio.expr`` and not the reverse.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from .ast import (
    BOOL,
    DATETIME,
    DECIMAL,
    DEFAULT_LIMITS,
    INT,
    NULL,
    STRING,
    Binary,
    Call,
    Ident,
    Limits,
    ListLit,
    Literal,
    Member,
    Node,
    Ternary,
    Type,
    Unary,
    Var,
    enum_t,
    list_t,
    ref_t,
)
from .errors import TypeCheckError
from .parser import parse

USER_ENTITY = "system.User"


class SchemaTypes(Protocol):
    """What the checker needs to know about the application model."""

    def field_type(self, entity: str, field: str) -> Type | None: ...

    def enum_values(self, name: str) -> Sequence[str] | None: ...


@dataclass(frozen=True)
class CheckEnv:
    schema: SchemaTypes
    record_entity: str | None = None
    env_vars: Mapping[str, Type] = field(default_factory=dict)
    # Flow variables (parameters and step outputs), visible as bare identifiers.
    variables: Mapping[str, Type] = field(default_factory=dict)
    limits: Limits = DEFAULT_LIMITS


@dataclass(frozen=True)
class CheckResult:
    source: str
    ast: Node
    type: Type
    fields: frozenset[str]
    types: Mapping[Node, Type] = field(repr=False, default_factory=dict)


def compatible(a: Type, b: Type) -> bool:
    """Same value type, ignoring nullability; int and decimal are interchangeable."""
    if a.is_numeric and b.is_numeric:
        return True
    return a.same_shape(b)


def assignable(src: Type, dst: Type) -> bool:
    """Can a value of ``src`` be stored in a slot of type ``dst``?"""
    if src.kind == "null":
        return dst.nullable
    if src.nullable and not dst.nullable:
        return False
    if src.kind == "int" and dst.kind == "decimal":
        return True
    return src.non_null().same_shape(dst.non_null())


class _Checker:
    def __init__(self, env: CheckEnv) -> None:
        self.env = env
        self.fields: set[str] = set()
        self.types: dict[Node, Type] = {}

    def fail(self, msg: str, node: Node) -> TypeCheckError:
        return TypeCheckError(msg, node.pos)

    def check(self, node: Node) -> Type:
        t = self._check(node)
        self.types[node] = t
        return t

    # -- node kinds
    def _check(self, node: Node) -> Type:  # noqa: C901 - one branch per node kind
        match node:
            case Literal(value=v):
                if v is None:
                    return NULL
                if isinstance(v, bool):
                    return BOOL
                if isinstance(v, int):
                    return INT
                if isinstance(v, str):
                    return STRING
                return DECIMAL
            case Var(name=n):
                return self._var(n, node)
            case Ident(name=n):
                return self._ident(n, node)
            case ListLit(items=items):
                return self._list(items, node)
            case Member():
                return self._member(node)
            case Call():
                return self._call(node)
            case Unary(op=op, operand=o):
                t = self.check(o)
                if op == "!":
                    self._need_bool(t, o, "!")
                    return BOOL
                if not t.is_numeric or t.nullable:
                    raise self.fail("unary '-' needs a non-null number", node)
                return t
            case Binary():
                return self._binary(node)
            case Ternary(cond=c, then=a, otherwise=b):
                self._need_bool(self.check(c), c, "condition")
                return self._unify(self.check(a), self.check(b), node)
        raise self.fail(f"unsupported node {type(node).__name__}", node)  # pragma: no cover

    def _var(self, name: str, node: Node) -> Type:
        if name == "now":
            return DATETIME
        if name == "currentUser":
            return ref_t(USER_ENTITY)
        if name == "env":
            return Type("envscope")
        raise self.fail(f"unknown variable ${name}", node)

    def _ident(self, name: str, node: Node) -> Type:
        rec = self.env.record_entity
        if name == "record":
            if rec is None:
                raise self.fail("'record' is not available in this context", node)
            return Type("entity", rec)
        if rec is not None:
            ft = self.env.schema.field_type(rec, name)
            if ft is not None:
                self.fields.add(name)
                return ft
        var = self.env.variables.get(name)
        if var is not None:
            return var
        if self.env.schema.enum_values(name) is not None:
            return Type("enumtype", name)
        raise self.fail(f"unknown identifier '{name}'", node)

    def _list(self, items: tuple[Node, ...], node: Node) -> Type:
        if not items:
            raise self.fail("empty list literals are not supported", node)
        elem = self.check(items[0])
        for it in items[1:]:
            elem = self._unify(elem, self.check(it), it)
        if elem.kind == "null" or elem.nullable:
            raise self.fail("list elements cannot be null", node)
        return list_t(elem)

    def _member(self, node: Member) -> Type:
        ot = self.check(node.obj)
        if ot.nullable and not node.safe and ot.kind not in ("enumtype", "envscope"):
            raise self.fail(f"'{node.name}' accessed on a nullable value; use '?.'", node)
        if ot.kind == "entity":
            assert ot.name is not None
            ft = self.env.schema.field_type(ot.name, node.name)
            if ft is None:
                raise self.fail(f"entity {ot.name} has no usable field '{node.name}'", node)
            if isinstance(node.obj, Ident) and node.obj.name == "record":
                self.fields.add(node.name)
            return ft
        if ot.kind == "enumtype":
            assert ot.name is not None
            values = self.env.schema.enum_values(ot.name) or ()
            if node.name not in values:
                raise self.fail(f"enum {ot.name} has no value '{node.name}'", node)
            return enum_t(ot.name)
        if ot.kind == "envscope":
            t = self.env.env_vars.get(node.name)
            if t is None:
                raise self.fail(f"unknown environment constant '{node.name}'", node)
            return t
        if ot.kind == "ref" and node.name == "id":
            return ot  # '.id' on a ref is the ref itself
        if ot.kind == "ref":
            raise self.fail("relationship traversal is not supported in this version", node)
        raise self.fail(f"type {ot} has no members", node)

    def _call(self, node: Call) -> Type:
        args = [self.check(a) for a in node.args]
        if node.obj is None:
            if node.name == "size" and len(args) == 1 and args[0].kind in ("string", "list"):
                return INT
            raise self.fail(f"unknown function '{node.name}'", node)
        ot = self.check(node.obj)
        if ot.nullable and ot.kind != "null":
            raise self.fail(f"method '{node.name}' called on a nullable value", node)
        n = node.name
        if ot.kind == "string":
            if n in ("startsWith", "endsWith", "contains") and [a.kind for a in args] == ["string"]:
                return BOOL
            if n in ("lower", "upper") and not args:
                return STRING
            if n == "size" and not args:
                return INT
        elif ot.kind == "list":
            if n == "size" and not args:
                return INT
            if n == "contains" and len(args) == 1 and ot.elem and compatible(ot.elem, args[0]):
                return BOOL
        elif ot.kind == "ref" and ot.name == USER_ENTITY:
            if n == "hasRole" and [a.kind for a in args] == ["string"]:
                return BOOL
        raise self.fail(f"type {ot} has no method '{n}' with those arguments", node)

    def _need_bool(self, t: Type, node: Node, what: str) -> None:
        if t.kind != "bool" or t.nullable:
            raise self.fail(f"{what} needs a non-null boolean, got {t}", node)

    def _unify(self, a: Type, b: Type, node: Node) -> Type:
        if a.kind == "null" and b.kind == "null":
            return NULL
        if a.kind == "null":
            return b.as_nullable()
        if b.kind == "null":
            return a.as_nullable()
        if a.is_numeric and b.is_numeric:
            kind = DECIMAL if "decimal" in (a.kind, b.kind) else INT
            return Type(kind.kind, nullable=a.nullable or b.nullable)
        if a.same_shape(b):
            return Type(a.kind, a.name, a.elem, a.nullable or b.nullable)
        raise self.fail(f"incompatible types {a} and {b}", node)

    def _binary(self, node: Binary) -> Type:  # noqa: C901 - one branch per operator class
        op = node.op
        lt, rt = self.check(node.left), self.check(node.right)
        if op in ("&&", "||"):
            self._need_bool(lt, node.left, op)
            self._need_bool(rt, node.right, op)
            return BOOL
        if op in ("==", "!="):
            if lt.kind != "null" and rt.kind != "null" and not compatible(lt, rt):
                raise self.fail(f"cannot compare {lt} with {rt}", node)
            return BOOL
        if op in ("<", "<=", ">", ">="):
            if lt.nullable or rt.nullable:
                raise self.fail("ordering a nullable value; use '??' or a null check", node)
            ok = (lt.is_numeric and rt.is_numeric) or (
                lt.kind == rt.kind and lt.kind in ("string", "datetime")
            )
            if not ok:
                raise self.fail(f"cannot order {lt} and {rt}", node)
            return BOOL
        if op == "in":
            if rt.kind != "list" or rt.elem is None:
                raise self.fail("right side of 'in' must be a list", node)
            if lt.nullable or not compatible(lt, rt.elem):
                raise self.fail(f"cannot test {lt} against list of {rt.elem}", node)
            return BOOL
        if op == "??":
            if not lt.nullable:
                raise self.fail("left side of '??' is never null", node)
            return self._unify(lt.non_null(), rt, node)
        if lt.nullable or rt.nullable:
            raise self.fail(f"arithmetic on a nullable value; use '??' ({op})", node)
        if op == "+" and lt.kind == "string" and rt.kind == "string":
            return STRING
        if not (lt.is_numeric and rt.is_numeric):
            raise self.fail(f"operator '{op}' needs numbers, got {lt} and {rt}", node)
        if op == "%":
            if lt.kind != "int" or rt.kind != "int":
                raise self.fail("'%' needs integers", node)
            return INT
        return INT if lt.kind == rt.kind == "int" else DECIMAL


def check(source: str, env: CheckEnv, expected: Type | None = None) -> CheckResult:
    """Parse and type-check ``source``. Raises an ``ExprError`` subclass on failure."""
    ast = parse(source, env.limits)
    c = _Checker(env)
    t = c.check(ast)
    if t.kind in ("entity", "enumtype", "envscope"):
        raise TypeCheckError(f"expression must evaluate to a value, not {t.kind}", ast.pos)
    if expected is not None and not assignable(t, expected):
        raise TypeCheckError(f"expected {expected}, got {t}", ast.pos)
    return CheckResult(source, ast, t, frozenset(c.fields), c.types)
