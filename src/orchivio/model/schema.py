"""Pydantic schema for the Orchivio application model, ``schema_version`` 1.1.

The model is the source of truth for generated UI, APIs, tests and migrations
(DESIGN.md section 8). This module defines structure only; cross-element rules live in
``orchivio.model.validate``.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

SCHEMA_VERSION = "1.1"

SCALAR_TYPES = frozenset(
    {"string", "text", "integer", "decimal", "boolean", "date", "datetime", "uuid", "json"}
)
OPERATIONS = ("read", "create", "update", "delete")
BUILTIN_ENTITIES = frozenset({"system.User"})

_UUID_RE = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"

ElementId = Annotated[str, StringConstraints(pattern=_UUID_RE)]
EntityName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z][A-Za-z0-9]{0,63}$")]
FieldName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,62}$")]
KeyName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,62}$")]
PageKey = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9-]{0,62}$")]
EnumValueName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,62}$")]
RoutePath = Annotated[str, StringConstraints(pattern=r"^/[A-Za-z0-9/_\-:]*$", max_length=200)]
Expr = Annotated[str, StringConstraints(min_length=1, max_length=2000)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Element(_Strict):
    """Base for named model elements; ``element_id`` is stable across renames."""

    element_id: ElementId | None = None


class ComputedSpec(_Strict):
    mode: Literal["virtual", "stored"] = "virtual"
    expr: Expr


class FieldDef(Element):
    name: FieldName
    type: str
    display_name: str | None = None
    required: bool = False
    unique: bool = False
    primary_key: bool = False
    generated: bool = False
    max_length: int | None = Field(default=None, ge=1)
    minimum: int | float | None = None
    maximum: int | float | None = None
    default: str | int | float | bool | None = None
    default_expr: Expr | None = None
    computed: ComputedSpec | None = None


class IndexDef(_Strict):
    fields: list[FieldName] = Field(min_length=1)
    unique: bool = False


class EntityDef(Element):
    name: EntityName
    display_name: str | None = None
    transient: bool = False
    fields: list[FieldDef] = Field(default_factory=list)
    indexes: list[IndexDef] = Field(default_factory=list)
    hooks: dict[str, str] = Field(default_factory=dict)  # P1: event -> flow key


class EnumDef(Element):
    name: EntityName
    values: list[EnumValueName] = Field(min_length=1)


class PageAction(_Strict):
    key: PageKey
    visible_expr: Expr | None = None
    editable_expr: Expr | None = None


class PageDef(Element):
    key: PageKey
    path: RoutePath
    type: Literal["list", "form", "detail"]
    entity: str
    mode: Literal["create", "edit"] | None = None
    layout: str | None = None
    columns: list[FieldName] = Field(default_factory=list)
    actions: list[PageAction | str] = Field(default_factory=list)


class ModuleDef(Element):
    key: KeyName
    enumerations: list[EnumDef] = Field(default_factory=list)
    entities: list[EntityDef] = Field(default_factory=list)
    pages: list[PageDef] = Field(default_factory=list)


class AccessRule(Element):
    entity: str  # qualified: module.Entity
    role: KeyName
    operations: list[Literal["read", "create", "update", "delete"]] = Field(min_length=1)
    row_constraint: Expr | None = None


class SecurityModel(_Strict):
    roles: list[KeyName] = Field(default_factory=list)
    access: list[AccessRule] = Field(default_factory=list)
    pages: dict[str, list[KeyName]] = Field(default_factory=dict)
    anonymous: bool = False  # explicit opt-in; requires A3 approval (FR-067)


class LintSuppression(_Strict):
    """Silences one lint rule on one element; the justification is part of the revision."""

    rule: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9-]{2,63}$")]
    element_id: ElementId
    justification: str = Field(min_length=10, max_length=500)


class ApplicationInfo(_Strict):
    key: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]{0,62}$")]
    name: str = Field(min_length=1, max_length=200)
    version: str = "0.1.0"
    default_locale: str = "en-US"
    locales: list[str] = Field(default_factory=list)


class AppModel(_Strict):
    schema_version: Literal["1.1"] = "1.1"
    application: ApplicationInfo
    modules: list[ModuleDef] = Field(default_factory=list)
    security: SecurityModel = Field(default_factory=SecurityModel)
    lint_suppressions: list[LintSuppression] = Field(default_factory=list)


# --- helpers --------------------------------------------------------------

_TYPE_RE = re.compile(r"^(enum|ref):([A-Za-z][A-Za-z0-9_.]*)$")


def parse_field_type(t: str) -> tuple[str, str | None] | None:
    """Return ``(kind, target)``: ('scalar', name) | ('enum', Name) | ('ref', Entity).

    ``None`` if ``t`` is not a syntactically valid type.
    """
    if t in SCALAR_TYPES:
        return "scalar", t
    m = _TYPE_RE.match(t)
    if m:
        return m.group(1), m.group(2)
    return None


def qualify(module_key: str, name: str) -> str:
    """``Product`` -> ``core.Product``; already-qualified names pass through."""
    return name if "." in name else f"{module_key}.{name}"


def canonical_json(model: AppModel | dict[str, Any]) -> str:
    """Stable serialisation used for hashing and storage comparisons."""
    data = model.model_dump(mode="json", exclude_none=True) if isinstance(model, AppModel) else dict(model)
    if not data.get("lint_suppressions"):
        data.pop("lint_suppressions", None)  # keeps hashes of models that have none unchanged
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def model_hash(model: AppModel) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(model).encode("utf-8")).hexdigest()
