"""Apply an expand/contract migration plan against a live database (FR-097).

Near-zero-downtime strategy
----------------------------
* **Expand phase** (safe to run BEFORE the new code is deployed):
  - ``create_table``   — new tables; backwards-compatible.
  - ``add_column``     — nullable columns; existing readers ignore them.
  - ``rename_table``   — renames the table; both names are live briefly.
* **Contract phase** (run AFTER all replicas serve the new code):
  - ``drop_column``    — removes a column no longer referenced.
  - ``alter_column_type`` — casts existing values to the new type.
  - ``set_not_null``   — makes a column mandatory (data must be filled first).
  - ``narrow_length``  — shortens a ``VARCHAR`` limit (data must be trimmed first).
  - ``drop_table``     — removes an entity that no longer exists in the model.

The caller is responsible for sequencing (deploy new code between expand and contract).
"""

from __future__ import annotations

import hashlib
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

# Orchivio field type → PostgreSQL column type
_PG_TYPES: dict[str, str] = {
    "string": "TEXT",
    "text": "TEXT",
    "integer": "BIGINT",
    "decimal": "NUMERIC(28,8)",
    "boolean": "BOOLEAN",
    "date": "DATE",
    "datetime": "TIMESTAMPTZ",
    "uuid": "UUID",
    "json": "JSONB",
}


def _pg_type(type_str: str) -> str:
    return _PG_TYPES.get(type_str, "TEXT")


def _tbl(prefix: str, qualified: str) -> str:
    """Map a qualified entity name (module.Entity) to a prefixed table name."""
    name = f"{prefix}{qualified.replace('.', '__')}"
    if len(name) <= 60:
        return name
    return prefix + hashlib.sha1(qualified.encode()).hexdigest()[:20]


def run_expand(conn: Connection, plan: list[dict[str, Any]], prefix: str) -> list[str]:
    """Apply the expand phase of *plan* using *conn*.

    Returns a list of human-readable descriptions of applied operations. Operations
    already present in the schema are skipped (``IF NOT EXISTS`` / ``IF EXISTS``).
    """
    applied: list[str] = []
    for step in plan:
        if step.get("phase") != "expand":
            continue
        op: str = step["op"]
        target: str = step["target"]

        if op == "create_table":
            tbl = _tbl(prefix, target)
            fields = step.get("fields", [])
            col_defs = ['id UUID PRIMARY KEY DEFAULT gen_random_uuid()']
            for f in fields:
                null_kw = "NOT NULL" if f.get("required") else "NULL"
                col_defs.append(f'"{f["name"]}" {_pg_type(f.get("type", "text"))} {null_kw}')
            conn.execute(text(f'CREATE TABLE IF NOT EXISTS "{tbl}" ({", ".join(col_defs)})'))
            applied.append(f"create_table {tbl}")

        elif op == "add_column":
            parts = target.rsplit(".", 1)
            tbl = _tbl(prefix, parts[0])
            col = parts[1] if len(parts) == 2 else target
            null_kw = "NOT NULL" if step.get("required") else "NULL"
            # PostgreSQL 9.6+ supports IF NOT EXISTS for ADD COLUMN
            conn.execute(text(
                f'ALTER TABLE "{tbl}" ADD COLUMN IF NOT EXISTS "{col}" {_pg_type(step.get("type", "text"))} {null_kw}'
            ))
            applied.append(f"add_column {tbl}.{col}")
            # FR-034: copy existing data when a field is renamed (copy_from is set by plan_migration)
            if step.get("copy_from"):
                src = str(step["copy_from"])
                src_parts = src.rsplit(".", 1)
                old_tbl = _tbl(prefix, src_parts[0]) if len(src_parts) == 2 else tbl
                old_col = src_parts[1] if len(src_parts) == 2 else src
                conn.execute(text(f'UPDATE "{old_tbl}" SET "{col}" = "{old_col}" WHERE "{col}" IS NULL'))
                applied.append(f"copy_data {old_tbl}.{old_col} -> {tbl}.{col}")
            elif step.get("backfill_required"):
                applied.append(f"WARNING {tbl}.{col} is required but has no default; backfill before running contract")

        elif op == "rename_table":
            old_tbl = _tbl(prefix, step["from"])
            new_tbl = _tbl(prefix, target)
            # ALTER TABLE … RENAME does not support IF EXISTS; guard with a catalog check
            exists = conn.scalar(text(
                "SELECT 1 FROM information_schema.tables WHERE table_name = :t"
            ), {"t": old_tbl})
            if exists:
                conn.execute(text(f'ALTER TABLE "{old_tbl}" RENAME TO "{new_tbl}"'))
                applied.append(f"rename_table {old_tbl} → {new_tbl}")

    return applied


def run_contract(conn: Connection, plan: list[dict[str, Any]], prefix: str) -> list[str]:
    """Apply the contract phase of *plan* using *conn*.

    Contract operations are **destructive** — only run after all replicas serve
    the new model version. The caller must pass ``force_contract=True`` to the API
    endpoint; this function itself has no guard.
    """
    applied: list[str] = []
    for step in plan:
        if step.get("phase") != "contract":
            continue
        op: str = step["op"]
        target: str = step["target"]
        parts = target.split(".")
        tbl = _tbl(prefix, ".".join(parts[:2])) if len(parts) >= 2 else _tbl(prefix, target)
        col = parts[2] if len(parts) >= 3 else (parts[1] if len(parts) == 2 else None)

        if op == "drop_column" and col:
            conn.execute(text(f'ALTER TABLE "{tbl}" DROP COLUMN IF EXISTS "{col}"'))
            applied.append(f"drop_column {tbl}.{col}")

        elif op == "alter_column_type" and col:
            new_type = _pg_type(step.get("to", "text"))
            conn.execute(text(
                f'ALTER TABLE "{tbl}" ALTER COLUMN "{col}" TYPE {new_type} USING "{col}"::{new_type}'
            ))
            applied.append(f"alter_column_type {tbl}.{col}: {step.get('from')} → {step.get('to')}")

        elif op == "set_not_null" and col:
            conn.execute(text(f'ALTER TABLE "{tbl}" ALTER COLUMN "{col}" SET NOT NULL'))
            applied.append(f"set_not_null {tbl}.{col}")

        elif op == "narrow_length" and col:
            new_len = int(step["to"])
            conn.execute(text(
                f'ALTER TABLE "{tbl}" ALTER COLUMN "{col}" TYPE VARCHAR({new_len}) USING "{col}"::VARCHAR({new_len})'
            ))
            applied.append(f"narrow_length {tbl}.{col}: → {new_len}")

        elif op == "drop_table":
            tbl_name = _tbl(prefix, target)
            conn.execute(text(f'DROP TABLE IF EXISTS "{tbl_name}"'))
            applied.append(f"drop_table {tbl_name}")

    return applied
