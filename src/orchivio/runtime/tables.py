"""Generate SQLAlchemy tables for the persistent entities of a model.

Application data lives in tables named with a per-application prefix, separate from the
platform's metadata tables (and, in production, in a separate preview/runtime database).
A virtual computed field has no column; a stored one does.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    MetaData,
    Numeric,
    String,
    Table,
    Text,
    Uuid,
)
from sqlalchemy.types import TypeEngine

from orchivio.model import AppModel, ModelIndex
from orchivio.model.schema import BUILTIN_ENTITIES, EntityDef, FieldDef, ModuleDef, parse_field_type, qualify

USER_REF_LENGTH = 64


class SchemaBuildError(ValueError):
    pass


@dataclass(frozen=True)
class EntityInfo:
    qualified: str
    module: ModuleDef
    entity: EntityDef
    table: Table
    pk: FieldDef
    stored_fields: tuple[FieldDef, ...]
    virtual_fields: tuple[FieldDef, ...]


def table_prefix(application_id: uuid.UUID) -> str:
    return f"app_{application_id.hex[:12]}__"


def _table_name(prefix: str, qualified: str) -> str:
    name = f"{prefix}{qualified.replace('.', '__')}"
    return name if len(name) <= 60 else prefix + hashlib.sha1(qualified.encode()).hexdigest()[:20]


def scalar_type(f: FieldDef, scalar: str) -> TypeEngine:
    return {
        "string": lambda: String(f.max_length or 255),
        "text": lambda: Text(),
        "integer": lambda: BigInteger(),
        "decimal": lambda: Numeric(28, 8, asdecimal=True),
        "boolean": lambda: Boolean(),
        "date": lambda: Date(),
        "datetime": lambda: DateTime(timezone=True),
        "uuid": lambda: Uuid(),
        "json": lambda: JSON(),
    }[scalar]()


class RuntimeSchema:
    """Tables and field metadata for one model, keyed by qualified entity name."""

    def __init__(self, model: AppModel, prefix: str) -> None:
        self.model = model
        self.prefix = prefix
        self.index = ModelIndex(model)
        self.metadata = MetaData()
        self.entities: dict[str, EntityInfo] = {}
        self._build()

    def _pk_column_type(self, qualified: str) -> TypeEngine:
        if qualified in BUILTIN_ENTITIES:
            return String(USER_REF_LENGTH)
        _, ent = self.index.entities[qualified]
        pk = next(f for f in ent.fields if f.primary_key)
        parsed = parse_field_type(pk.type)
        return scalar_type(pk, parsed[1]) if parsed and parsed[0] == "scalar" else String(255)

    def _build(self) -> None:
        names = {q: _table_name(self.prefix, q) for q, (_, e) in self.index.entities.items() if not e.transient}
        for q, (mod, ent) in self.index.entities.items():
            if ent.transient:
                continue
            pk = next(f for f in ent.fields if f.primary_key)
            cols: list = []
            stored, virtual = [], []
            for f in ent.fields:
                if f.computed and f.computed.mode == "virtual":
                    virtual.append(f)
                    continue
                stored.append(f)
                cols.append(self._column(mod, f, names))
            tname = names[q]
            args: list = list(cols)
            for f in stored:
                parsed = parse_field_type(f.type)
                if parsed and parsed[0] == "enum":
                    values = self.index.enums[qualify(mod.key, parsed[1] or "")]
                    quoted = ", ".join("'" + v + "'" for v in values)  # values match [a-z][a-z0-9_]*
                    args.append(CheckConstraint(f'"{f.name}" IN ({quoted})', name=_short("ck", tname, f.name)))
            for i, idx in enumerate(ent.indexes):
                args.append(Index(_short("ix", tname, str(i)), *idx.fields, unique=idx.unique))
            table = Table(tname, self.metadata, *args)
            self.entities[q] = EntityInfo(q, mod, ent, table, pk, tuple(stored), tuple(virtual))

    def _column(self, mod: ModuleDef, f: FieldDef, names: dict[str, str]) -> Column:
        parsed = parse_field_type(f.type)
        assert parsed is not None
        kind, target = parsed
        fk = None
        if kind == "scalar":
            type_ = scalar_type(f, target or "string")
        elif kind == "enum":
            type_ = String(64)
        else:
            q = self.index.resolve_entity(mod.key, target or "")
            if q is None:
                raise SchemaBuildError(f"unknown reference target {target!r}")
            type_ = self._pk_column_type(q)
            if q in names:
                _, tent = self.index.entities[q]
                tpk = next(x for x in tent.fields if x.primary_key)
                fk = ForeignKey(f"{names[q]}.{tpk.name}")
        nullable = not (f.required or f.primary_key or f.generated)
        if f.computed:  # stored computed values are filled by the runtime before insert
            nullable = True
        extra = [fk] if fk is not None else []
        return Column(f.name, type_, *extra, primary_key=f.primary_key, nullable=nullable,
                      unique=bool(f.unique and not f.primary_key))

    def info(self, qualified: str) -> EntityInfo | None:
        return self.entities.get(qualified)

    def create_all(self, engine) -> None:
        self.metadata.create_all(engine)

    def table_names(self) -> list[str]:
        return [i.table.name for i in self.entities.values()]


def _short(kind: str, table: str, suffix: str) -> str:
    raw = f"{kind}_{table}_{suffix}"
    return raw if len(raw) <= 60 else f"{kind}_{hashlib.sha1(raw.encode()).hexdigest()[:24]}"

