"""Change proposals: the single path from "suggested" to "applied"."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from orchivio.db import ModelInvalid, ProposalRepository
from orchivio.db.models import Proposal
from orchivio.db.proposals import NoChanges, NotPending, Stale
from orchivio.policy import ApprovalLevel, can_approve

from ..deps import Principal, api_error, authorize, current_principal, get_db, load_application
from ..idempotency import IdempotentRoute
from ..modelio import load_model
from ..schemas import Page

router = APIRouter(tags=["proposals"], route_class=IdempotentRoute)


class ProposalIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: dict[str, Any]
    title: str = Field(min_length=1, max_length=300)


class DecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["approve", "reject"]
    comment: str | None = Field(default=None, max_length=1000)
    acknowledge_risks: bool = False


class ProposalOut(BaseModel):
    id: uuid.UUID
    application_id: uuid.UUID
    ai_job_id: uuid.UUID | None
    base_revision_id: uuid.UUID | None
    result_revision_id: uuid.UUID | None
    source: str
    title: str
    approval_level: str
    status: str
    created_by: uuid.UUID | None
    approved_by: uuid.UUID | None
    created_at: datetime
    decided_at: datetime | None


class ProposalDetail(ProposalOut):
    diff: dict[str, Any]
    lint: list[dict[str, Any]]
    warnings: list[dict[str, Any]]
    decision: dict[str, Any] | None
    model: dict[str, Any] | None = None


def proposal_out(p: Proposal) -> ProposalOut:
    return ProposalOut(
        id=p.id, application_id=p.application_id, ai_job_id=p.ai_job_id, base_revision_id=p.base_revision_id,
        result_revision_id=p.result_revision_id, source=p.source, title=p.title,
        approval_level=ApprovalLevel(p.approval_level).name, status=p.status, created_by=p.created_by,
        approved_by=p.approved_by, created_at=p.created_at, decided_at=p.decided_at)


def proposal_detail(p: Proposal, include_model: bool = False) -> ProposalDetail:
    cs = p.change_set_json
    return ProposalDetail(**proposal_out(p).model_dump(), diff=cs.get("diff", {}), lint=cs.get("lint", []),
                          warnings=cs.get("warnings", []), decision=cs.get("decision"),
                          model=cs.get("model") if include_model else None)


def model_invalid(e: ModelInvalid):
    return api_error(422, "model_invalid", "the model has validation errors",
                     [i.as_dict() for i in e.issues if i.severity == "error"])


def _load(db: Session, principal: Principal, proposal_id: uuid.UUID, action: str):
    p = db.get(Proposal, proposal_id)
    if p is None:
        raise api_error(404, "not_found", "not found")
    app, project = load_application(db, principal, p.application_id, action)
    return p, app, project


@router.post("/applications/{app_id}/proposals", status_code=201, response_model=ProposalDetail)
def create_proposal(app_id: uuid.UUID, body: ProposalIn, principal: Principal = Depends(current_principal),
                    db: Session = Depends(get_db)) -> ProposalDetail:
    app, project = load_application(db, principal, app_id, "model.propose")
    try:
        p = ProposalRepository(db).create(app.id, load_model(body.model), source="human", title=body.title,
                                          created_by=principal.user_id, organization_id=project.organization_id)
    except ModelInvalid as e:
        raise model_invalid(e) from e
    except NoChanges as e:
        raise api_error(409, "no_changes", str(e)) from e
    db.commit()
    return proposal_detail(p)


@router.get("/applications/{app_id}/proposals", response_model=Page[ProposalOut])
def list_proposals(app_id: uuid.UUID, status: str | None = Query(None, pattern="^(pending|rejected|applied|superseded)$"),
                   limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                   principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> Page[ProposalOut]:
    app, _ = load_application(db, principal, app_id, "model.read")
    q = select(func.count()).select_from(Proposal).where(Proposal.application_id == app.id)
    if status:
        q = q.where(Proposal.status == status)
    total = db.scalar(q) or 0
    items = ProposalRepository(db).list(app.id, status, limit, offset)
    return Page(items=[proposal_out(p) for p in items], limit=limit, offset=offset, total=total)


@router.get("/proposals/{proposal_id}", response_model=ProposalDetail)
def get_proposal(proposal_id: uuid.UUID, include_model: bool = Query(False),
                 principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> ProposalDetail:
    p, _, _ = _load(db, principal, proposal_id, "model.read")
    return proposal_detail(p, include_model)


@router.post("/proposals/{proposal_id}/approval", response_model=ProposalDetail)
def decide(proposal_id: uuid.UUID, body: DecisionIn, principal: Principal = Depends(current_principal),
           db: Session = Depends(get_db)) -> ProposalDetail:
    action = "model.apply" if body.decision == "approve" else "model.propose"
    p, app, project = _load(db, principal, proposal_id, action)
    repo = ProposalRepository(db)
    org = project.organization_id
    try:
        if body.decision == "reject":
            repo.reject(p, principal.user_id, comment=body.comment, organization_id=org)
        else:
            role = authorize(db, principal, org, "model.apply", project_id=project.id)
            level = ApprovalLevel(p.approval_level)
            if not can_approve(role, level):
                raise api_error(403, "forbidden", f"approving a {level.name} change needs a higher role")
            if level >= ApprovalLevel.A3 and not body.acknowledge_risks:
                raise api_error(409, "approval_required",
                                f"this proposal is {level.name}; resend with acknowledge_risks=true",
                                {"level": level.name, "diff": p.change_set_json.get("diff")})
            repo.approve(p, principal.user_id, comment=body.comment, organization_id=org)
    except NotPending as e:
        raise api_error(409, "not_pending", str(e)) from e
    except Stale as e:
        db.commit()  # keep the 'superseded' status and its audit event
        raise api_error(409, "stale_proposal", str(e)) from e
    except ModelInvalid as e:
        db.rollback()
        raise model_invalid(e) from e
    db.commit()
    return proposal_detail(p)
