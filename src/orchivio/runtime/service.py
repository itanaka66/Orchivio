"""CRUD data layer for a generated application (FR-030, FR-023, FR-063).

Every operation goes through here: resolve role access, apply the compiled row predicate to
the query (reads, updates, deletes, counts), validate server-side, check that the resulting
record still satisfies the row constraint (creates and updates), and audit. If a row
constraint cannot be compiled the operation is denied rather than run unfiltered.
"""

from __future__ import annotations

import json
import logging
import math
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from sqlalchemy import Engine, delete, func, insert, select, text, update
from sqlalchemy.exc import IntegrityError

from orchivio.expr import (
    CheckEnv,
    CheckResult,
    EvalContext,
    EvalError,
    ExprError,
    NotCompilable,
    SqlContext,
    UserRef,
    check,
    evaluate,
)
from orchivio.model import AppModel
from orchivio.model.schema import FieldDef, parse_field_type, qualify
from orchivio.ports import AuditRecord, AuditSink
from orchivio.security import record_allowed, resolve_access, resolve_fields, row_predicate

from . import outbox
from .tables import USER_REF_LENGTH, EntityInfo, RuntimeSchema

log = logging.getLogger("orchivio.runtime")
MAX_PAGE = 200
MAX_JSON_BYTES = 100_000


class DataError(Exception):
    status = 400
    code = "data_error"

    def __init__(self, message: str, details: Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details


class UnknownEntity(DataError):
    status, code = 404, "unknown_entity"


class RowNotFound(DataError):
    status, code = 404, "not_found"


class Forbidden(DataError):
    status, code = 403, "forbidden"


class Invalid(DataError):
    status, code = 422, "invalid_record"


class Conflict(DataError):
    status, code = 409, "conflict"


class HookFailed(DataError):
    """An entity event hook (a flow) refused or failed; the whole operation is rolled back."""

    status, code = 409, "hook_failed"


MAX_HOOK_DEPTH = 3  # hooks that write can trigger further hooks; bounded recursion (FR-054)


class Hooks(Protocol):
    """Runs the flow bound to an entity event. Implemented by ``orchivio.logic`` (inverted so that
    the runtime does not depend on the engine)."""

    def run(self, data: "AppData", flow: str, event: str, entity: str, user: "AppUser | None",
            record: dict[str, Any], previous: dict[str, Any] | None) -> None: ...


@dataclass(frozen=True)
class AppUser:
    id: str
    roles: frozenset[str]


# --- value coercion ----------------------------------------------------------------------


def coerce_value(schema: RuntimeSchema, mod_key: str, f: FieldDef, value: Any) -> Any:
    """Validate ``value`` for ``f`` and return it in its stored Python type. Raises ValueError."""
    if value is None:
        return None
    kind, target = parse_field_type(f.type) or ("", None)
    if kind == "enum":
        values = schema.index.enums[qualify(mod_key, target or "")]
        if not isinstance(value, str) or value not in values:
            raise ValueError(f"must be one of: {', '.join(values)}")
        return value
    if kind == "ref":
        q = schema.index.resolve_entity(mod_key, target or "")
        if q == "system.User":
            if not isinstance(value, str) or not 1 <= len(value) <= USER_REF_LENGTH:
                raise ValueError(f"must be a user id of 1-{USER_REF_LENGTH} characters")
            return value
        info = schema.info(q or "")
        if info is None:
            raise ValueError("references an unknown entity")
        return coerce_value(schema, info.module.key, info.pk, value)
    return _coerce_scalar(f, target or "string", value)


def _coerce_scalar(f: FieldDef, scalar: str, v: Any) -> Any:  # noqa: C901 - one branch per type
    if scalar in ("string", "text"):
        if not isinstance(v, str):
            raise ValueError("must be a string")
        limit = f.max_length or (255 if scalar == "string" else None)
        if limit and len(v) > limit:
            raise ValueError(f"must be at most {limit} characters")
        return v
    if scalar == "integer":
        if isinstance(v, bool) or not isinstance(v, int):
            raise ValueError("must be an integer")
        return _in_range(f, v)
    if scalar == "decimal":
        if isinstance(v, bool) or not isinstance(v, (int, float, str, Decimal)):
            raise ValueError("must be a number")
        try:
            d = Decimal(str(v))
        except InvalidOperation as e:
            raise ValueError("must be a number") from e
        if not d.is_finite() or abs(d) >= Decimal(10) ** 20:
            raise ValueError("number out of range")
        return _in_range(f, d.quantize(Decimal("0.00000001")))
    if scalar == "boolean":
        if not isinstance(v, bool):
            raise ValueError("must be true or false")
        return v
    if scalar == "uuid":
        try:
            return v if isinstance(v, uuid.UUID) else uuid.UUID(str(v))
        except ValueError as e:
            raise ValueError("must be a UUID") from e
    if scalar == "date":
        if isinstance(v, datetime):
            return v.date()
        if isinstance(v, date):
            return v
        try:
            return date.fromisoformat(str(v))
        except ValueError as e:
            raise ValueError("must be an ISO date (YYYY-MM-DD)") from e
    if scalar == "datetime":
        if isinstance(v, datetime):
            dt = v
        else:
            try:
                dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
            except ValueError as e:
                raise ValueError("must be an ISO 8601 datetime") from e
        return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
    if scalar == "json":
        try:
            if len(json.dumps(v)) > MAX_JSON_BYTES:
                raise ValueError("JSON value too large")
        except TypeError as e:
            raise ValueError("must be valid JSON") from e
        return v
    raise ValueError(f"unsupported type {scalar}")


def _in_range(f: FieldDef, v: Any) -> Any:
    if f.minimum is not None and v < f.minimum:
        raise ValueError(f"must be at least {f.minimum}")
    if f.maximum is not None and v > f.maximum:
        raise ValueError(f"must be at most {f.maximum}")
    return v


def to_eval(value: Any) -> Any:
    """Stored value -> expression-engine value (uuid as string, date as datetime)."""
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, date):
        return datetime.combine(value, time.min, tzinfo=UTC)
    if isinstance(value, float):
        return Decimal(str(value))
    return value


