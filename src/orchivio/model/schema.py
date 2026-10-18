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
OPERATIONS = ("read", "create", "update", "delete")  # the CRUD matrix that generated tests cover
ALL_OPERATIONS = (*OPERATIONS, "export")  # export is granted separately from read (FR-074)
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


FLOW_KEY_PATTERN = r"^[A-Za-z][A-Za-z0-9_]{0,62}$"
STEP_ID_PATTERN = r"^[a-z][a-z0-9_]{0,30}$"
FlowKey = Annotated[str, StringConstraints(pattern=FLOW_KEY_PATTERN)]
StepId = Annotated[str, StringConstraints(pattern=STEP_ID_PATTERN)]
VarName = Annotated[str, StringConstraints(pattern=STEP_ID_PATTERN)]

StepType = Literal["start", "retrieve", "create", "change", "delete", "decision", "loop", "call_flow",
                   "call_integration", "raise_error", "end"]
Handle = Literal["out", "true", "false", "body", "done"]


class Position(_Strict):
    """Editor layout only; never affects behaviour or diffs."""

    x: float = Field(ge=-100_000, le=100_000)
    y: float = Field(ge=-100_000, le=100_000)


class FlowParam(_Strict):
    name: VarName
    type: str


class FlowStep(_Strict):
    id: StepId
    type: StepType
    label: str | None = Field(default=None, max_length=100)
    config: dict[str, Any] = Field(default_factory=dict)
    position: Position | None = None


class FlowEdge(_Strict):
    id: StepId
    source: StepId
    target: StepId
    handle: Handle = "out"


class FlowTestUser(_Strict):
    id: str = Field(default="test-user", min_length=1, max_length=64)
    roles: list[KeyName] | None = None  # None: every role the model defines


class RecordExpectation(_Strict):
    entity: str
    where: Expr | None = None  # boolean over the record's fields; omitted: all records
    count: int = Field(ge=0, le=100_000)


class FlowExpect(_Strict):
    status: Literal["succeeded", "failed"] = "succeeded"
    error_contains: str | None = Field(default=None, max_length=200)
    visited: list[StepId] = Field(default_factory=list, max_length=200)
    not_visited: list[StepId] = Field(default_factory=list, max_length=200)
    records: list[RecordExpectation] = Field(default_factory=list, max_length=20)


class IntegrationMock(_Strict):
    """A canned answer for an integration operation in a flow test (tests never reach the network)."""

    status: int = Field(default=200, ge=100, le=599)
    json_body: Any = Field(default=None, alias="json")

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class FlowTestCase(_Strict):
    """A declarative flow test: fixtures in, run, assertions out (FR-058)."""

    name: str = Field(min_length=1, max_length=100)
    args: dict[str, Any] = Field(default_factory=dict)
    user: FlowTestUser = Field(default_factory=FlowTestUser)
    fixtures: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    mocks: dict[str, IntegrationMock] | None = None  # "<integration>.<operation>" -> canned response
    expect: FlowExpect = Field(default_factory=FlowExpect)


class ScheduleDef(_Strict):
    """Run a server flow on a cron schedule (UTC) as a system identity holding ``run_as_role`` (FR-055)."""

    cron: str = Field(max_length=100)
    run_as_role: KeyName
    args: dict[str, str | int | float | bool | None] = Field(default_factory=dict, max_length=30)
    enabled: bool = True
    missed_runs: Literal["skip", "run_once"] = "skip"  # what to do for occurrences missed while nothing was running
    max_attempts: int = Field(default=3, ge=1, le=10)
    backoff_seconds: int = Field(default=60, ge=1, le=3600)  # doubled after every failed attempt


class FlowDef(Element):
    """A Server or Client Flow as a directed acyclic graph of allowlisted steps (FLOW-SPEC.md)."""

    key: FlowKey
    kind: Literal["server", "client"] = "server"
    description: str | None = Field(default=None, max_length=500)
    params: list[FlowParam] = Field(default_factory=list, max_length=30)
    steps: list[FlowStep] = Field(default_factory=list, max_length=200)
    edges: list[FlowEdge] = Field(default_factory=list, max_length=400)
    tests: list[FlowTestCase] = Field(default_factory=list, max_length=50)
    schedule: ScheduleDef | None = None


