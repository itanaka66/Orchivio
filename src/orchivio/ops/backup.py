"""Logical backup and restore of the platform database and artifacts (FR-044).

A backup is a zip of one JSON-lines file per table (in foreign-key order), the artifact files, and a
manifest with row counts, SHA-256 hashes and the schema revision. It is portable (SQLite and
PostgreSQL) and verifiable. It complements, not replaces, a database-native strategy such as
``pg_dump``/WAL archiving for large installations; see docs/OPERATIONS.md.

Restore is all-or-nothing: it requires an *empty* database already migrated to the backup's
revision, inserts everything in one transaction, and only then writes artifacts.

A backup contains password hashes, token hashes, every model and the audit log. Treat it as
confidential: encrypt it at rest and restrict access. Sessions are excluded by default so that a
restored system forces a fresh login. Preview/runtime data is synthetic and rebuildable and is not
included.
"""

from __future__ import annotations

import hashlib
import io
import json
import uuid
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import Column, DateTime, Engine, Table, Uuid, func, insert, select, text, update

from orchivio import __version__
from orchivio.db.models import Base
from orchivio.ports import LocalArtifactStore
from orchivio.ports.storage import validate_key

FORMAT = "orchivio.backup/1"
MANIFEST = "manifest.json"
DEFAULT_EXCLUDE = frozenset({"sessions"})
_CHUNK = 1000
_EPOCH = (1980, 1, 1, 0, 0, 0)


class BackupError(Exception):
    pass


@dataclass
class RestoreReport:
    tables: dict[str, int] = field(default_factory=dict)
    artifacts: int = 0
    excluded: list[str] = field(default_factory=list)


# --- value encoding -----------------------------------------------------------------------------


def _encode(v: Any) -> Any:
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, (date, Decimal)):
        return v.isoformat() if isinstance(v, date) else str(v)
    if isinstance(v, (list, tuple)):
        return [_encode(x) for x in v]
    if isinstance(v, dict):
        return {k: _encode(x) for k, x in v.items()}
    return v


def _decode(col: Column, v: Any, dialect: str) -> Any:
    if v is None:
        return None
    if isinstance(col.type, Uuid):
        return uuid.UUID(v)
    if isinstance(col.type, DateTime):
        dt = datetime.fromisoformat(v)
        # SQLite stores naive timestamps; PostgreSQL columns are timezone-aware and the application stores UTC.
        return dt.replace(tzinfo=UTC) if dt.tzinfo is None and dialect == "postgresql" else dt
    return v  # JSON columns and the UUID-list type take plain values (the type converts)


def _tables(exclude: Iterable[str]) -> list[Table]:
    skip = set(exclude)
    return [t for t in Base.metadata.sorted_tables if t.name not in skip]


def _deferred_columns(table: Table) -> list[str]:
    """Columns of foreign keys that point 'forwards' (use_alter): filled after all rows exist."""
    return [c.name for fk in table.foreign_key_constraints if fk.use_alter for c in fk.columns]


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _artifact_files(root: Path | None) -> dict[str, Path]:
    if root is None or not root.exists():
        return {}
    out: dict[str, Path] = {}
    for p in sorted(root.rglob("*")):
        if p.is_file() and not p.is_symlink():
            key = p.relative_to(root).as_posix()
            validate_key(key)
            out[key] = p
    return out


# --- backup --------------------------------------------------------------------------------------


