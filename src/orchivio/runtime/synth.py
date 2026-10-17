"""Deterministic, constraint-aware synthetic data (FR-128).

Values come only from the model and a seed, never from production data. Rows go through the
same validation as real writes (``AppData.seed``), so a row that cannot satisfy the model is
skipped instead of inserted.
"""

from __future__ import annotations

import random
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from orchivio.model.schema import FieldDef, parse_field_type, qualify

from .service import AppData, Conflict, Invalid
from .tables import EntityInfo, RuntimeSchema

MAX_ROWS = 200
BASE_DATE = date(2025, 1, 1)


def _order(schema: RuntimeSchema) -> list[str]:
    """Entities with their referenced entities first; cycles fall back to name order."""
    deps: dict[str, set[str]] = {}
    for q, info in schema.entities.items():
        targets = set()
        for f in info.entity.fields:
            kind, target = parse_field_type(f.type) or ("", None)
            if kind == "ref":
                t = schema.index.resolve_entity(info.module.key, target or "")
                if t in schema.entities and t != q:
                    targets.add(t)
        deps[q] = targets
    out: list[str] = []
    remaining = dict(deps)
    while remaining:
        ready = sorted(q for q, d in remaining.items() if d <= set(out))
        if not ready:
            ready = [sorted(remaining)[0]]
        for q in ready:
            out.append(q)
            remaining.pop(q)
    return out


def _string(f: FieldDef, i: int) -> str:
    suffix = f"-{i}"
    limit = f.max_length or 255
    return (f.name[: max(1, limit - len(suffix))] + suffix)[:limit]


def _value(schema: RuntimeSchema, info: EntityInfo, f: FieldDef, i: int, rng: random.Random,
           users: tuple[str, ...], ids: dict[str, list[Any]]) -> Any:
    kind, target = parse_field_type(f.type) or ("", None)
    if kind == "enum":
        return rng.choice(schema.index.enums[qualify(info.module.key, target or "")])
    if kind == "ref":
        q = schema.index.resolve_entity(info.module.key, target or "")
        if q == "system.User":
            return rng.choice(users)
        pool = ids.get(q or "", [])
        return rng.choice(pool) if pool else None
    if target in ("string",):
        return _string(f, i)
    if target == "text":
        return f"Synthetic text {i}"
    if target == "integer":
        lo = int(f.minimum) if f.minimum is not None else 0
        hi = int(f.maximum) if f.maximum is not None else lo + 1000
        if f.unique:
            v = lo + i
            return v if v <= hi else None
        return rng.randint(lo, max(lo, hi))
    if target == "decimal":
        lo = Decimal(str(f.minimum)) if f.minimum is not None else Decimal(0)
        hi = Decimal(str(f.maximum)) if f.maximum is not None else lo + Decimal(1000)
        return (lo + (hi - lo) * Decimal(rng.randint(0, 1000)) / Decimal(1000)).quantize(Decimal("0.01"))
    if target == "boolean":
        return rng.choice([True, False])
    if target == "date":
        return (BASE_DATE + timedelta(days=rng.randint(0, 364))).isoformat()
    if target == "datetime":
        return (datetime(2025, 1, 1, tzinfo=UTC) + timedelta(minutes=rng.randint(0, 525_600))).isoformat()
    if target == "uuid":
        return str(uuid.UUID(int=rng.getrandbits(128), version=4))
    if target == "json":
        return {"synthetic": True, "n": i}
    return None


def make_row(schema: RuntimeSchema, info: EntityInfo, i: int, rng: random.Random, users: tuple[str, ...],
             ids: dict[str, list[Any]], *, always_fill: bool = False) -> dict[str, Any] | None:
    """One synthetic row for ``info``, or ``None`` if a required value cannot be produced
    (for example a required reference with no existing target)."""
    row: dict[str, Any] = {}
    for f in info.stored_fields:
        if f.generated or f.computed:
            continue
        if not always_fill and not (f.required or f.primary_key) and rng.random() < 0.2:
            continue
        v = _value(schema, info, f, i, rng, users, ids)
        if v is None and (f.required or f.primary_key):
            return None
        if v is not None:
            row[f.name] = v
    return row


def generate(schema: RuntimeSchema, data: AppData, *, rows_per_entity: int = 10, seed: int = 0,
             users: tuple[str, ...] = ("preview-user",)) -> dict[str, dict[str, int]]:
    """Insert synthetic rows. Returns ``{entity: {"inserted": n, "skipped": m}}``."""
    rows_per_entity = max(0, min(rows_per_entity, MAX_ROWS))
    ids: dict[str, list[Any]] = {}
    summary: dict[str, dict[str, int]] = {}
    for q in _order(schema):
        info = schema.entities[q]
        rng = random.Random(f"{seed}:{q}")
        inserted = skipped = 0
        for i in range(rows_per_entity):
            row = make_row(schema, info, i, rng, users, ids)
            if row is None:
                skipped += 1
                continue
            try:
                stored = data.seed(q, row)
            except (Invalid, Conflict):
                skipped += 1
                continue
            ids.setdefault(q, []).append(stored[info.pk.name])
            inserted += 1
        summary[q] = {"inserted": inserted, "skipped": skipped}
    return summary
