"""Branch management and three-way merge (docs/MERGE.md, R0.4)."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from orchivio.db import BranchError, MergeOutcome, NotFound, RevisionRepository
from orchivio.db.models import Branch

from ..deps import Principal, api_error, authorize, current_principal, get_db, load_application, not_found, utcnow
from ..idempotency import IdempotentRoute

router = APIRouter(tags=["branches"], route_class=IdempotentRoute)

_BRANCH_ERRORS: dict[str, int] = {
    "invalid_branch_name": 422,
    "branch_exists": 409,
    "no_revision": 409,
    "main_protected": 422,
    "same_branch": 422,
    "no_common_ancestor": 422,
    "branch_error": 422,
}


def _branch_fail(e: BranchError) -> None:
    status = _BRANCH_ERRORS.get(e.code, 422)
    raise api_error(status, e.code, str(e))


def _branch_out(b: Branch) -> dict[str, Any]:
    return {
        "id": str(b.id),
        "name": b.name,
        "status": b.status,
        "head_revision_id": str(b.head_revision_id) if b.head_revision_id else None,
        "created_at": b.created_at.isoformat(),
    }


def _conflict_out(c: Any) -> dict[str, Any]:
    return {
        "id": str(c.id) if c.id else None,
        "element": c.node,
        "kind": c.kind,
        "path": c.message,
        "message": c.message,
    }


def _outcome_out(o: MergeOutcome) -> dict[str, Any]:
    return {
        "status": o.status,
        "revision_no": o.revision.revision_no if o.revision else None,
        "conflicts_resolved": len(o.all_conflicts),
        "level": None,
        "changes": [{"kind": c.get("kind"), "path": c.get("path"), "summary": c.get("summary")} for c in ([] if not o.diff else _diff_changes(o.diff))],
    }


def _diff_changes(diff: Any) -> list[dict[str, Any]]:
    return [{"kind": c.kind, "path": f"{c.element_type}.{c.name}", "summary": f"{c.kind} {c.element_type} {c.name!r}"} for c in (diff.changes or [])][:50]


class BranchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=61)
    from_revision_id: uuid.UUID | None = None


class MergeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    into: str = Field(default="main", min_length=1, max_length=61)
    dry_run: bool = False
    resolutions: dict[str, str] = Field(default_factory=dict)
    acknowledge_risks: bool = False


@router.get("/applications/{app_id}/branches")
def list_branches(app_id: uuid.UUID, principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    app, project = load_application(db, principal, app_id, "model.read")
    repo = RevisionRepository(db)
    return [_branch_out(b) for b in repo.branches(app.id)]


@router.post("/applications/{app_id}/branches", status_code=201)
def create_branch(app_id: uuid.UUID, body: BranchIn, principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict[str, Any]:
    app, project = load_application(db, principal, app_id, "model.propose")
    repo = RevisionRepository(db)
    try:
        branch = repo.create_branch(app.id, body.name, from_revision_id=body.from_revision_id,
                                    created_by=principal.user_id, organization_id=project.organization_id)
        db.commit()
    except BranchError as e:
        _branch_fail(e)
    except NotFound:
        raise not_found("revision")
    return _branch_out(branch)


@router.delete("/branches/{branch_id}", status_code=204)
def abandon_branch(branch_id: uuid.UUID, principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> None:
    branch = db.get(Branch, branch_id)
    if branch is None:
        raise not_found("branch")
    app, project = load_application(db, principal, branch.application_id, "model.propose")
    repo = RevisionRepository(db)
    try:
        repo.abandon_branch(branch, by=principal.user_id, organization_id=project.organization_id)
        db.commit()
    except BranchError as e:
        _branch_fail(e)


@router.post("/branches/{branch_id}/merge")
def merge_branch(branch_id: uuid.UUID, body: MergeIn, principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict[str, Any]:
    branch = db.get(Branch, branch_id)
    if branch is None:
        raise not_found("branch")
    app, project = load_application(db, principal, branch.application_id, "model.propose")
    repo = RevisionRepository(db)

    def _require_acknowledged(diff: Any) -> None:
        if not body.acknowledge_risks:
            raise api_error(409, "acknowledge_required", "this merge includes A3-level changes; set acknowledge_risks to proceed")

    try:
        outcome = repo.merge_branches(
            app.id, source=branch.name, target=body.into,
            resolutions=body.resolutions or None,
            dry_run=body.dry_run,
            created_by=principal.user_id,
            organization_id=project.organization_id,
            require_acknowledged=_require_acknowledged,
        )
        if not body.dry_run and outcome.status == "merged":
            db.commit()
    except BranchError as e:
        _branch_fail(e)
    except NotFound:
        raise not_found("branch")

    if outcome.status in ("conflicts", "invalid") and outcome.conflicts:
        raise api_error(409, "merge_conflicts", "resolve conflicts before merging",
                        [_conflict_out(c) for c in outcome.conflicts])

    return _outcome_out(outcome)
