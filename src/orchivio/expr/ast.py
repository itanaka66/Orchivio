"""AST nodes, static types, and execution limits for the expression language."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class Limits:
    """Deterministic bounds (EXPRESSION-SPEC.md section 6). No wall-clock timers."""

    max_source_len: int = 2000
    max_depth: int = 32
    max_nodes: int = 500
    max_steps: int = 10_000
    max_list_size: int = 1_000
    max_string_len: int = 100_000


DEFAULT_LIMITS = Limits()


# --- nodes ----------------------------------------------------------------


@dataclass(frozen=True)
class Literal:
    value: None | bool | int | Decimal | str
    pos: int = 0


@dataclass(frozen=True)
class Var:
    """``$name`` context variable (without the ``$``)."""

    name: str
    pos: int = 0


@dataclass(frozen=True)
class Ident:
    name: str
    pos: int = 0


@dataclass(frozen=True)
class ListLit:
    items: tuple[Node, ...]
    pos: int = 0


@dataclass(frozen=True)
class Member:
    obj: Node
    name: str
    safe: bool = False
    pos: int = 0


@dataclass(frozen=True)
class Call:
    obj: Node | None
    name: str
    args: tuple[Node, ...]
    pos: int = 0


@dataclass(frozen=True)
class Unary:
    op: str
    operand: Node
    pos: int = 0


@dataclass(frozen=True)
class Binary:
    op: str
    left: Node
    right: Node
    pos: int = 0


@dataclass(frozen=True)
class Ternary:
    cond: Node
    then: Node
    otherwise: Node
    pos: int = 0


Node = Literal | Var | Ident | ListLit | Member | Call | Unary | Binary | Ternary


# --- types ----------------------------------------------------------------


@dataclass(frozen=True)
class Type:
    """Static type. ``kind`` is one of:

    int, decimal, string, bool, datetime, null, enum, ref, list  (value types)
    entity, enumtype, envscope                                   (namespace-like)
    """

    kind: str
    name: str | None = None
    elem: Type | None = None
    nullable: bool = False

    def non_null(self) -> Type:
        return Type(self.kind, self.name, self.elem, False)

    def as_nullable(self) -> Type:
        return Type(self.kind, self.name, self.elem, True)

    def same_shape(self, other: Type) -> bool:
        return (self.kind, self.name, self.elem) == (other.kind, other.name, other.elem)

    @property
    def is_numeric(self) -> bool:
        return self.kind in ("int", "decimal")

    def __str__(self) -> str:
        base = {
            "enum": f"enum:{self.name}",
            "ref": f"ref:{self.name}",
            "list": f"list<{self.elem}>",
        }.get(self.kind, self.kind)
        return base + ("?" if self.nullable and self.kind != "null" else "")


INT = Type("int")
DECIMAL = Type("decimal")
STRING = Type("string")
BOOL = Type("bool")
DATETIME = Type("datetime")
NULL = Type("null", nullable=True)


def enum_t(name: str, nullable: bool = False) -> Type:
    return Type("enum", name, None, nullable)


def ref_t(name: str, nullable: bool = False) -> Type:
    return Type("ref", name, None, nullable)


def list_t(elem: Type) -> Type:
    return Type("list", None, elem.non_null())

