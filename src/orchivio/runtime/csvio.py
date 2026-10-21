"""CSV import and export for generated applications (FR-089, FR-074).

Import is not a back door: every row goes through ``AppData.create`` as the calling user, so role and row-level
access, field-level write access, validation, uniqueness, hooks, audit and business events all apply exactly as for
an API call. The file is validated up front (encoding, size, header, columns), then rows are created in one of
three modes:

* ``all_or_nothing`` (default): every row is tried inside one transaction; if any row fails nothing is kept and every
  error is reported, so one pass shows everything that needs fixing.
* ``skip_errors``: each row is its own transaction; good rows are kept, failed rows are reported.
* ``dry_run`` (a flag, combinable with either mode): run everything, then roll back. Duplicates *within* the file are
  caught because later rows see earlier ones.

Export writes only the fields the caller may read, needs the separate ``export`` permission, is audited by the data
service, and neutralises spreadsheet formula injection (cells starting with = + - @ are prefixed with a quote).
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from orchivio.model.schema import FieldDef, parse_field_type

from .service import AppData, AppUser, DataError, Invalid

MAX_BYTES = 2 * 1024 * 1024
MAX_ROWS = 5000
MAX_COLUMNS = 100
MAX_REPORTED_ERRORS = 100
MODES = ("all_or_nothing", "skip_errors", "upsert")
DELIMITERS = {",": ",", ";": ";", "tab": "\t", "|": "|"}
_TRUE, _FALSE = {"true", "1", "yes", "y", "t"}, {"false", "0", "no", "n", "f"}


class CsvError(Exception):
    """The file as a whole cannot be imported (as opposed to individual rows failing)."""

    def __init__(self, message: str, details: Any = None) -> None:
        super().__init__(message)
        self.message, self.details = message, details


@dataclass
class RowError:
    row: int  # 1-based among data rows
    line: int  # line in the file
    errors: list[dict[str, str]]

    def as_dict(self) -> dict[str, Any]:
        return {"row": self.row, "line": self.line, "errors": self.errors}


@dataclass
class ImportResult:
    mode: str
    dry_run: bool
    total: int = 0
    imported: int = 0  # rows that were created (or would be, in a dry run)
    failed: int = 0
    committed: bool = False
    columns: dict[str, str] = field(default_factory=dict)  # header -> field
    errors: list[RowError] = field(default_factory=list)
    errors_truncated: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"mode": self.mode, "dry_run": self.dry_run, "total_rows": self.total, "imported": self.imported,
                "failed": self.failed, "committed": self.committed, "columns": self.columns,
                "errors": [e.as_dict() for e in self.errors], "errors_truncated": self.errors_truncated}


class _Abort(Exception):
    pass


def _decode(content: bytes | str, max_bytes: int) -> str:
    if isinstance(content, str):
        return content
    if len(content) > max_bytes:
        raise CsvError(f"the file is larger than {max_bytes // 1024} KiB")
    try:
        return content.decode("utf-8-sig")  # Excel writes a BOM
    except UnicodeDecodeError as e:
        raise CsvError("the file must be UTF-8 text") from e


def _from_text(data: AppData, mod_key: str, f: FieldDef, text: str) -> Any:
    """A CSV cell as the JSON-ish value the data service expects. Raises ``ValueError`` for impossible values."""
    kind, target = parse_field_type(f.type) or ("", None)
    if kind == "ref":
        q = data.schema.index.resolve_entity(mod_key, target or "")
        info = data.schema.info(q or "")
        return _from_text(data, info.module.key, info.pk, text) if info is not None else text
    if kind == "enum":
        return text
    scalar = target or "string"
    if scalar in ("string", "text"):
        return text
    text = text.strip()
    if scalar == "integer":
        try:
            return int(text)
        except ValueError as e:
            raise ValueError("must be a whole number") from e
    if scalar == "boolean":
        if text.lower() in _TRUE:
            return True
        if text.lower() in _FALSE:
            return False
        raise ValueError("must be true or false")
    if scalar == "json":
        try:
            return json.loads(text)
        except ValueError as e:
            raise ValueError("must be valid JSON") from e
    return text  # decimal, uuid, date, datetime: parsed and range-checked by the data service


def _map_columns(data: AppData, entity: str, user: AppUser, headers: list[str], mapping: Mapping[str, str | None] | None,
                 ignore_unknown: bool) -> dict[int, FieldDef]:
    info = data._info(entity)
    writable = data.writable_fields(entity, user, "create")
    fields = {f.name: f for f in info.entity.fields}
    by_label = {(f.display_name or "").lower(): f for f in info.entity.fields if f.display_name}
    problems: list[dict[str, str]] = []
    chosen: dict[int, FieldDef] = {}
    seen: dict[str, str] = {}
    for i, raw in enumerate(headers):
        h = raw.strip()
        if not h:
            problems.append({"column": str(i + 1), "message": "the header is empty"})
            continue
        if mapping is not None and h in mapping:
            target = mapping[h]
            if target is None:
                continue  # explicitly ignored
            f = fields.get(target)
        else:
            f = fields.get(h.lower()) or by_label.get(h.lower())
        usable = f is not None and (writable is None or f.name in writable)
        if not usable:
            if not ignore_unknown:
                problems.append({"column": h, "message": "no field with this name that you may import"})
            continue
        if f.generated or f.computed:
            problems.append({"column": h, "message": "this field is set by the system and cannot be imported"})
            continue
        if f.name in seen:
            problems.append({"column": h, "message": f"maps to '{f.name}' like column '{seen[f.name]}'"})
            continue
        seen[f.name] = h
        chosen[i] = f
    if problems:
        raise CsvError("the file's columns cannot be imported", problems)
    if not chosen:
        raise CsvError("no column maps to a field of this entity")
    return chosen


def import_csv(data: AppData, entity: str, user: AppUser, content: bytes | str, *, mapping: Mapping[str, str | None] | None = None,
               mode: str = "all_or_nothing", dry_run: bool = False, delimiter: str = ",", ignore_unknown: bool = False,
               key_field: str | None = None, max_rows: int = MAX_ROWS, max_bytes: int = MAX_BYTES) -> ImportResult:
    if mode not in MODES:
        raise CsvError(f"mode must be one of: {', '.join(MODES)}")
    if len(delimiter) != 1:
        raise CsvError("the delimiter must be a single character")
    text = _decode(content, max_bytes)
    if "\x00" in text:
        raise CsvError("the file contains NUL bytes; it is not a text CSV")
    info = data._info(entity)  # unknown entity -> 404 before touching the file
    rows_iter = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter, strict=True)
    try:
        headers = next(rows_iter)
    except StopIteration:
        raise CsvError("the file is empty") from None
    except csv.Error as e:
        raise CsvError(f"the header cannot be read: {e}") from e
    if len(headers) > MAX_COLUMNS:
        raise CsvError(f"more than {MAX_COLUMNS} columns")
    columns = _map_columns(data, entity, user, headers, mapping, ignore_unknown)
    result = ImportResult(mode, dry_run, columns={headers[i].strip(): f.name for i, f in columns.items()})

    parsed: list[tuple[int, int, dict[str, Any], list[dict[str, str]]]] = []  # row, line, values, cell errors
    try:
        for cells in rows_iter:
            line = rows_iter.line_num
            if not any(c.strip() for c in cells):
                continue  # blank line
            result.total += 1
            if result.total > max_rows:
                raise CsvError(f"more than {max_rows} rows; split the file")
            values: dict[str, Any] = {}
            errs: list[dict[str, str]] = []
            if len(cells) > len(headers):
                errs.append({"field": "", "message": f"{len(cells)} cells but only {len(headers)} columns"})
            for i, f in columns.items():
                cell = cells[i] if i < len(cells) else ""
                if cell.strip() == "":
                    continue  # empty = not supplied: defaults apply, required fields report "is required"
                try:
                    values[f.name] = _from_text(data, info.module.key, f, cell)
                except ValueError as e:
                    errs.append({"field": f.name, "message": str(e)})
            parsed.append((result.total, line, values, errs))
    except csv.Error as e:
        raise CsvError(f"the file is not valid CSV near line {rows_iter.line_num}: {e}") from e
    if result.total == 0:
        raise CsvError("the file has a header but no rows")

    def attempt(row: int, line: int, values: dict[str, Any], cell_errors: list[dict[str, str]]) -> bool:
        errs = list(cell_errors)
        if not errs:
            try:
                if mode == "upsert" and key_field and key_field in values:
                    existing = data.list(entity, user, filters={key_field: values[key_field]}, limit=2, offset=0)
                    matched = existing["items"]
                    if len(matched) > 1:
                        errs = [{"field": key_field, "message": f"multiple rows match this key value"}]
                    elif len(matched) == 1:
                        pk_name = data._info(entity).pk.name
                        data.update(entity, user, matched[0][pk_name], values)
                        return True
                    else:
                        data.create(entity, user, values)
                        return True
                else:
                    data.create(entity, user, values)
                    return True
            except Invalid as e:
                errs = list(e.details) if isinstance(e.details, list) else [{"field": "", "message": e.message}]
            except DataError as e:
                errs = [{"field": "", "message": e.message}]
        result.failed += 1
        if len(result.errors) < MAX_REPORTED_ERRORS:
            result.errors.append(RowError(row, line, errs))
        else:
            result.errors_truncated = True
        return False

    if mode == "skip_errors" and not dry_run:
        for item in parsed:
            with data.transaction():
                ok = attempt(*item)
            result.imported += ok
        result.committed = result.imported > 0
        return result
    try:
        with data.transaction():
            for item in parsed:
                result.imported += attempt(*item)
            if dry_run or (mode in ("all_or_nothing", "upsert") and result.failed):
                raise _Abort
    except _Abort:
        if mode in ("all_or_nothing", "upsert") and result.failed:
            result.imported = 0  # nothing was kept
        return result
    result.committed = True
    return result


def _safe(value: Any) -> str:
    s = "" if value is None else (json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value))
    if isinstance(value, bool):
        s = "true" if value else "false"
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


def export_csv(data: AppData, entity: str, user: AppUser, *, limit: int = 10_000) -> tuple[bytes, int]:
    """CSV bytes (UTF-8 with BOM, for Excel) and the row count."""
    rows = data.export_rows(entity, user, limit=limit)
    info = data._info(entity)
    visible = data.readable_fields(entity, user)
    names = [f.name for f in info.entity.fields if visible is None or f.name in visible]
    out = io.StringIO(newline="")
    w = csv.writer(out, lineterminator="\r\n")
    w.writerow(names)
    for r in rows:
        w.writerow([_safe(r.get(n)) for n in names])
    return b"\xef\xbb\xbf" + out.getvalue().encode("utf-8"), len(rows)


__all__ = ["CsvError", "ImportResult", "RowError", "export_csv", "import_csv"]
