"""The task inbox and process starts for end users (application plane, FR-111, docs/PROCESS-SPEC.md).

Same authentication as the rest of ``/apps/{app_id}``: application sessions only. A person sees the tasks assigned to
them and the tasks offered to a role they hold; nothing else, and other people's tasks look like they do not exist.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import desc, func, or_, select
from sqlalchemy.orm import Session

from orchivio.db.models import ProcessEvent, ProcessInstance, UserTask
from orchivio.process import ProcessService, ServiceError
from orchivio.process.service import aware

from ..deps import api_error, get_db, not_found, utcnow
from ..schemas import Page
from .appauth import Caller, usable

router = APIRouter(tags=["tasks"], prefix="/apps/{app_id}")


class StartIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    args: dict[str, Any] = Field(default_factory=dict)


class CompleteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    outcome: str = Field(min_length=1, max_length=31)
    result: dict[str, Any] = Field(default_factory=dict)


class DelegateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=100)


def member(c: Caller = Depends(usable)) -> Caller:
    if c.user is None:
        raise api_error(401, "unauthorized", "sign in to use the task inbox")
    return c


def service(request: Request, db: Session) -> ProcessService:
    s = request.app.state
    return ProcessService(db, s.preview_store, audit=s.audit_sink, integrations=s.integration_clients)


def failure(e: ServiceError):
    return api_error(e.status, e.code, e.message, e.details)


def task_out(t: UserTask, c: Caller | None = None, now=None) -> dict[str, Any]:
    uid = str(c.user.id) if c is not None and c.user else None
    due = aware(t.due_at)
    return {"id": str(t.id), "instance_id": str(t.instance_id), "step_id": t.step_id, "title": t.title, "status": t.status,
            "assignee_role": t.assignee_role, "assignee_user": t.assignee_user, "delegated_by": t.delegated_by,
            "mine": uid is not None and t.assignee_user == uid,
            "claimable": bool(c and t.status == "open" and t.assignee_user is None and t.assignee_role in c.roles),
            "outcomes": t.outcomes, "form": t.form, "due_at": due.isoformat() if due else None,
            "overdue": bool(due and now and t.status == "open" and due < now),
            "escalated_at": aware(t.escalated_at).isoformat() if t.escalated_at else None,
            "created_at": aware(t.created_at).isoformat(), "completed_at": aware(t.completed_at).isoformat() if t.completed_at else None,
            "outcome": t.outcome}


def _task(db: Session, c: Caller, task_id: uuid.UUID) -> UserTask:
    t = db.get(UserTask, task_id)
    if t is None or t.application_id != c.application.id:
        raise not_found("task")
    uid = str(c.user.id)  # type: ignore[union-attr]
    if not (ProcessService.visible(t, uid, c.roles) or t.completed_by == uid or t.delegated_by == uid):
        raise not_found("task")  # other people's tasks do not exist as far as you can tell
    return t


@router.get("/processes")
def startable(c: Caller = Depends(member)) -> dict[str, Any]:
    """Processes the caller's roles may start."""
    out = []
    for m in c.model.modules:
        for p in m.processes or []:
            if set(p.start_roles) & set(c.roles):
                out.append({"process": f"{m.key}.{p.key}", "description": p.description,
                            "params": [{"name": x.name, "type": x.type} for x in p.params]})
    return {"items": out}


