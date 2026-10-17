"""Unified change proposals and AI job records (FR-013..015, DESIGN.md sections 7.2, 9.2).

AI Builder, human editors and (later) MCP agents all submit changes through this one pipeline.
A proposal never touches the canonical model; only ``approve`` does, by creating a new
immutable revision through ``RevisionRepository``.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from orchivio.model import AppModel, ModelDiff
from orchivio.policy import ApprovalLevel

from .models import AIJob, Application, AuditEvent, Proposal
from .repository import ModelInvalid, NotFound, RevisionRepository

SOURCES = ("ai_builder", "mcp", "human")


class ProposalError(Exception):
    code = "proposal_error"


class NoChanges(ProposalError):
    code = "no_changes"


class NotPending(ProposalError):
    code = "not_pending"


class Stale(ProposalError):
    code = "stale_proposal"


def _now() -> datetime:
    return datetime.now(UTC)


def diff_json(diff: ModelDiff | None) -> dict[str, Any]:
    if diff is None:
        return {"level": ApprovalLevel.A2.name, "security_changed": False, "initial": True, "changes": []}
    return {"level": diff.level.name, "security_changed": diff.security_changed, "initial": False,
            "changes": [c.as_dict() for c in diff.changes]}


class ProposalRepository:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.revisions = RevisionRepository(session)

    def get(self, proposal_id: uuid.UUID) -> Proposal:
        p = self.session.get(Proposal, proposal_id)
        if p is None:
            raise NotFound(f"proposal {proposal_id}")
        return p

    def list(self, application_id: uuid.UUID, status: str | None = None, limit: int = 50, offset: int = 0) -> list[Proposal]:
        q = select(Proposal).where(Proposal.application_id == application_id)
        if status:
            q = q.where(Proposal.status == status)
        return list(self.session.scalars(
            q.order_by(Proposal.created_at.desc(), Proposal.id).limit(max(1, min(limit, 200))).offset(max(0, offset))))

    def create(self, application_id: uuid.UUID, model: AppModel, *, source: str, title: str,
               created_by: uuid.UUID | None, organization_id: uuid.UUID | None = None,
               ai_job_id: uuid.UUID | None = None) -> Proposal:
        """Plan ``model`` against the current revision and store it as a pending proposal.

        Raises ``ModelInvalid`` (nothing stored) or ``NoChanges``.
        """
        if source not in SOURCES:
            raise ValueError(f"unknown proposal source {source!r}")
        app = self.session.get(Application, application_id)
        if app is None:
            raise NotFound(f"application {application_id}")
        plan = self.revisions.plan(application_id, model)
        if not plan.valid:
            raise ModelInvalid(plan.issues)
        if plan.diff is not None and plan.diff.is_empty:
            raise NoChanges("the proposal does not change the current model")
        level = max(ApprovalLevel.A2, plan.diff.level) if plan.diff else ApprovalLevel.A2
        proposal = Proposal(
            application_id=application_id,
            ai_job_id=ai_job_id,
            base_revision_id=app.current_revision_id,
            source=source,
            title=title[:300],
            change_set_json={
                "model": plan.model.model_dump(mode="json", exclude_none=True),
                "diff": diff_json(plan.diff),
                "lint": [f.as_dict() for f in plan.lint if not f.suppressed],
                "warnings": [i.as_dict() for i in plan.issues if i.severity == "warning"],
            },
            approval_level=int(level),
            status="pending",
            created_by=created_by,
        )
        self.session.add(proposal)
        self.session.flush()
        self._audit("proposal.created", proposal, created_by, organization_id,
                    {"source": source, "level": level.name, "ai_job_id": str(ai_job_id) if ai_job_id else None})
        return proposal

    def reject(self, proposal: Proposal, by: uuid.UUID, *, comment: str | None = None,
               organization_id: uuid.UUID | None = None) -> Proposal:
        self._require_pending(proposal)
        proposal.status = "rejected"
        proposal.approved_by = None
        proposal.decided_at = _now()
        cs = dict(proposal.change_set_json)
        cs["decision"] = {"by": str(by), "outcome": "rejected", "comment": comment}
        proposal.change_set_json = cs
        self._audit("proposal.rejected", proposal, by, organization_id, {"comment": comment})
        return proposal

    def approve(self, proposal: Proposal, by: uuid.UUID, *, comment: str | None = None,
                organization_id: uuid.UUID | None = None) -> Proposal:
        """Approve and apply in one step: stores the proposed model as a new revision."""
        self._require_pending(proposal)
        app = self.session.get(Application, proposal.application_id, with_for_update=True)
        if app is None:
            raise NotFound(f"application {proposal.application_id}")
        if app.current_revision_id != proposal.base_revision_id:
            proposal.status = "superseded"
            proposal.decided_at = _now()
            self._audit("proposal.superseded", proposal, by, organization_id, {})
            raise Stale("the model changed after this proposal was made; propose again")
        rev = self.revisions.create(
            proposal.application_id,
            AppModel.model_validate(proposal.change_set_json["model"]),
            created_by=by,
            message=f"Proposal: {proposal.title}"[:1000],
            organization_id=organization_id,
        )
        proposal.status = "applied"
        proposal.approved_by = by
        proposal.decided_at = _now()
        proposal.result_revision_id = rev.id
        cs = dict(proposal.change_set_json)
        cs["decision"] = {"by": str(by), "outcome": "approved", "comment": comment}
        proposal.change_set_json = cs
        self._audit("proposal.approved", proposal, by, organization_id,
                    {"revision_no": rev.revision_no, "comment": comment})
        return proposal

    @staticmethod
    def _require_pending(p: Proposal) -> None:
        if p.status != "pending":
            raise NotPending(f"proposal is {p.status}")

    def _audit(self, action: str, p: Proposal, actor: uuid.UUID | None, org: uuid.UUID | None, meta: dict[str, Any]) -> None:
        self.session.add(AuditEvent(organization_id=org, actor_id=actor, plane="platform", channel="api",
                                    action=action, target_type="proposal", target_id=str(p.id), meta=meta))


# --- AI jobs ---------------------------------------------------------------------------


def start_ai_job(session: Session, *, project_id: uuid.UUID, application_id: uuid.UUID | None, type: str,
                 created_by: uuid.UUID | None, input_text: str, template: str) -> AIJob:
    """Record the job before calling the provider. Only a hash of the input is stored."""
    job = AIJob(project_id=project_id, application_id=application_id, type=type, status="running",
                created_by=created_by, prompt_template_version=template, started_at=_now(),
                input_ref="sha256:" + hashlib.sha256(input_text.encode("utf-8")).hexdigest())
    session.add(job)
    session.flush()
    return job


def finish_ai_job(job: AIJob, *, ok: bool, provider: str | None = None, model: str | None = None,
                  usage: dict[str, Any] | None = None, error: str | None = None, result_ref: str | None = None) -> None:
    job.status = "succeeded" if ok else "failed"
    job.provider, job.model = provider, model
    job.usage = usage or {}
    job.error = error[:2000] if error else None
    job.result_ref = result_ref
    job.finished_at = _now()


def month_cost_usd(session: Session, project_id: uuid.UUID, now: datetime | None = None) -> float:
    """Recorded AI spend for the calendar month (UTC)."""
    now = now or _now()
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    jobs = session.scalars(select(AIJob).where(AIJob.project_id == project_id, AIJob.created_at >= start))
    return float(sum(float((j.usage or {}).get("cost_usd", 0.0)) for j in jobs))