def to_json(value: Any) -> Any:
    if isinstance(value, (uuid.UUID, Decimal)):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


# --- service -----------------------------------------------------------------------------


class AppData:
    def __init__(self, engine: Engine, schema: RuntimeSchema, *, audit: AuditSink | None = None,
                 application_id: uuid.UUID | None = None, organization_id: uuid.UUID | None = None,
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC), hooks: Hooks | None = None) -> None:
        self.engine = engine
        self.schema = schema
        self.model: AppModel = schema.model
        self.audit = audit
        self.application_id = application_id
        self.organization_id = organization_id
        self.clock = clock
        self.hooks = hooks
        # Not thread-safe by design: an AppData is created per request / per run.
        self._bound = None
        self._pending_audit: list[AuditRecord] = []
        self._hook_depth = 0
        self._computed: dict[str, list[tuple[FieldDef, CheckResult]]] = {}
        self._defaults: dict[tuple[str, str], CheckResult] = {}
        self._prepare_expressions()
        if application_id is not None:
            outbox.ensure(engine)

    # -- setup ----------------------------------------------------------------------------

    def _prepare_expressions(self) -> None:
        for q, info in self.schema.entities.items():
            types = self.schema.index.types_for(info.module.key)
            computed = {}
            for f in info.entity.fields:
                if f.computed:
                    computed[f.name] = (f, check(f.computed.expr, CheckEnv(types, q)))
                if f.default_expr:
                    self._defaults[(q, f.name)] = check(f.default_expr, CheckEnv(types, None))
            order: list[str] = []
            seen: set[str] = set()

            def visit(name: str) -> None:
                if name in seen:
                    return
                seen.add(name)
                for dep in sorted(computed[name][1].fields & computed.keys()):
                    visit(dep)
                order.append(name)

            for name in sorted(computed):
                visit(name)
            self._computed[q] = [computed[n] for n in order]

    # -- connections and transactions -----------------------------------------------------

    @contextmanager
    def _conn(self, write: bool = False) -> Iterator[Any]:
        if self._bound is not None:
            yield self._bound
        elif write:
            with self.engine.begin() as conn:
                yield conn
        else:
            with self.engine.connect() as conn:
                yield conn

    @contextmanager
    def transaction(self) -> Iterator[AppData]:
        """Run several operations atomically. Re-entrant: nested use joins the outer transaction.

        Audit events are held back and written only if the transaction commits, so a rolled-back
        flow leaves no trace of writes that never happened.
        """
        if self._bound is not None:
            yield self
            return
        with self.engine.begin() as conn:
            self._bound, self._pending_audit = conn, []
            try:
                yield self
            except BaseException:
                self._pending_audit = []
                raise
            finally:
                self._bound = None
        pending, self._pending_audit = self._pending_audit, []
        for event in pending:
            if self.audit is not None:
                self.audit.record(event)

    def _hook(self, event: str, info: EntityInfo, user: AppUser | None, record: dict[str, Any],
              previous: dict[str, Any] | None = None) -> None:
        flow = info.entity.hooks.get(event)
        if not flow or self.hooks is None:
            return
        if self._hook_depth >= MAX_HOOK_DEPTH:
            raise HookFailed(f"hooks are nested more than {MAX_HOOK_DEPTH} levels deep")
        self._hook_depth += 1
        try:
            self.hooks.run(self, flow, event, info.qualified, user, record, previous)
        finally:
            self._hook_depth -= 1

    # -- helpers --------------------------------------------------------------------------

    def _info(self, entity: str) -> EntityInfo:
        info = self.schema.info(entity)
        if info is None:
            raise UnknownEntity(f"unknown or non-persistent entity {entity!r}")
        return info

    def _eval_ctx(self, info: EntityInfo, user: AppUser | None, record: Mapping[str, Any] | None) -> EvalContext:
        enums = {e.name: tuple(e.values) for e in info.module.enumerations}
        return EvalContext(record=record, current_user=UserRef(user.id, user.roles) if user else None,
                           now=self.clock(), enums=enums)

    def _guard(self, info: EntityInfo, user: AppUser, op: str):
        decision = resolve_access(self.model, user.roles, info.qualified, op)
        if not decision.allowed:
            raise Forbidden(f"your roles may not {op} {info.entity.name}")
        try:
            pred = row_predicate(decision, SqlContext(current_user=UserRef(user.id, user.roles), now=self.clock(),
                                                      dialect=self.engine.dialect.name))
        except NotCompilable as e:
            log.warning("row constraint on %s cannot be compiled (%s); denying", info.qualified, e.message)
            raise Forbidden("access rule cannot be evaluated; denied") from e
        return decision, text(pred.sql).bindparams(**pred.params)

    def readable_fields(self, entity: str, user: AppUser) -> frozenset[str] | None:
        """Fields ``user`` may see on ``entity`` (``None`` = all). The primary key is always included so a
        record can still be addressed."""
        info = self._info(entity)
        names = resolve_fields(self.model, user.roles, info.qualified, "read")
        return None if names is None else names | {info.pk.name}

    def writable_fields(self, entity: str, user: AppUser, op: str) -> frozenset[str] | None:
        return resolve_fields(self.model, user.roles, self._info(entity).qualified, op)

    def _check_writable(self, info: EntityInfo, user: AppUser, op: str, data: Mapping[str, Any]) -> None:
        """Reject writes to fields the roles may not write. A field the user cannot even read is reported as
        unknown, so the error does not reveal that it exists."""
        writable = self.writable_fields(info.qualified, user, op)
        if writable is None:
            return
        readable = self.readable_fields(info.qualified, user)
        known = {f.name for f in info.entity.fields}
        errors = []
        for name in data:
            if name in known and name not in writable:
                hidden = readable is not None and name not in readable
                errors.append({"field": name, "message": "unknown field" if hidden else "read-only for your roles"})
        if errors:
            raise Invalid("the record is invalid", errors)

    def _row_to_eval(self, info: EntityInfo, row: Mapping[str, Any]) -> dict[str, Any]:
        rec = {f.name: to_eval(row.get(f.name)) for f in info.stored_fields}
        return rec

    def _with_computed(self, info: EntityInfo, user: AppUser | None, rec: dict[str, Any]) -> dict[str, Any]:
        for f, res in self._computed.get(info.qualified, []):
            try:
                rec[f.name] = to_eval(evaluate(res.ast, self._eval_ctx(info, user, rec)))
            except (EvalError, ExprError) as e:
                raise Invalid(f"computed field '{f.name}' could not be evaluated: {e.message}") from e
        return rec

    def eval_record(self, entity: str, row: Mapping[str, Any], user: AppUser | None = None) -> dict[str, Any]:
        """A stored row as the expression engine sees it (computed fields included)."""
        info = self._info(entity)
        return self._with_computed(info, user, self._row_to_eval(info, row))

    def candidate_record(self, entity: str, user: AppUser, data: Mapping[str, Any]) -> dict[str, Any]:
        """The expression-engine view of the record ``create`` would store (nothing is written).
        Raises ``Invalid`` if ``data`` is not a valid record."""
        info = self._info(entity)
        return self._build_new(info, user, data)[1]

    def _present(self, info: EntityInfo, row: Mapping[str, Any], user: AppUser | None) -> dict[str, Any]:
        rec = self._with_computed(info, user, self._row_to_eval(info, row))
        visible = None if user is None else self.readable_fields(info.qualified, user)
        out = {}
        for f in info.entity.fields:
            if visible is not None and f.name not in visible:
                continue  # removed from the response, not blanked (FR-064)
            raw = row.get(f.name) if f in info.stored_fields else rec.get(f.name)
            out[f.name] = to_json(raw)
        return out

    def _audit(self, action: str, info: EntityInfo, user: AppUser, row_id: Any) -> None:
        if self.audit is None:
            return
        record = (AuditRecord(
            action=action, plane="application", channel="api", organization_id=self.organization_id,
            target_type=info.qualified, target_id=str(row_id),
            metadata={"application_id": str(self.application_id) if self.application_id else None,
                      "app_user": user.id, "roles": sorted(user.roles)}))
        if self._bound is not None:
            self._pending_audit.append(record)
        else:
            self.audit.record(record)

    def _emit(self, kind: str, info: EntityInfo, pk: Any, row: Mapping[str, Any], previous: Mapping[str, Any] | None = None) -> None:
        """Business event for the transactional outbox, written in the current transaction (FR-083)."""
        if self.application_id is None:
            return
        payload: dict[str, Any] = {"entity": info.qualified, "id": to_json(pk), "record": self._present(info, row, None)}
        if previous is not None:
            payload["changed"] = [f.name for f in info.stored_fields if row.get(f.name) != previous.get(f.name)]
        with self._conn(write=True) as conn:
            outbox.add(conn, self.application_id, f"{info.qualified}.{kind}", f"{info.qualified}/{to_json(pk)}", payload, self.clock())

    def _coerce_pk(self, info: EntityInfo, raw: Any) -> Any:
        try:
            return coerce_value(self.schema, info.module.key, info.pk, raw)
        except ValueError as e:
            raise RowNotFound("record not found") from e

    # -- validation -----------------------------------------------------------------------

    def _validate(self, info: EntityInfo, data: Mapping[str, Any], *, creating: bool) -> dict[str, Any]:
        out, errors = self._validate_partial(info, data, creating=creating)
        if errors:
            raise Invalid("the record is invalid", errors)
        return out

    def _validate_partial(self, info: EntityInfo, data: Mapping[str, Any], *, creating: bool):
        fields = {f.name: f for f in info.entity.fields}
        errors: list[dict[str, str]] = []
        out: dict[str, Any] = {}
        for name, raw in data.items():
            f = fields.get(name)
            if f is None:
                errors.append({"field": name, "message": "unknown field"})
            elif f.generated or f.computed or (f.primary_key and not creating):
                errors.append({"field": name, "message": "this field is set by the system"})
            else:
                try:
                    out[name] = coerce_value(self.schema, info.module.key, f, raw)
                except ValueError as e:
                    errors.append({"field": name, "message": str(e)})
        return out, errors

    def _required_errors(self, info: EntityInfo, values: Mapping[str, Any]) -> list[dict[str, str]]:
        return [{"field": f.name, "message": "is required"} for f in info.stored_fields
                if f.required and not f.generated and not f.computed and values.get(f.name) is None]

    def _check_required(self, info: EntityInfo, values: Mapping[str, Any]) -> None:
        errors = self._required_errors(info, values)
        if errors:
            raise Invalid("the record is invalid", errors)

    def _check_references_and_unique(self, info: EntityInfo, values: Mapping[str, Any], exclude_pk: Any = None) -> None:
        with self._conn() as conn:
            for f in info.stored_fields:
                v = values.get(f.name)
                kind, target = parse_field_type(f.type) or ("", None)
                if v is not None and kind == "ref":
                    q = self.schema.index.resolve_entity(info.module.key, target or "")
                    tinfo = self.schema.info(q or "")
                    if tinfo is not None and conn.execute(
                            select(1).select_from(tinfo.table).where(tinfo.table.c[tinfo.pk.name] == v)).first() is None:
                        raise Invalid("the record is invalid", [{"field": f.name, "message": "referenced record does not exist"}])
                if v is not None and f.unique and not f.primary_key:
                    self._unique(conn, info, [f.name], values, exclude_pk)
            for idx in info.entity.indexes:
                if idx.unique and all(values.get(n) is not None for n in idx.fields):
                    self._unique(conn, info, list(idx.fields), values, exclude_pk)

    @staticmethod
    def _unique(conn, info: EntityInfo, names: list[str], values: Mapping[str, Any], exclude_pk: Any) -> None:
        t = info.table
        q = select(1).select_from(t).where(*[t.c[n] == values[n] for n in names])
        if exclude_pk is not None:
            q = q.where(t.c[info.pk.name] != exclude_pk)
        if conn.execute(q).first() is not None:
            raise Conflict("a record with the same value already exists", {"fields": names})

    def _build_new(self, info: EntityInfo, user: AppUser | None, data: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        """Validated column values and the eval record for a new row (no access checks)."""
        supplied, errors = self._validate_partial(info, data, creating=True)
        values: dict[str, Any] = {}
        ctx0 = self._eval_ctx(info, user, None)
        for f in info.stored_fields:
            if f.name in supplied:
                values[f.name] = supplied[f.name]
            elif f.generated:
                values[f.name] = uuid.uuid4() if (f.type == "uuid") else self.clock()
            elif f.computed:
                continue
            elif (info.qualified, f.name) in self._defaults:
                res = self._defaults[(info.qualified, f.name)]
                try:
                    produced = evaluate(res.ast, ctx0)
                    produced = str(produced) if isinstance(produced, str) else produced  # UserRef -> plain str
                    values[f.name] = coerce_value(self.schema, info.module.key, f, produced)
                except (EvalError, ValueError) as e:
                    raise Invalid("the record is invalid", [{"field": f.name, "message": f"default failed: {e}"}]) from e
            elif f.default is not None:
                values[f.name] = coerce_value(self.schema, info.module.key, f, f.default)
        bad = {e["field"] for e in errors}
        errors += [e for e in self._required_errors(info, values) if e["field"] not in bad]
        if errors:  # report every problem at once, not one per request
            raise Invalid("the record is invalid", errors)
        rec = self._with_computed(info, user, {k: to_eval(v) for k, v in values.items()})
        for f, _ in self._computed.get(info.qualified, []):
            if f.computed and f.computed.mode == "stored":
                values[f.name] = _restore(f, rec.get(f.name))
        return values, rec

    # -- operations -----------------------------------------------------------------------

    def list(self, entity: str, user: AppUser, *, filters: Mapping[str, Any] | None = None, order_by: str | None = None,
             descending: bool = False, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        info = self._info(entity)
        _, pred = self._guard(info, user, "read")
        t = info.table
        where = [pred]
        visible = self.readable_fields(entity, user)  # filtering or sorting by a hidden field would leak its values
        for name, raw in (filters or {}).items():
            f = next((x for x in info.stored_fields if x.name == name and (visible is None or x.name in visible)), None)
            if f is None:
                raise Invalid("unknown filter field", [{"field": name, "message": "not a stored field"}])
            try:
                where.append(t.c[name] == coerce_value(self.schema, info.module.key, f, _parse_query_value(f, raw)))
            except ValueError as e:
                raise Invalid("invalid filter", [{"field": name, "message": str(e)}]) from e
        order_col = t.c[info.pk.name]
        if order_by is not None:
            if order_by not in t.c or (visible is not None and order_by not in visible):
                raise Invalid("cannot sort by that field", [{"field": order_by, "message": "not a stored field"}])
            order_col = t.c[order_by]
        limit = max(1, min(limit, MAX_PAGE))
        with self._conn() as conn:
            total = conn.execute(select(func.count()).select_from(t).where(*where)).scalar_one()
            rows = conn.execute(select(t).where(*where).order_by(order_col.desc() if descending else order_col,
                                                                 t.c[info.pk.name]).limit(limit).offset(max(0, offset))).mappings().all()
        return {"items": [self._present(info, dict(r), user) for r in rows], "total": total, "limit": limit, "offset": offset}

    def select_rows(self, entity: str, user: AppUser, *, where: tuple[str, Mapping[str, Any]] | None = None,
                    limit: int = 100) -> list[dict[str, Any]]:
        """Raw stored rows the user may read, optionally narrowed by a parameterised SQL predicate
        (used by flows). The role/row-constraint predicate is always applied."""
        info = self._info(entity)
        _, pred = self._guard(info, user, "read")
        clauses = [pred]
        if where is not None:
            clauses.append(text(where[0]).bindparams(**where[1]))
        with self._conn() as conn:
            rows = conn.execute(select(info.table).where(*clauses).order_by(info.table.c[info.pk.name])
                                .limit(max(1, min(limit, MAX_PAGE * 5)))).mappings().all()
        visible = self.readable_fields(entity, user)
        if visible is None:
            return [dict(r) for r in rows]
        return [{k: (v if k in visible else None) for k, v in r.items()} for r in rows]  # flows act as the caller

    def get(self, entity: str, user: AppUser, row_id: Any) -> dict[str, Any]:
        info = self._info(entity)
        _, pred = self._guard(info, user, "read")
        pk = self._coerce_pk(info, row_id)
        with self._conn() as conn:
            row = conn.execute(select(info.table).where(info.table.c[info.pk.name] == pk, pred)).mappings().first()
        if row is None:
            raise RowNotFound("record not found")
        return self._present(info, dict(row), user)

    def create(self, entity: str, user: AppUser, data: Mapping[str, Any]) -> dict[str, Any]:
        info = self._info(entity)
        row = self.create_record(entity, user, data)
        return self._present(info, row, user)

    def create_record(self, entity: str, user: AppUser, data: Mapping[str, Any]) -> dict[str, Any]:
        """Create and return the stored row (atomic with its hooks)."""
        info = self._info(entity)
        with self.transaction():
            decision, _ = self._guard(info, user, "create")
            self._check_writable(info, user, "create", data)
            values, rec = self._build_new(info, user, data)
            if not record_allowed(decision, rec, self._eval_ctx(info, user, rec)):
                raise Forbidden("the new record would not satisfy your row-level access rule")
            self._check_references_and_unique(info, values)
            self._hook("before_create", info, user, rec)
            try:
                with self._conn(write=True) as conn:
                    conn.execute(insert(info.table).values(**values))
            except IntegrityError as e:
                raise Conflict("the record violates a database constraint") from e
            pk = values[info.pk.name]
            with self._conn() as conn:
                row = dict(conn.execute(select(info.table).where(info.table.c[info.pk.name] == pk)).mappings().one())
            self._audit("record.created", info, user, pk)
            self._emit("created", info, pk, row)
            self._hook("after_create", info, user, self.eval_record(entity, row, user))
            return row

    def update(self, entity: str, user: AppUser, row_id: Any, patch: Mapping[str, Any]) -> dict[str, Any]:
        info = self._info(entity)
        return self._present(info, self.update_record(entity, user, row_id, patch), user)

    def update_record(self, entity: str, user: AppUser, row_id: Any, patch: Mapping[str, Any]) -> dict[str, Any]:
        """Update and return the stored row (atomic with its hooks)."""
        info = self._info(entity)
        with self.transaction():
            decision, pred = self._guard(info, user, "update")
            pk = self._coerce_pk(info, row_id)
            t = info.table
            with self._conn() as conn:
                current = conn.execute(select(t).where(t.c[info.pk.name] == pk, pred)).mappings().first()
            if current is None:
                raise RowNotFound("record not found")
            self._check_writable(info, user, "update", patch)
            changes = self._validate(info, patch, creating=False)
            merged = {f.name: current[f.name] for f in info.stored_fields}
            merged.update(changes)
            self._check_required(info, merged)
            rec = self._with_computed(info, user, {k: to_eval(v) for k, v in merged.items()})
            for f, _ in self._computed.get(info.qualified, []):
                if f.computed and f.computed.mode == "stored":
                    merged[f.name] = changes[f.name] = _restore(f, rec.get(f.name))
            if not record_allowed(decision, rec, self._eval_ctx(info, user, rec)):
                raise Forbidden("the updated record would not satisfy your row-level access rule")
            self._check_references_and_unique(info, merged, exclude_pk=pk)
            self._hook("before_update", info, user, rec, self.eval_record(entity, dict(current), user))
            try:
                with self._conn(write=True) as conn:
                    if changes:
                        res = conn.execute(update(t).where(t.c[info.pk.name] == pk, pred).values(**changes))
                        if res.rowcount == 0:  # row vanished or stopped matching concurrently
                            raise RowNotFound("record not found")
            except IntegrityError as e:
                raise Conflict("the record violates a database constraint") from e
            with self._conn() as conn:  # the caller was allowed to update it; no read predicate here
                row = dict(conn.execute(select(t).where(t.c[info.pk.name] == pk)).mappings().one())
            self._audit("record.updated", info, user, pk)
            self._emit("updated", info, pk, row, dict(current))
            self._hook("after_update", info, user, self.eval_record(entity, row, user),
                       self.eval_record(entity, dict(current), user))
            return row

    def delete(self, entity: str, user: AppUser, row_id: Any) -> None:
        info = self._info(entity)
        with self.transaction():
            _, pred = self._guard(info, user, "delete")
            pk = self._coerce_pk(info, row_id)
            with self._conn() as conn:
                current = conn.execute(select(info.table).where(info.table.c[info.pk.name] == pk, pred)).mappings().first()
            if current is None:
                raise RowNotFound("record not found")
            before = self.eval_record(entity, dict(current), user)
            self._hook("before_delete", info, user, before)
            try:
                with self._conn(write=True) as conn:
                    res = conn.execute(delete(info.table).where(info.table.c[info.pk.name] == pk, pred))
            except IntegrityError as e:
                raise Conflict("other records still reference this one") from e
            if res.rowcount == 0:
                raise RowNotFound("record not found")
            self._audit("record.deleted", info, user, pk)
            self._emit("deleted", info, pk, dict(current))
            self._hook("after_delete", info, user, before)

    def seed(self, entity: str, data: Mapping[str, Any]) -> dict[str, Any]:
        """Insert a validated row without access checks. For synthetic preview data only."""
        info = self._info(entity)
        values, _ = self._build_new(info, None, data)
        self._check_references_and_unique(info, values)
        with self._conn(write=True) as conn:
            conn.execute(insert(info.table).values(**values))
        return values


def _parse_query_value(f: FieldDef, raw: Any) -> Any:
    if not isinstance(raw, str):
        return raw
    scalar = (parse_field_type(f.type) or ("", ""))[1]
    if f.type.startswith(("enum:", "ref:")):
        return raw
    try:
        if scalar == "integer":
            return int(raw)
        if scalar == "boolean":
            if raw not in ("true", "false"):
                raise ValueError("must be true or false")
            return raw == "true"
    except ValueError as e:
        raise ValueError(str(e) or "invalid value") from e
    return raw


def _restore(f: FieldDef, value: Any) -> Any:
    """Stored computed value for the column (decimals/datetimes come back as such)."""
    if value is None:
        return None
    scalar = (parse_field_type(f.type) or ("", ""))[1]
    if scalar == "uuid":
        return uuid.UUID(str(value))
    if scalar == "date" and isinstance(value, datetime):
        return value.date()
    if scalar == "decimal":
        return Decimal(str(value)).quantize(Decimal("0.00000001"))
    return value