@router.post("/processes/{process}/start", status_code=201)
def start(process: str, body: StartIn, request: Request, c: Caller = Depends(member), db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        inst = service(request, db).start(c.application, process, body.args, initiator=str(c.user.id), roles=c.roles)  # type: ignore[union-attr]
    except ServiceError as e:
        db.rollback()
        raise failure(e) from e
    db.commit()
    return {"instance_id": str(inst.id), "status": inst.status, "process": inst.process}


@router.get("/tasks", response_model=Page[dict])
def list_tasks(request: Request, scope: str = Query("all", pattern="^(mine|available|all)$"), status: str = Query("open", pattern="^(open|completed)$"),
               limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0), c: Caller = Depends(member),
               db: Session = Depends(get_db)) -> Page[Any]:
    uid = str(c.user.id)  # type: ignore[union-attr]
    mine = UserTask.assignee_user == uid
    offered = (UserTask.assignee_user.is_(None)) & (UserTask.assignee_role.in_(sorted(c.roles) or [""]))
    q = select(UserTask).where(UserTask.application_id == c.application.id, UserTask.status == status)
    if status == "completed":
        q = q.where(or_(UserTask.completed_by == uid, mine))
    else:
        q = q.where({"mine": mine, "available": offered, "all": or_(mine, offered)}[scope])
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    rows = db.scalars(q.order_by(UserTask.due_at.is_(None), UserTask.due_at, desc(UserTask.created_at), UserTask.id).limit(limit).offset(offset)).all()
    now = utcnow()
    return Page(items=[task_out(t, c, now) for t in rows], limit=limit, offset=offset, total=total)


@router.get("/tasks/{task_id}")
def get_task(task_id: uuid.UUID, c: Caller = Depends(member), db: Session = Depends(get_db)) -> dict[str, Any]:
    return task_out(_task(db, c, task_id), c, utcnow())


def _act(request: Request, db: Session, c: Caller, task_id: uuid.UUID, fn) -> dict[str, Any]:
    task = _task(db, c, task_id)
    try:
        fn(service(request, db), task)
    except ServiceError as e:
        db.rollback()
        raise failure(e) from e
    db.commit()
    db.refresh(task)
    return task_out(task, c, utcnow())


@router.post("/tasks/{task_id}/claim")
def claim(task_id: uuid.UUID, request: Request, c: Caller = Depends(member), db: Session = Depends(get_db)) -> dict[str, Any]:
    return _act(request, db, c, task_id, lambda s, t: s.claim(t, str(c.user.id), c.roles))  # type: ignore[union-attr]


@router.post("/tasks/{task_id}/release")
def release(task_id: uuid.UUID, request: Request, c: Caller = Depends(member), db: Session = Depends(get_db)) -> dict[str, Any]:
    return _act(request, db, c, task_id, lambda s, t: s.release(t, str(c.user.id)))  # type: ignore[union-attr]


@router.post("/tasks/{task_id}/delegate")
def delegate(task_id: uuid.UUID, body: DelegateIn, request: Request, c: Caller = Depends(member), db: Session = Depends(get_db)) -> dict[str, Any]:
    return _act(request, db, c, task_id, lambda s, t: s.delegate(t, str(c.user.id), c.roles, body.username, c.application.id))  # type: ignore[union-attr]


@router.post("/tasks/{task_id}/complete")
def complete(task_id: uuid.UUID, body: CompleteIn, request: Request, c: Caller = Depends(member), db: Session = Depends(get_db)) -> dict[str, Any]:
    out = _act(request, db, c, task_id, lambda s, t: s.complete(t, str(c.user.id), c.roles, body.outcome, body.result))  # type: ignore[union-attr]
    inst = db.get(ProcessInstance, uuid.UUID(out["instance_id"]))
    out["instance_status"] = inst.status if inst else None
    return out


@router.get("/process-instances/{instance_id}")
def my_instance(instance_id: uuid.UUID, c: Caller = Depends(member), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Progress of a process the caller started (status and event trail; the variables stay private to builders)."""
    inst = db.get(ProcessInstance, instance_id)
    if inst is None or inst.application_id != c.application.id or inst.initiator != str(c.user.id):  # type: ignore[union-attr]
        raise not_found("process instance")
    events = db.scalars(select(ProcessEvent).where(ProcessEvent.instance_id == inst.id).order_by(ProcessEvent.id)).all()
    return {"id": str(inst.id), "process": inst.process, "status": inst.status,
            "events": [{"at": aware(e.at).isoformat(), "type": e.type, "step_id": e.step_id} for e in events]}
