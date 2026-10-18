"""Scheduled jobs and the background queue (docs/OPERATIONS.md, FR-055/056)."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from orchivio.db import to_model
from orchivio.db.models import AppRevision, JobRun
from orchivio.jobs import queue, scheduled_flows
from orchivio.model.cron import CronError, parse_cron

from ..deps import (
    Principal,
    api_error,
    audit,
    current_principal,
    get_db,
    load_application,
    not_found,
)
from ..schemas import Page

router = APIRouter(tags=["jobs"])


class JobIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    flow: str = Field(min_length=1, max_length=130)
    args: dict[str, str | int | float | bool | None] = Field(default_factory=dict, max_length=30)
    run_as_role: str | None = Field(default=None, max_length=64)


def job_out(j: JobRun) -> dict[str, Any]:
    def ts(d):
        return queue.aware(d).isoformat() if d else None

    return {"id": str(j.id), "application_id": str(j.application_id), "flow": j.flow, "args": j.args,
            "run_as_role": j.run_as_role, "trigger": j.trigger, "scheduled_for": ts(j.scheduled_for), "status": j.status,
            "attempt": j.attempt, "max_attempts": j.max_attempts, "available_at": ts(j.available_at),
            "started_at": ts(j.started_at), "finished_at": ts(j.finished_at), "error": j.error, "result": j.result,
            "model_hash": j.model_hash, "created_at": ts(j.created_at)}


def _current_model(db: Session, app):
    if app.current_revision_id is None:
        raise api_error(409, "no_revision", "the application has no revision yet")
    return to_model(db.get(AppRevision, app.current_revision_id))


def _load_job(db: Session, principal: Principal, job_id: uuid.UUID, action: str):
    job = db.get(JobRun, job_id)
    if job is None:
        raise not_found("job")
    app, project = load_application(db, principal, job.application_id, action)
    return job, app, project


@router.get("/applications/{app_id}/schedules")
def list_schedules(app_id: uuid.UUID, principal: Principal = Depends(current_principal),
                   db: Session = Depends(get_db)) -> dict[str, Any]:
    app, _ = load_application(db, principal, app_id, "model.read")
    if app.current_revision_id is None:
        return {"items": []}
    now = queue.utcnow()
    items = []
    for flow, s in scheduled_flows(_current_model(db, app)):
        try:
            nxt = parse_cron(s.cron).next_after(now).isoformat() if s.enabled else None
        except CronError:
            nxt = None
        last = db.scalar(select(JobRun).where(JobRun.application_id == app.id, JobRun.flow == flow, JobRun.trigger == "schedule")
                         .order_by(desc(JobRun.created_at)).limit(1))
        items.append({"flow": flow, "cron": s.cron, "enabled": s.enabled, "run_as_role": s.run_as_role,
                      "missed_runs": s.missed_runs, "max_attempts": s.max_attempts, "backoff_seconds": s.backoff_seconds,
                      "next_run_at": nxt, "last_run": job_out(last) if last else None})
    return {"items": items}


@router.post("/applications/{app_id}/jobs", status_code=201)
def enqueue_job(app_id: uuid.UUID, body: JobIn, request: Request, principal: Principal = Depends(current_principal),
                db: Session = Depends(get_db)) -> dict[str, Any]:
    """Queue a run of a server flow now. Runs under the given application role (or the schedule's)."""
    app, project = load_application(db, principal, app_id, "model.propose")
    model = _current_model(db, app)
    q = body.flow if "." in body.flow else f"{model.modules[0].key}.{body.flow}"
    flow = next((f for m in model.modules for f in m.flows if f"{m.key}.{f.key}" == q), None)
    if flow is None:
        raise api_error(404, "unknown_flow", "unknown flow")
    if flow.kind != "server":
        raise api_error(422, "not_runnable", "only server flows can be queued")
    if any(q == f"{m.key}.{h}" for m in model.modules for e in m.entities for h in e.hooks.values()):
        raise api_error(422, "not_runnable", "this flow is an entity hook; it runs when its event happens")
    sched = flow.schedule
    role = body.run_as_role or (sched.run_as_role if sched else None)
    if role is None:
        raise api_error(422, "role_required", "give run_as_role (the flow has no schedule to take it from)")
    if role not in model.security.roles:
        raise api_error(422, "unknown_role", "the model does not define this role", {"defined": model.security.roles})
    args = body.args if body.args or sched is None else dict(sched.args)
    job_id = queue.enqueue(db, application_id=app.id, flow=q, args=args, run_as_role=role, trigger="manual",
                           max_attempts=sched.max_attempts if sched else 3, backoff_seconds=sched.backoff_seconds if sched else 60,
                           created_by=principal.user_id)
    audit(db, request, "job.enqueued", actor_id=principal.user_id, org_id=project.organization_id, target_type="job_run",
          target_id=job_id, meta={"flow": q, "role": role})
    db.commit()
    return job_out(db.get(JobRun, job_id))


@router.get("/applications/{app_id}/jobs", response_model=Page[dict])
def list_jobs(app_id: uuid.UUID, status: str | None = Query(None, pattern="^(queued|running|succeeded|dead|cancelled)$"),
              limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
              principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> Page[Any]:
    app, _ = load_application(db, principal, app_id, "model.read")
    q = select(JobRun).where(JobRun.application_id == app.id)
    if status:
        q = q.where(JobRun.status == status)
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    rows = db.scalars(q.order_by(desc(JobRun.created_at), JobRun.id).limit(limit).offset(offset)).all()
    return Page(items=[job_out(j) for j in rows], limit=limit, offset=offset, total=total)


@router.get("/jobs/{job_id}")
def get_job(job_id: uuid.UUID, principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict[str, Any]:
    job, _, _ = _load_job(db, principal, job_id, "model.read")
    return job_out(job)


@router.post("/jobs/{job_id}/retry")
def retry_job(job_id: uuid.UUID, request: Request, principal: Principal = Depends(current_principal),
              db: Session = Depends(get_db)) -> dict[str, Any]:
    """Give a dead-lettered run a fresh set of attempts."""
    job, _, project = _load_job(db, principal, job_id, "model.propose")
    if not queue.requeue_dead(db, job):
        raise api_error(409, "not_dead", "only dead-lettered runs can be retried")
    audit(db, request, "job.retried", actor_id=principal.user_id, org_id=project.organization_id, target_type="job_run",
          target_id=job.id, meta={"flow": job.flow})
    db.commit()
    return job_out(job)


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: uuid.UUID, request: Request, principal: Principal = Depends(current_principal),
               db: Session = Depends(get_db)) -> dict[str, Any]:
    job, _, project = _load_job(db, principal, job_id, "model.propose")
    if not queue.cancel(db, job):
        raise api_error(409, "not_queued", "only queued runs can be cancelled")
    audit(db, request, "job.cancelled", actor_id=principal.user_id, org_id=project.organization_id, target_type="job_run",
          target_id=job.id, meta={"flow": job.flow})
    db.commit()
    return job_out(job)
