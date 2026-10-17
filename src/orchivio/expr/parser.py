"""Lexer and recursive-descent parser for the Orchivio expression language."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

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
from .errors import LimitExceeded, ParseError

_TOKEN_RE = re.compile(
    r"""
    (?P<ws>\s+)
  | (?P<num>\d+(?:\.\d+)?)
  | (?P<str>'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*")
  | (?P<var>\$[A-Za-z_][A-Za-z0-9_]*)
  | (?P<id>[A-Za-z_][A-Za-z0-9_]*)
  | (?P<op>\?\?|\?\.|&&|\|\||==|!=|<=|>=|[-+*/%<>!?:.,()\[\]])
    """,
    re.VERBOSE,
)
_KEYWORDS = {"true", "false", "null", "in"}
_ESCAPES = {"n": "\n", "t": "\t", "\\": "\\", "'": "'", '"': '"'}

# Binary operator precedence (higher binds tighter); ternary is lowest.
_PREC = {
    "??": 1,
    "||": 2,
    "&&": 3,
    "==": 4,
    "!=": 4,
    "<": 5,
    "<=": 5,
    ">": 5,
    ">=": 5,
    "in": 5,
    "+": 6,
    "-": 6,
    "*": 7,
    "/": 7,
    "%": 7,
}


@dataclass(frozen=True)
class Token:
    kind: str  # num, str, var, id, kw, op, eof
    text: str
    pos: int


def _unescape(raw: str, pos: int) -> str:
    body = raw[1:-1]
    out: list[str] = []
    i = 0
    while i < len(body):
        c = body[i]
        if c == "\\":
            i += 1
            e = body[i]
            if e not in _ESCAPES:
                raise ParseError(f"unknown escape \\{e}", pos + 1 + i)
            out.append(_ESCAPES[e])
        else:
            out.append(c)
        i += 1
    return "".join(out)


def tokenize(source: str) -> list[Token]:
    tokens: list[Token] = []
    i = 0
    while i < len(source):
        m = _TOKEN_RE.match(source, i)
        if not m:
            raise ParseError(f"unexpected character {source[i]!r}", i)
        kind = m.lastgroup or ""
        text = m.group()
        if kind != "ws":
            if kind == "id" and text in _KEYWORDS:
                kind = "kw"
            tokens.append(Token(kind, text, i))
        i = m.end()
    tokens.append(Token("eof", "", len(source)))
    return tokens


class _Parser:
    def __init__(self, source: str, limits: Limits) -> None:
        self.limits = limits
        self.tokens = tokenize(source)
        self.i = 0
        self.depth = 0
        self.nodes = 0

    @property
    def tok(self) -> Token:
        return self.tokens[self.i]

    def advance(self) -> Token:
        t = self.tok
        self.i += 1
        return t

    def is_op(self, text: str) -> bool:
        return self.tok.kind == "op" and self.tok.text == text

    def expect_op(self, text: str) -> Token:
        if not self.is_op(text):
            found = self.tok.text or "end of input"
            raise ParseError(f"expected {text!r}, found {found!r}", self.tok.pos)
        return self.advance()

    def count(self) -> None:
        self.nodes += 1
        if self.nodes > self.limits.max_nodes:
            raise LimitExceeded(f"expression exceeds {self.limits.max_nodes} nodes", self.tok.pos)

    def enter(self) -> None:
        self.depth += 1
        if self.depth > self.limits.max_depth:
            raise LimitExceeded(f"expression nesting exceeds {self.limits.max_depth}", self.tok.pos)

    def leave(self) -> None:
        self.depth -= 1

    def parse(self) -> Node:
        node = self.expr()
        if self.tok.kind != "eof":
            raise ParseError(f"unexpected {self.tok.text!r}", self.tok.pos)
        return node

    def expr(self) -> Node:
        self.enter()
        try:
            cond = self.binary(1)
            if self.is_op("?"):
                pos = self.advance().pos
                then = self.expr()
                self.expect_op(":")
                otherwise = self.expr()
                self.count()
                return Ternary(cond, then, otherwise, pos)
            return cond
        finally:
            self.leave()

    def _binary_op(self) -> str | None:
        t = self.tok
        if t.kind in ("op", "kw") and t.text in _PREC:
            return t.text
        return None

    def binary(self, min_prec: int) -> Node:
        left = self.unary()
        while True:
            op = self._binary_op()
            if op is None or _PREC[op] < min_prec:
                return left
            pos = self.advance().pos
            right = self.binary(_PREC[op] + 1)
            self.count()
            left = Binary(op, left, right, pos)

    def unary(self) -> Node:
        if self.is_op("!") or self.is_op("-"):
            t = self.advance()
            self.enter()
            try:
                operand = self.unary()
            finally:
                self.leave()
            self.count()
            return Unary(t.text, operand, t.pos)
        return self.postfix()

    def postfix(self) -> Node:
        node = self.primary()
        while self.is_op(".") or self.is_op("?."):
            t = self.advance()
            if self.tok.kind != "id":
                raise ParseError("expected member name", self.tok.pos)
            name = self.advance().text
            self.count()
            if self.is_op("("):
                node = Call(node, name, self.args(), t.pos)
            else:
                node = Member(node, name, t.text == "?.", t.pos)
        return node

    def args(self) -> tuple[Node, ...]:
        self.expect_op("(")
        out: list[Node] = []
        if not self.is_op(")"):
            out.append(self.expr())
            while self.is_op(","):
                self.advance()
                out.append(self.expr())
        self.expect_op(")")
        return tuple(out)

    def primary(self) -> Node:
        t = self.tok
        self.count()
        if t.kind == "num":
            self.advance()
            return Literal(Decimal(t.text) if "." in t.text else int(t.text), t.pos)
        if t.kind == "str":
            self.advance()
            return Literal(_unescape(t.text, t.pos), t.pos)
        if t.kind == "kw" and t.text in ("true", "false", "null"):
            self.advance()
            return Literal({"true": True, "false": False, "null": None}[t.text], t.pos)
        if t.kind == "var":
            self.advance()
            return Var(t.text[1:], t.pos)
        if t.kind == "id":
            self.advance()
            if self.is_op("("):
                return Call(None, t.text, self.args(), t.pos)
            return Ident(t.text, t.pos)
        if self.is_op("("):
            self.advance()
            inner = self.expr()
            self.expect_op(")")
            return inner
        if self.is_op("["):
            self.advance()
            items: list[Node] = []
            if not self.is_op("]"):
                items.append(self.expr())
                while self.is_op(","):
                    self.advance()
                    items.append(self.expr())
            self.expect_op("]")
            if len(items) > self.limits.max_list_size:
                raise LimitExceeded("list literal too large", t.pos)
            return ListLit(tuple(items), t.pos)
        raise ParseError(f"unexpected {t.text or 'end of input'!r}", t.pos)


def parse(source: str, limits: Limits = DEFAULT_LIMITS) -> Node:
    """Parse ``source`` into an AST, enforcing ``limits``."""
    if len(source) > limits.max_source_len:
        raise LimitExceeded(f"expression longer than {limits.max_source_len} characters", 0)
    if not source.strip():
        raise ParseError("empty expression", 0)
    return _Parser(source, limits).parse()
