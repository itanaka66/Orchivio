"""Exception hierarchy for the expression engine."""

from __future__ import annotations


class ExprError(Exception):
    """Base class. ``pos`` is a 0-based character offset into the source."""

    def __init__(self, message: str, pos: int = 0) -> None:
        super().__init__(message)
        self.message = message
        self.pos = pos

    def __str__(self) -> str:
        return f"{self.message} (at {self.pos})"


class ParseError(ExprError):
    pass


class TypeCheckError(ExprError):
    pass


class EvalError(ExprError):
    pass


class LimitExceeded(ExprError):
    pass


class NotCompilable(ExprError):
    """The expression cannot be translated to a SQL predicate."""
