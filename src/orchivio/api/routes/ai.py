"""AI Builder endpoints: analyze requirements, propose model changes, explain elements.

Calls are synchronous for now (the worker/job-queue offload is planned). Every call is recorded
as an ``ai_jobs`` row; only a hash of the input text is stored.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from orchivio.ai import AIBuilder, BuilderError, ToolContext, ToolInfo
from orchivio.db import ModelInvalid, ProposalRepository, to_model
from orchivio.db.models import Application, AppRevision
from orchivio.db.proposals import NoChanges, finish_ai_job, month_cost_usd, start_ai_job
from orchivio.llm import NO_USAGE, LLMError, LLMProvider, Usage

from ..deps import (
    Principal,
    api_error,
    audit,
    authorize,
    current_principal,
    get_db,
    load_application,
)
from .proposals import ProposalDetail, proposal_detail

router = APIRouter(tags=["ai"])

MAX_TEXT = 20_000


class RequirementsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    requirements: str = Field(min_length=1, max_length=MAX_TEXT)
    register_requirements: bool = Field(default=False, description="also store the requirements the text states as draft requirement rows (needs story.manage)")


class ProposeIn(RequirementsIn):
    title: str | None = Field(default=None, max_length=300)
    use_tools: bool = Field(default=False, description="let the Builder consult the organization's enabled read-only MCP tools")


class ExplainIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    element_id: uuid.UUID
    question: str | None = Field(default=None, max_length=2000)


class ProposeOut(BaseModel):
    job_id: uuid.UUID
    proposal: ProposalDetail
    usage: dict[str, Any]
    registered_requirements: dict[str, Any] | None = None


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


def auto_register(request: Request, db: Session, provider: LLMProvider, principal: Principal, app: Application, project, text: str) -> dict[str, Any]:
    """The optional second step of analyze/propose: failure here never fails the call that already succeeded."""
    from .agile import register_requirements  # local import: agile imports this module's guard

    try:
        authorize_story(db, principal, project)
        return register_requirements(db, request, provider, principal, app, project, text)
    except BuilderError:
        return {"error": "ai_output_invalid", "requirements": []}
    except LLMError:
        return {"error": "ai_provider_error", "requirements": []}
    except HTTPException as e:
        return {"error": "forbidden" if e.status_code == 403 else "failed", "requirements": []}


def authorize_story(db: Session, principal: Principal, project) -> None:
    authorize(db, principal, project.organization_id, "story.manage", project_id=project.id)


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
    out = {"job_id": str(job.id), "analysis": r.analysis.model_dump(), "usage": _usage(r.usage, r.attempts)}
    if body.register_requirements:
        out["registered_requirements"] = auto_register(request, db, provider, principal, app, project, body.requirements)
    return out


@router.post("/applications/{app_id}/ai/propose", status_code=201, response_model=ProposeOut)
def propose(app_id: uuid.UUID, body: ProposeIn, request: Request,
            principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> ProposeOut:
    provider, app, project = _guard(request, db, principal, app_id, "model.propose")
    context: list[ToolContext] = []
    calls: list[dict[str, Any]] = []
    plan_usage = Usage()
    if body.use_tools:
        try:
            context, calls, plan_usage = gather_tool_context(request, db, provider, principal, project, body.requirements)
        except LLMError as e:
            raise _provider_failed(e) from e
    out = run_proposal(db, provider, principal, app, project, body.requirements, body.title,
                       context=context, tool_calls=calls, extra_usage=plan_usage)
    if body.register_requirements:
        out.registered_requirements = auto_register(request, db, provider, principal, app, project, body.requirements)
    return out


def gather_tool_context(request: Request, db: Session, provider: LLMProvider, principal: Principal, project,
                        requirements: str) -> tuple[list[ToolContext], list[dict[str, Any]], Usage]:
    """Let the model *request* calls to the organization's enabled A0 tools, run those that pass the broker's checks, and
    return their output as untrusted context. One planning round only: tool output never triggers further calls."""
    broker = request.app.state.mcp_broker
    catalog = broker.catalog(project.organization_id)
    if not catalog:
        return [], [{"note": "no tools are enabled for this organization"}], Usage()
    limit = request.app.state.settings.mcp_client_max_calls
    try:
        plan = AIBuilder(provider).plan_tool_calls(
            requirements, [ToolInfo(c.server, c.tool, c.description, c.input_schema) for c in catalog], limit)
    except BuilderError as e:  # an unusable plan is not fatal: propose without background
        return [], [{"note": "the tool plan was unusable; continued without tools"}], e.usage
    records = broker.run(project.organization_id, [(c.server, c.tool, c.arguments) for c in plan.calls], limit)
    meta = []
    for r in records:
        audit(db, request, "mcp.client_call", actor_id=principal.user_id, org_id=project.organization_id, target_type="mcp_server",
              target_id=None, meta={"server": r.server, "tool": r.tool, "args_sha256": r.args_sha256, "ok": r.ok,
                                    "error": r.error, "chars": len(r.text), "truncated": r.truncated})
        meta.append({"server": r.server, "tool": r.tool, "ok": r.ok, "error": r.error, "chars": len(r.text), "truncated": r.truncated})
    return [ToolContext(r.server, r.tool, r.text) for r in records if r.ok and r.text], meta, plan.usage


def run_proposal(db: Session, provider: LLMProvider, principal: Principal, app: Application, project,
                 requirements: str, title: str | None, job_type: str = "propose_model", *,
                 context: list[ToolContext] | None = None, tool_calls: list[dict[str, Any]] | None = None,
                 extra_usage: Usage = NO_USAGE) -> ProposeOut:
    """Ask the provider for a model, store it as a pending proposal and record the job."""
    job = start_ai_job(db, project_id=project.id, application_id=app.id, type=job_type,
                       created_by=principal.user_id, input_text=requirements,
                       template="propose_model_with_context@1" if context else "propose_model@1")
    body = ProposeIn(requirements=requirements[:MAX_TEXT], title=title)
    try:
        r = AIBuilder(provider).propose_model(body.requirements, _current_model(db, app), context or None)
    except BuilderError as e:
        finish_ai_job(job, ok=False, usage=_usage(e.usage, e.attempts), error=str(e))
        db.commit()
        raise api_error(422, "ai_output_invalid", "the AI could not produce a valid model",
                        [i.as_dict() for i in e.issues][:30]) from e
    except LLMError as e:
        finish_ai_job(job, ok=False, error=str(e))
        db.commit()
        raise _provider_failed(e) from e
    usage = _usage(r.usage + extra_usage, r.attempts)
    if tool_calls:
        usage["tool_calls"] = tool_calls
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


@router.post("/applications/{app_id}/ai/plan")
def plan_implementation(app_id: uuid.UUID, body: RequirementsIn, request: Request,
                        principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Analyse requirements then produce an ordered, typed implementation task list (FR-012)."""
    provider, app, project = _guard(request, db, principal, app_id, "model.propose")
    job = start_ai_job(db, project_id=project.id, application_id=app.id, type="plan_implementation",
                       created_by=principal.user_id, input_text=body.requirements,
                       template="plan_implementation@1")
    try:
        builder = AIBuilder(provider)
        analysis_r = builder.analyze(body.requirements)
        plan_r = builder.plan_implementation(body.requirements, analysis_r.analysis)
    except BuilderError as e:
        finish_ai_job(job, ok=False, usage=_usage(e.usage, e.attempts), error=str(e))
        db.commit()
        raise api_error(422, "ai_output_invalid", "the AI did not produce a usable plan") from e
    except LLMError as e:
        finish_ai_job(job, ok=False, error=str(e))
        db.commit()
        raise _provider_failed(e) from e
    combined = plan_r.usage + analysis_r.usage
    total_attempts = analysis_r.attempts + plan_r.attempts
    usage = _usage(combined, total_attempts)
    finish_ai_job(job, ok=True, provider=plan_r.provider, model=plan_r.model_name, usage=usage)
    if body.register_requirements:
        pass  # analysis already extracted entities; requirement rows handled by caller via auto_register if desired
    db.commit()
    return {
        "job_id": str(job.id),
        "analysis": analysis_r.analysis.model_dump(),
        "plan": plan_r.plan.model_dump(),
        "usage": usage,
    }