PROCESS_STEP_TYPES = ("start", "user_task", "system_task", "decision", "parallel_split", "parallel_join", "timer", "end")
ProcessStepType = Literal["start", "user_task", "system_task", "decision", "parallel_split", "parallel_join", "timer", "end"]
OutcomeName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,30}$")]


class ProcessFormField(_Strict):
    """A value the person completing a user task supplies; it becomes a process variable of the same name."""

    name: VarName
    type: Literal["string", "integer", "decimal", "boolean"] = "string"
    required: bool = True


class ProcessStep(_Strict):
    id: StepId
    type: ProcessStepType
    label: str | None = Field(default=None, max_length=100)
    config: dict[str, Any] = Field(default_factory=dict)
    position: Position | None = None


class ProcessEdge(_Strict):
    id: StepId
    source: StepId
    target: StepId
    outcome: OutcomeName | None = None  # the user-task outcome, or true/false after a decision


class ProcessDef(Element):
    """A long-running business process: tasks for people, flows run by the system, decisions, parallel paths and timers
    (FR-110). Instances are persisted and survive restarts; people see their tasks in the task inbox (FR-111)."""

    key: FlowKey
    description: str | None = Field(default=None, max_length=500)
    params: list[FlowParam] = Field(default_factory=list, max_length=30)
    start_roles: list[KeyName] = Field(default_factory=list, max_length=20)  # application roles that may start an instance
    run_as_role: KeyName | None = None  # role under which system tasks run their flows
    steps: list[ProcessStep] = Field(default_factory=list, max_length=100)
    edges: list[ProcessEdge] = Field(default_factory=list, max_length=300)


HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
INTEGRATION_PARAM_TYPES = ("string", "integer", "decimal", "boolean")
IntegrationKey = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,62}$")]
DestinationName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9-]{0,62}$")]
HttpPath = Annotated[str, StringConstraints(pattern=r"^/[A-Za-z0-9/_\-.{}~%:@!$&'()*+,;=]*$", max_length=300)]


class IntegrationParam(_Strict):
    name: FieldName
    location: Literal["path", "query"]
    type: Literal["string", "integer", "decimal", "boolean"] = "string"
    required: bool = True
    json_name: str | None = Field(default=None, max_length=100)  # the name on the wire when it is not ``name``


class IntegrationOperation(_Strict):
    """One call of an external REST API, typed by the model: scalar parameters, a request-body entity and a
    response entity (both transient entities of the module)."""

    id: KeyName
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
    path: HttpPath
    summary: str | None = Field(default=None, max_length=200)
    params: list[IntegrationParam] = Field(default_factory=list, max_length=30)
    body: EntityName | None = None
    body_map: dict[str, str] = Field(default_factory=dict, max_length=100)  # field -> JSON property, where different
    response: EntityName | None = None
    response_kind: Literal["object", "list"] | None = None
    response_map: dict[str, str] = Field(default_factory=dict, max_length=100)
    timeout_seconds: int = Field(default=10, ge=1, le=30)


class IntegrationDef(Element):
    """A consumed REST API (FR-081). ``destination`` is a logical name; the real base URL and credentials are
    configured per installation outside the model, so the model stays portable and destinations stay allowlisted."""

    key: IntegrationKey
    description: str | None = Field(default=None, max_length=500)
    destination: DestinationName
    operations: list[IntegrationOperation] = Field(default_factory=list, max_length=200)


class ModuleDef(Element):
    key: KeyName
    enumerations: list[EnumDef] = Field(default_factory=list)
    entities: list[EntityDef] = Field(default_factory=list)
    pages: list[PageDef] = Field(default_factory=list)
    flows: list[FlowDef] = Field(default_factory=list)
    integrations: list[IntegrationDef] | None = None  # None (not []) keeps the hash of models without any
    processes: list[ProcessDef] | None = None


class FieldAccess(_Strict):
    """Which fields a rule exposes (FR-064). ``None`` means every field; a list is an allow-list, so a field
    added to the entity later stays hidden from restricted rules until someone lists it."""

    read: list[FieldName] | None = None
    write: list[FieldName] | None = None


class AccessRule(Element):
    entity: str  # qualified: module.Entity
    role: KeyName
    operations: list[Literal["read", "create", "update", "delete", "export"]] = Field(min_length=1)
    row_constraint: Expr | None = None
    fields: FieldAccess | None = None


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
