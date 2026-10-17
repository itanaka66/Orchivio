"""CSV import and export, for end users (``/apps/{id}``) and for the Studio preview (docs/APP-SECURITY.md §7)."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.orm import Session

from orchivio.db.models import AuditEvent
from orchivio.runtime import AppData, AppUser, CsvError, DataError, export_csv, import_csv
from orchivio.runtime.csvio import DELIMITERS

from ..deps import Principal, api_error, audit, current_principal, get_db, load_application
from ..observability import request_id_var
from .appauth import Caller, _open, usable
from .preview import _open as _open_preview

router = APIRouter(tags=["csv"])

IMPORT_QUERY = "mode=all_or_nothing|skip_errors, dry_run, delimiter=, ; | tab, ignore_unknown, map.<header>=<field> ('map.Header=' ignores a column)"


async def _body(request: Request) -> bytes:
    limit = request.app.state.settings.csv_max_bytes
    if int(request.headers.get("content-length") or 0) > limit:
        raise api_error(413, "file_too_large", f"CSV files are limited to {limit // 1024} KiB")
    raw = await request.body()
    if len(raw) > limit:
        raise api_error(413, "file_too_large", f"CSV files are limited to {limit // 1024} KiB")
    return raw


def _options(request: Request, mode: str, dry_run: bool, delimiter: str, ignore_unknown: bool) -> dict[str, Any]:
    if delimiter not in DELIMITERS:
        raise api_error(422, "invalid_delimiter", f"delimiter must be one of: {', '.join(DELIMITERS)}")
    mapping = {k[4:]: (v or None) for k, v in request.query_params.items() if k.startswith("map.")}
    return {"mode": mode, "dry_run": dry_run, "delimiter": DELIMITERS[delimiter], "ignore_unknown": ignore_unknown,
            "mapping": mapping or None, "max_rows": request.app.state.settings.csv_max_rows,
            "max_bytes": request.app.state.settings.csv_max_bytes}


def _do_import(data: AppData, entity: str, user: AppUser, raw: bytes, opts: dict[str, Any]) -> dict[str, Any]:
    try:
        data.require(entity, user, "create")  # no point parsing a file the caller may not import
        result = import_csv(data, entity, user, raw, **opts)
    except CsvError as e:
        raise api_error(422, "invalid_csv", e.message, e.details) from e
    except DataError as e:
        raise api_error(e.status, e.code, e.message, e.details) from e
    if result.mode == "all_or_nothing" and result.failed and not result.dry_run:
        raise api_error(422, "import_rejected", f"{result.failed} of {result.total} rows have errors; nothing was imported",
                        result.as_dict())
    return result.as_dict()


def _do_export(data: AppData, entity: str, user: AppUser, limit: int) -> tuple[bytes, int]:
    try:
        return export_csv(data, entity, user, limit=limit)
    except DataError as e:
        raise api_error(e.status, e.code, e.message, e.details) from e


def _csv_response(content: bytes, entity: str) -> Response:
    name = entity.replace(".", "-").lower()
    return Response(content, media_type="text/csv; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="{name}.csv"'})


# --- end users --------------------------------------------------------------------------------


def _app_audit(db: Session, request: Request, c: Caller, action: str, **meta: Any) -> None:
    db.add(AuditEvent(organization_id=c.project.organization_id, actor_id=c.user.id if c.user else None, plane="application",
                      channel="api", action=action, target_type="entity", target_id=str(meta.get("entity")),
                      meta={"request_id": request_id_var.get(), "application_id": str(c.application.id), **meta}))


@router.post("/apps/{app_id}/import/{entity}")
async def app_import(app_id: uuid.UUID, entity: str, request: Request, mode: str = Query("all_or_nothing"), dry_run: bool = False,
                     delimiter: str = Query(","), ignore_unknown: bool = False, c: Caller = Depends(usable),
                     db: Session = Depends(get_db)) -> dict[str, Any]:
    """Body: the CSV file (``text/csv``). Query: mode, dry_run, delimiter, ignore_unknown, map.<header>=<field>."""
    raw = await _body(request)
    opts = _options(request, mode, dry_run, delimiter, ignore_unknown)
    data = _open(request, c)
    out = _do_import(data, entity, c.runtime_user, raw, opts)
    _app_audit(db, request, c, "import.completed" if not dry_run else "import.dry_run", entity=entity, mode=mode,
               rows=out["total_rows"], imported=out["imported"], failed=out["failed"])
    db.commit()
    return out


@router.get("/apps/{app_id}/export/{entity}")
def app_export(app_id: uuid.UUID, entity: str, request: Request, c: Caller = Depends(usable), db: Session = Depends(get_db)) -> Response:
    data = _open(request, c)
    content, n = _do_export(data, entity, c.runtime_user, request.app.state.settings.csv_max_export_rows)
    _app_audit(db, request, c, "export.completed", entity=entity, rows=n)
    db.commit()
    return _csv_response(content, entity)


# --- Studio preview ---------------------------------------------------------------------------


@router.post("/applications/{app_id}/preview/import/{entity}")
async def preview_import(app_id: uuid.UUID, entity: str, request: Request, mode: str = Query("all_or_nothing"), dry_run: bool = False,
                         delimiter: str = Query(","), ignore_unknown: bool = False,
                         principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict[str, Any]:
    raw = await _body(request)
    opts = _options(request, mode, dry_run, delimiter, ignore_unknown)
    data, user = _open_preview(request, db, principal, app_id, "model.propose", request.headers.get("x-preview-user"),
                               request.headers.get("x-preview-roles"))
    out = _do_import(data, entity, user, raw, opts)
    _, project = load_application(db, principal, app_id, "model.read")
    audit(db, request, "preview.import", actor_id=principal.user_id, org_id=project.organization_id, target_type="entity", target_id=entity,
          meta={"mode": mode, "dry_run": dry_run, "rows": out["total_rows"], "imported": out["imported"], "failed": out["failed"]})
    db.commit()
    return out


@router.get("/applications/{app_id}/preview/export/{entity}")
def preview_export(app_id: uuid.UUID, entity: str, request: Request, principal: Principal = Depends(current_principal),
                   db: Session = Depends(get_db)) -> Response:
    data, user = _open_preview(request, db, principal, app_id, "model.read", request.headers.get("x-preview-user"),
                               request.headers.get("x-preview-roles"))
    content, n = _do_export(data, entity, user, request.app.state.settings.csv_max_export_rows)
    _, project = load_application(db, principal, app_id, "model.read")
    audit(db, request, "preview.export", actor_id=principal.user_id, org_id=project.organization_id, target_type="entity", target_id=entity,
          meta={"rows": n})
    db.commit()
    return _csv_response(content, entity)
