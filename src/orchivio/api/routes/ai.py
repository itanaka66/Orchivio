"""AI Builder endpoints: analyze requirements, propose model changes, explain elements.

Calls are synchronous for now (the worker/job-queue offload is planned). Every call is recorded
as an ``ai_jobs`` row; only a hash of the input text is stored.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from orchivio.ai import AIBuilder, BuilderError
from orchivio.db import ModelInvalid, ProposalRepository, to_model
from orchivio.db.models import Application, AppRevision
from orchivio.db.proposals import NoChanges, finish_ai_job, month_cost_usd, start_ai_job
from orchivio.llm import LLMError, LLMProvider, Usage

from ..deps import Principal, api_error, current_principal, get_db, load_application
from .proposals import ProposalDetail, proposal_detail

router = APIRouter(tags=["ai"])

MAX_TEXT = 20_000


class RequirementsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    requirements: str = Field(min_length=1, max_length=MAX_TEXT)


class ProposeIn(RequirementsIn):
    title: str | None = Field(default=None, max_length=300)


class ExplainIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    element_id: uuid.UUID
    question: str | None = Field(default=None, max_length=2000)


class ProposeOut(BaseModel):
    job_id: uuid.UUID
    proposal: ProposalDetail
    usage: dict[str, Any]


def _usage(u: Usage, attempts: int) -> dict[str, Any]:
    return {"input_tokens": u.input_tokens, "output_tokens": u.output_tokens, "cost_usd": u.cost_usd,
            "attempts": attempts}


def _guard(request: Request, db: Session, principal: Principal, app_id: uuid.UUID, action: str):
    """Provider configured, project consent for external providers, monthly budget."""
    provider: LLMProvider | None = request.app.state.llm_provider
    app, project = load_application(db, principal, app_id, action)
    if provider is None:
        raise api_error(503, "ai_not_configured", "no AI provider is configured on this server")
    cfg = project.settings or {}
    if provider.external and cfg.get("ai_allow_external") is not True:
        raise api_error(403, "external_ai_disabled",
                        "this provider sends project data to an external service; a project admin must set "
                        "settings.ai_allow_external to true first")
    budget = cfg.get("ai_monthly_budget_usd")
    if isinstance(budget, (int, float)) and not isinstance(budget, bool) and month_cost_usd(db, project.id) >= budget:
        raise api_error(429, "ai_budget_exceeded", "the project's monthly AI budget is used up")
    return provider, app, project


def _current_model(db: Session, app: Application):
    rev = db.get(AppRevision, app.current_revision_id) if app.current_revision_id else None
    return to_model(rev) if rev else None


def _provider_failed(e: LLMError):
    # Provider messages can contain request details; keep them in the job record, not the response.
    return api_error(502, "ai_provider_error", "the AI provider failed to answer")


@router.post("/applications/{app_id}/ai/analyze")
def analyze(app_id: uuid.UUID, body: RequirementsIn, request: Request,
            principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict[str, Any]:
    provider, app, project = _guard(request, db, principal, app_id, "model.propose")
    job = start_ai_job(db, project_id=project.id, application_id=app.id, type="analyze_requirements",
                       created_by=principal.user_id, input_text=body.requirements, template="analyze_requirements@1")
    try:
        r = AIBuilder(provider).analyze(body.requirements)
    except BuilderError as e:
        finish_ai_job(job, ok=False, usage=_usage(e.usage, e.attempts), error=str(e))
        db.commit()
        raise api_error(422, "ai_output_invalid", "the AI did not produce a usable analysis") from e
    except LLMError as e:
        finish_ai_job(job, ok=False, error=str(e))
        db.commit()
        raise _provider_failed(e) from e
    finish_ai_job(job, ok=True, provider=r.provider, model=r.model_name, usage=_usage(r.usage, r.attempts))
    db.commit()
    return {"job_id": str(job.id), "analysis": r.analysis.model_dump(), "usage": _usage(r.usage, r.attempts)}


@router.post("/applications/{app_id}/ai/propose", status_code=201, response_model=ProposeOut)
def propose(app_id: uuid.UUID, body: ProposeIn, request: Request,
            principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> ProposeOut:
    provider, app, project = _guard(request, db, principal, app_id, "model.propose")
    job = start_ai_job(db, project_id=project.id, application_id=app.id, type="propose_model",
                       created_by=principal.user_id, input_text=body.requirements, template="propose_model@1")
    try:
        r = AIBuilder(provider).propose_model(body.requirements, _current_model(db, app))
    except BuilderError as e:
        finish_ai_job(job, ok=False, usage=_usage(e.usage, e.attempts), error=str(e))
        db.commit()
        raise api_error(422, "ai_output_invalid", "the AI could not produce a valid model",
                        [i.as_dict() for i in e.issues][:30]) from e
    except LLMError as e:
        finish_ai_job(job, ok=False, error=str(e))
        db.commit()
        raise _provider_failed(e) from e
    usage = _usage(r.usage, r.attempts)
    try:
        proposal = ProposalRepository(db).create(
            app.id, r.model, source="ai_builder", title=body.title or body.requirements.strip()[:120],
            created_by=principal.user_id, organization_id=project.organization_id, ai_job_id=job.id)
    except (ModelInvalid, NoChanges) as e:
        finish_ai_job(job, ok=False, provider=r.provider, model=r.model_name, usage=usage, error=str(e))
        db.commit()
        if isinstance(e, NoChanges):
            raise api_error(409, "no_changes", "the AI proposed no change to the current model") from e
        raise api_error(422, "ai_output_invalid", "the AI could not produce a valid model") from e
    finish_ai_job(job, ok=True, provider=r.provider, model=r.model_name, usage=usage, result_ref=f"proposal:{proposal.id}")
    db.commit()
    return ProposeOut(job_id=job.id, proposal=proposal_detail(proposal), usage=usage)


@router.post("/applications/{app_id}/ai/explain")
def explain(app_id: uuid.UUID, body: ExplainIn, request: Request,
            principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Read-only (A0): explains one element using only it and its direct relations."""
    provider, app, project = _guard(request, db, principal, app_id, "model.read")
    model = _current_model(db, app)
    if model is None:
        raise api_error(409, "no_revision", "the application has no model yet")
    job = start_ai_job(db, project_id=project.id, application_id=app.id, type="explain_element",
                       created_by=principal.user_id, input_text=f"{body.element_id}\n{body.question or ''}",
                       template="explain_element@1")
    try:
        r = AIBuilder(provider).explain(model, str(body.element_id), body.question)
    except KeyError as e:
        finish_ai_job(job, ok=False, error="unknown element")
        db.commit()
        raise api_error(404, "not_found", "not found") from e
    except BuilderError as e:
        finish_ai_job(job, ok=False, usage=_usage(e.usage, e.attempts), error=str(e))
        db.commit()
        raise api_error(422, "ai_output_invalid", "the AI did not produce a usable explanation") from e
    except LLMError as e:
        finish_ai_job(job, ok=False, error=str(e))
        db.commit()
        raise _provider_failed(e) from e
    usage = _usage(r.usage, r.attempts)
    finish_ai_job(job, ok=True, provider=r.provider, model=r.model_name, usage=usage)
    db.commit()
    return {"job_id": str(job.id), "explanation": r.explanation, "cited_element_ids": r.cited_element_ids,
            "usage": usage, "note": "AI-generated; citations are checked against the model, the text is not."}