def backup(engine: Engine, *, artifacts_dir: Path | str | None = None, exclude: Iterable[str] = DEFAULT_EXCLUDE,
           now: datetime | None = None) -> bytes:
    """Write a backup archive and return its bytes."""
    excluded = sorted(set(exclude))
    tables = _tables(excluded)
    files: dict[str, bytes] = {}
    counts: dict[str, int] = {}
    with engine.connect() as conn:
        if engine.dialect.name == "postgresql":  # one consistent snapshot across all tables
            conn.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
        revision = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
        if revision is None:
            raise BackupError("the database has no schema revision; migrate it first")
        for t in tables:
            pk = list(t.primary_key.columns)
            lines = []
            rows = conn.execute(select(t).order_by(*pk)).mappings()
            for row in rows:
                lines.append(json.dumps({k: _encode(v) for k, v in row.items()}, ensure_ascii=False, sort_keys=True))
            counts[t.name] = len(lines)
            files[f"tables/{t.name}.jsonl"] = ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8")
    art = _artifact_files(Path(artifacts_dir) if artifacts_dir else None)
    for key, path in art.items():
        files[f"artifacts/{key}"] = path.read_bytes()
    manifest = {
        "format": FORMAT, "orchivio_version": __version__, "schema_revision": revision,
        "created_at": (now or datetime.now(UTC)).isoformat(), "excluded_tables": excluded,
        "tables": {n: {"rows": counts[n], "sha256": _sha(files[f"tables/{n}.jsonl"])} for n in counts},
        "artifacts": {k: _sha(files[f"artifacts/{k}"]) for k in art},
    }
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in {**files, MANIFEST: json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")}.items():
            info = zipfile.ZipInfo(name, _EPOCH)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            z.writestr(info, data)
    return out.getvalue()


# --- verify and restore -----------------------------------------------------------------------------


def read_backup(data: bytes) -> tuple[dict[str, Any], dict[str, bytes]]:
    """Parse and verify an archive: every table file and artifact must match the manifest."""
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise BackupError("not a valid backup archive") from e
    with z:
        raw = {i.filename: z.read(i) for i in z.infolist() if not i.is_dir()}
    if MANIFEST not in raw:
        raise BackupError("the manifest is missing")
    try:
        manifest = json.loads(raw.pop(MANIFEST))
    except ValueError as e:
        raise BackupError("the manifest is unreadable") from e
    if manifest.get("format") != FORMAT:
        raise BackupError(f"unsupported backup format {manifest.get('format')!r}")
    expected = {f"tables/{n}.jsonl": m["sha256"] for n, m in manifest["tables"].items()}
    expected |= {f"artifacts/{k}": h for k, h in manifest["artifacts"].items()}
    for name, digest in expected.items():
        if name not in raw:
            raise BackupError(f"{name} is listed in the manifest but missing")
        if _sha(raw[name]) != digest:
            raise BackupError(f"{name} does not match the manifest (corrupted or modified)")
    extra = set(raw) - set(expected)
    if extra:
        raise BackupError(f"unexpected file in the archive: {sorted(extra)[0]}")
    return manifest, raw


def restore(data: bytes, engine: Engine, *, artifacts_dir: Path | str | None = None) -> RestoreReport:
    """Restore into an empty, migrated database. Raises ``BackupError`` and changes nothing on failure."""
    manifest, raw = read_backup(data)
    known = {t.name: t for t in Base.metadata.sorted_tables}
    unknown = sorted(set(manifest["tables"]) - set(known))
    if unknown:
        raise BackupError(f"the backup contains tables this version does not know: {unknown}")
    report = RestoreReport(excluded=list(manifest.get("excluded_tables", [])))
    dialect = engine.dialect.name
    ordered = [known[t.name] for t in Base.metadata.sorted_tables if t.name in manifest["tables"]]
    patches: list[tuple[Table, Any, Any, dict[str, Any]]] = []  # forward references, applied once every table is in
    with engine.begin() as conn:
        current = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
        if current != manifest["schema_revision"]:
            raise BackupError(f"the target database is at revision {current!r} but the backup is from "
                              f"{manifest['schema_revision']!r}; migrate the target to that revision first")
        for t in ordered:
            if conn.execute(select(func.count()).select_from(t)).scalar_one():
                raise BackupError(f"the target database is not empty ({t.name} has rows); restore needs an empty database")
        for t in ordered:
            deferred = _deferred_columns(t)
            later: list[tuple[Any, dict[str, Any]]] = []
            batch: list[dict[str, Any]] = []
            pk = next(iter(t.primary_key.columns))
            for line in raw[f"tables/{t.name}.jsonl"].decode("utf-8").splitlines():
                rec = json.loads(line)
                values = {c.name: _decode(c, rec.get(c.name), dialect) for c in t.columns if c.name in rec}
                if deferred:
                    later.append((values[pk.name], {c: values.pop(c) for c in deferred if values.get(c) is not None}))
                    for c in deferred:
                        values[c] = None
                batch.append(values)
                if len(batch) >= _CHUNK:
                    conn.execute(insert(t), batch)
                    batch = []
            if batch:
                conn.execute(insert(t), batch)
            patches += [(t, pk, key, patch) for key, patch in later if patch]
            _resync_sequence(conn, t, dialect)
            report.tables[t.name] = manifest["tables"][t.name]["rows"]
            got = conn.execute(select(func.count()).select_from(t)).scalar_one()
            if got != report.tables[t.name]:
                raise BackupError(f"{t.name}: restored {got} rows, expected {report.tables[t.name]}")
        # PostgreSQL checks foreign keys at once, so a column pointing at a table restored later (applications ->
        # app_revisions) can only be filled in after every table has its rows.
        for t, pk, key, patch in patches:
            conn.execute(update(t).where(pk == key).values(**patch))
    if artifacts_dir is not None and manifest["artifacts"]:
        store = LocalArtifactStore(artifacts_dir)
        for key in manifest["artifacts"]:
            store.put(key, raw[f"artifacts/{key}"])
            report.artifacts += 1
    return report


def _resync_sequence(conn: Any, t: Table, dialect: str) -> None:
    """PostgreSQL: after inserting explicit ids, move the identity sequence past them."""
    if dialect != "postgresql":
        return
    pk = list(t.primary_key.columns)
    if len(pk) == 1 and pk[0].autoincrement is True and pk[0].type.python_type is int:
        conn.execute(text(f"SELECT setval(pg_get_serial_sequence('{t.name}', '{pk[0].name}'), "
                          f"COALESCE((SELECT MAX({pk[0].name}) FROM {t.name}), 1), (SELECT COUNT(*) FROM {t.name}) > 0)"))
