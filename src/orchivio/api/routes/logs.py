"""Application log viewer and AI log analysis (FR-123).

Lines come from ``orchivio.api.app_log`` (background jobs, webhook deliveries, integration calls): fixed messages and
allow-listed scalar fields, never record data. Reading needs ``logs.read`` (editors and up) on the application's organization.
Analysis sends only the selected lines (ids, time, level, source, message, fields) to the provider, so it follows the same
consent and budget rules as every AI call, and the answer is kept only if it cites lines that were actually sent.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchivio.ai import AIBuilder, BuilderError
from orchivio.db.models import AppLog
from orchivio.db.proposals import finish_ai_job, start_ai_job
from orchivio.llm import LLMError

from ..deps import Principal, api_error, current_principal, get_db, load_application
from .ai import _guard, _provider_failed, _usage

router = APIRouter(tags=["logs"])

LEVELS = ("debug", "info", "warning", "error")
MAX_PAGE = 500
MAX_ANALYZED = 200


def log_out(r: AppLog) -> dict[str, Any]:
    return {"id": r.id, "time": r.created_at.isoformat(), "level": r.level, "source": r.source, "message": r.message,
            "request_id": r.request_id, "trace_id": r.trace_id, "fields": r.fields}


def _query(app_id: uuid.UUID, level: str, source: str | None, q: str | None, since: datetime | None, until: datetime | None):
    stmt = select(AppLog).where(AppLog.application_id == app_id, AppLog.level.in_(LEVELS[LEVELS.index(level):]))
    if source:
        stmt = stmt.where(AppLog.source == source)
    if q:
        stmt = stmt.where(AppLog.message.contains(q, autoescape=True))
    if since:
        stmt = stmt.where(AppLog.created_at >= since)
    if until:
        stmt = stmt.where(AppLog.created_at < until)
    return stmt


@router.get("/applications/{app_id}/logs")
def list_logs(app_id: uuid.UUID, level: str = Query("info", pattern="^(debug|info|warning|error)$"),
              source: str | None = Query(None, max_length=24), q: str | None = Query(None, max_length=100),
              since: datetime | None = None, until: datetime | None = None, limit: int = Query(100, ge=1, le=MAX_PAGE),
              before_id: int | None = Query(None, ge=1), principal: Principal = Depends(current_principal),
              db: Session = Depends(get_db)) -> dict[str, Any]:
    """Newest first. Page with ``before_id`` = the last id of the previous page."""
    load_application(db, principal, app_id, "logs.read")
    stmt = _query(app_id, level, source, q, since, until)
    if before_id is not None:
        stmt = stmt.where(AppLog.id < before_id)
    rows = list(db.scalars(stmt.order_by(AppLog.id.desc()).limit(limit + 1)))
    more = len(rows) > limit
    rows = rows[:limit]
    return {"items": [log_out(r) for r in rows], "next_before_id": rows[-1].id if more and rows else None}


class AnalyzeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    minutes: int = Field(default=60, ge=1, le=1440)
    level: str = Field(default="warning", pattern="^(debug|info|warning|error)$")
    source: str | None = Field(default=None, max_length=24)
    question: str | None = Field(default=None, max_length=1000)


@router.post("/applications/{app_id}/logs/analyze")
def analyze_logs(app_id: uuid.UUID, body: AnalyzeIn, request: Request, principal: Principal = Depends(current_principal),
                 db: Session = Depends(get_db)) -> dict[str, Any]:
    """Ask the AI what a recent window of log lines shows. Read-only (A0); findings cite line ids, unknown ids are dropped."""
    provider, app, project = _guard(request, db, principal, app_id, "logs.read")
    since = datetime.now(UTC) - timedelta(minutes=body.minutes)
    rows = list(db.scalars(_query(app.id, body.level, body.source, None, since, None).order_by(AppLog.id.desc()).limit(MAX_ANALYZED)))
    if not rows:
        raise api_error(409, "no_logs", "there are no log lines in that window")
    rows.reverse()
    entries = [{"id": r.id, "time": r.created_at.isoformat(), "level": r.level, "source": r.source, "message": r.message, **r.fields}
               for r in rows]
    job = start_ai_job(db, project_id=project.id, application_id=app.id, type="analyze_logs", created_by=principal.user_id,
                       input_text=f"logs:{rows[0].id}-{rows[-1].id}:{body.question or ''}", template="analyze_logs@1")
    try:
        r = AIBuilder(provider).analyze_logs(entries, body.question)
    except BuilderError as e:
        finish_ai_job(job, ok=False, usage=_usage(e.usage, e.attempts), error=str(e))
        db.commit()
        raise api_error(422, "ai_output_invalid", "the AI did not produce a usable analysis") from e
    except LLMError as e:
        finish_ai_job(job, ok=False, error=str(e))
        db.commit()
        raise _provider_failed(e) from e
    usage = _usage(r.usage, r.attempts)
    finish_ai_job(job, ok=True, provider=r.provider, model=r.model_name, usage=usage)
    db.commit()
    return {"job_id": str(job.id), "lines_analyzed": len(rows), "summary": r.analysis.summary,
            "findings": [f.model_dump() for f in r.analysis.findings], "dropped_findings": r.dropped_findings, "usage": usage,
            "note": "AI-generated; every finding cites line ids that were sent, but the explanation itself is not verified."}
