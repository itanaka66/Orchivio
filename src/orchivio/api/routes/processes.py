"""Process monitoring and control for builders and admins (Studio side, docs/PROCESS-SPEC.md)."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Header, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from orchivio.db.models import ProcessEvent, ProcessInstance, UserTask
from orchivio.process import ServiceError
from orchivio.process.service import aware

from ..deps import (
    Principal,
    current_principal,
    get_db,
    load_application,
    not_found,
    utcnow,
)
from ..schemas import Page
from .tasks import failure, service, task_out

router = APIRouter(tags=["processes"])


class StartIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    args: dict[str, Any] = Field(default_factory=dict)


def instance_out(i: ProcessInstance, detail: bool = False) -> dict[str, Any]:
    out = {"id": str(i.id), "application_id": str(i.application_id), "process": i.process, "status": i.status, "initiator": i.initiator,
           "error": i.error, "started_at": aware(i.started_at).isoformat(), "finished_at": aware(i.finished_at).isoformat() if i.finished_at else None,
           "next_wake_at": aware(i.next_wake_at).isoformat() if i.next_wake_at else None, "revision_id": str(i.revision_id)}
    if detail:
        st = i.state
        out["variables"] = st.get("variables", {})
        out["tokens"] = st.get("tokens", [])
    return out


def _load(db: Session, principal: Principal, instance_id: uuid.UUID, action: str):
    inst = db.get(ProcessInstance, instance_id)
    if inst is None:
        raise not_found("process instance")
    app, project = load_application(db, principal, inst.application_id, action)
    return inst, app, project


@router.post("/applications/{app_id}/processes/{process}/start", status_code=201)
def start_in_preview(app_id: uuid.UUID, process: str, body: StartIn, request: Request,
                     x_preview_user: str | None = Header(default=None, alias="X-Preview-User"),
                     principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Start an instance as a builder, for testing. The initiator is the simulated user (default: the Studio account)."""
    app, project = load_application(db, principal, app_id, "model.propose")
    initiator = (x_preview_user or f"studio:{principal.user_id}").strip()[:64]
    try:
        inst = service(request, db).start(app, process, body.args, initiator=initiator, roles=None, actor=f"studio:{principal.user_id}")
    except ServiceError as e:
        db.rollback()
        raise failure(e) from e
    db.commit()
    return instance_out(inst)


@router.get("/applications/{app_id}/process-instances", response_model=Page[dict])
def list_instances(app_id: uuid.UUID, status: str | None = Query(None, pattern="^(running|completed|failed|cancelled)$"),
                   process: str | None = Query(None, max_length=130), limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                   principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> Page[Any]:
    app, _ = load_application(db, principal, app_id, "model.read")
    q = select(ProcessInstance).where(ProcessInstance.application_id == app.id)
    if status:
        q = q.where(ProcessInstance.status == status)
    if process:
        q = q.where(ProcessInstance.process == process)
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    rows = db.scalars(q.order_by(desc(ProcessInstance.started_at), ProcessInstance.id).limit(limit).offset(offset)).all()
    return Page(items=[instance_out(i) for i in rows], limit=limit, offset=offset, total=total)


@router.get("/process-instances/{instance_id}")
def get_instance(instance_id: uuid.UUID, principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict[str, Any]:
    inst, _, _ = _load(db, principal, instance_id, "model.read")
    now = utcnow()
    tasks = db.scalars(select(UserTask).where(UserTask.instance_id == inst.id).order_by(UserTask.created_at, UserTask.id)).all()
    events = db.scalars(select(ProcessEvent).where(ProcessEvent.instance_id == inst.id).order_by(ProcessEvent.id)).all()
    return {**instance_out(inst, detail=True), "tasks": [task_out(t, None, now) for t in tasks],
            "events": [{"at": aware(e.at).isoformat(), "type": e.type, "step_id": e.step_id, "actor": e.actor, "data": e.data} for e in events]}


@router.post("/process-instances/{instance_id}/cancel")
def cancel(instance_id: uuid.UUID, request: Request, principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict[str, Any]:
    inst, _, _ = _load(db, principal, instance_id, "model.propose")
    try:
        service(request, db).cancel(inst, f"studio:{principal.user_id}")
    except ServiceError as e:
        db.rollback()
        raise failure(e) from e
    db.commit()
    db.refresh(inst)
    return instance_out(inst)


@router.post("/process-instances/{instance_id}/retry")
def retry(instance_id: uuid.UUID, request: Request, principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Run a failed step (for example a system task whose flow failed) again."""
    inst, _, _ = _load(db, principal, instance_id, "model.propose")
    try:
        service(request, db).retry(inst, f"studio:{principal.user_id}")
    except ServiceError as e:
        db.rollback()
        raise failure(e) from e
    db.commit()
    db.refresh(inst)
    return instance_out(inst)


@router.get("/applications/{app_id}/user-tasks", response_model=Page[dict])
def all_tasks(app_id: uuid.UUID, status: str | None = Query(None, pattern="^(open|completed|cancelled)$"), limit: int = Query(50, ge=1, le=200),
              offset: int = Query(0, ge=0), principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> Page[Any]:
    """Every task of the application, for monitoring (bottlenecks, overdue and escalated work)."""
    app, _ = load_application(db, principal, app_id, "model.read")
    q = select(UserTask).where(UserTask.application_id == app.id)
    if status:
        q = q.where(UserTask.status == status)
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    rows = db.scalars(q.order_by(desc(UserTask.created_at), UserTask.id).limit(limit).offset(offset)).all()
    now = utcnow()
    return Page(items=[task_out(t, None, now) for t in rows], limit=limit, offset=offset, total=total)
