"""Requirements, story board, traceability (FR-098) and preview feedback with AI clustering (FR-099).

Everything is scoped to a project. Reading needs ``project.read``; changing needs ``story.manage`` (editors and up). Trace links
connect requirements and stories to model elements, revisions and tests, and are checked against the project's real data when
created. AI-clustered stories are only ever *proposed*: a person moves them onto the backlog. Feedback text is untrusted.
"""

from __future__ import annotations

import base64
import binascii
import csv
import io
import json
import uuid
from collections import defaultdict
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from orchivio.ai import AIBuilder, BuilderError
from orchivio.db import to_model
from orchivio.db.models import (
    Application,
    AppRevision,
    FeedbackItem,
    Project,
    Requirement,
    Story,
    TraceLink,
)
from orchivio.db.proposals import finish_ai_job, start_ai_job
from orchivio.llm import LLMError
from orchivio.model.ids import iter_elements
from orchivio.testgen import generate_cases

from ..deps import (
    Principal,
    api_error,
    audit,
    current_principal,
    get_db,
    load_application,
    load_project,
    not_found,
    utcnow,
)
from ..idempotency import IdempotentRoute
from .ai import _guard, _provider_failed, _usage

router = APIRouter(tags=["agile"], route_class=IdempotentRoute)

REQ_STATUS = "^(draft|accepted|implemented|rejected)$"
STORY_STATUS = "^(proposed|backlog|ready|in_progress|done|rejected)$"
MAX_SCREENSHOT = 512 * 1024
FEEDBACK_PER_HOUR = 30
MAX_CLUSTERED = 100
MAX_DOCUMENT = 20_000
_IMAGES = ((b"\x89PNG\r\n\x1a\n", "image/png"), (b"\xff\xd8\xff", "image/jpeg"))


# --- requirements ---------------------------------------------------------------------------------


class RequirementIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=300)
    body: str = Field(default="", max_length=10_000)
    status: str = Field(default="draft", pattern=REQ_STATUS)


class RequirementPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = Field(default=None, min_length=1, max_length=300)
    body: str | None = Field(default=None, max_length=10_000)
    status: str | None = Field(default=None, pattern=REQ_STATUS)


def req_out(r: Requirement) -> dict[str, Any]:
    return {"id": str(r.id), "title": r.title, "body": r.body, "status": r.status, "source": r.source, "created_at": r.created_at.isoformat()}


def _owned(db: Session, model: type, project: Project, item_id: uuid.UUID, what: str):
    row = db.get(model, item_id)
    if row is None or row.project_id != project.id:
        raise not_found(what)
    return row


@router.post("/projects/{project_id}/requirements", status_code=201)
def create_requirement(project_id: uuid.UUID, body: RequirementIn, request: Request, principal: Principal = Depends(current_principal),
                       db: Session = Depends(get_db)) -> dict[str, Any]:
    project = load_project(db, principal, project_id, "story.manage")
    r = Requirement(project_id=project.id, title=body.title, body=body.body, status=body.status, created_by=principal.user_id)
    db.add(r)
    db.flush()
    audit(db, request, "requirement.created", actor_id=principal.user_id, org_id=project.organization_id, target_type="requirement", target_id=r.id)
    db.commit()
    return req_out(r)


@router.get("/projects/{project_id}/requirements")
def list_requirements(project_id: uuid.UUID, status: str | None = Query(None, pattern=REQ_STATUS), principal: Principal = Depends(current_principal),
                      db: Session = Depends(get_db)) -> dict[str, Any]:
    project = load_project(db, principal, project_id, "project.read")
    stmt = select(Requirement).where(Requirement.project_id == project.id).order_by(Requirement.created_at, Requirement.id)
    if status:
        stmt = stmt.where(Requirement.status == status)
    return {"items": [req_out(r) for r in db.scalars(stmt)]}


@router.get("/projects/{project_id}/requirements/{req_id}")
def get_requirement(project_id: uuid.UUID, req_id: uuid.UUID, principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict[str, Any]:
    return req_out(_owned(db, Requirement, load_project(db, principal, project_id, "project.read"), req_id, "requirement"))


@router.patch("/projects/{project_id}/requirements/{req_id}")
def update_requirement(project_id: uuid.UUID, req_id: uuid.UUID, body: RequirementPatch, request: Request,
                       principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict[str, Any]:
    project = load_project(db, principal, project_id, "story.manage")
    r = _owned(db, Requirement, project, req_id, "requirement")
    changed = [k for k, v in body.model_dump(exclude_none=True).items()]
    for k in changed:
        setattr(r, k, getattr(body, k))
    audit(db, request, "requirement.updated", actor_id=principal.user_id, org_id=project.organization_id, target_type="requirement", target_id=r.id,
          meta={"fields": changed})
    db.commit()
    return req_out(r)


@router.delete("/projects/{project_id}/requirements/{req_id}", status_code=204)
def delete_requirement(project_id: uuid.UUID, req_id: uuid.UUID, request: Request, principal: Principal = Depends(current_principal),
                       db: Session = Depends(get_db)) -> Response:
    project = load_project(db, principal, project_id, "story.manage")
    r = _owned(db, Requirement, project, req_id, "requirement")
    _drop_links(db, project, "requirement", r.id)
    db.delete(r)
    audit(db, request, "requirement.deleted", actor_id=principal.user_id, org_id=project.organization_id, target_type="requirement", target_id=r.id)
    db.commit()
    return Response(status_code=204)


class ExtractIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=MAX_DOCUMENT)


def register_requirements(db: Session, request: Request, provider, principal: Principal, app: Application, project: Project, text: str) -> dict[str, Any]:  # noqa: ANN001
    """Ask the AI for the requirements ``text`` states and store them as draft rows. The AI job is recorded and committed; on failure it
    is marked failed and ``BuilderError`` / ``LLMError`` propagate (nothing is stored)."""
    existing = [t for (t,) in db.execute(select(Requirement.title).where(Requirement.project_id == project.id))]
    job = start_ai_job(db, project_id=project.id, application_id=app.id, type="extract_requirements", created_by=principal.user_id,
                       input_text=text, template="extract_requirements@1")
    try:
        r = AIBuilder(provider).extract_requirements(text, existing)
    except BuilderError as e:
        finish_ai_job(job, ok=False, usage=_usage(e.usage, e.attempts), error=str(e))
        db.commit()
        raise
    except LLMError as e:
        finish_ai_job(job, ok=False, error=str(e))
        db.commit()
        raise
    rows = [Requirement(project_id=project.id, title=x.title, body=x.body, status="draft", source="ai", created_by=principal.user_id) for x in r.requirements]
    db.add_all(rows)
    db.flush()
    usage = _usage(r.usage, r.attempts)
    finish_ai_job(job, ok=True, provider=r.provider, model=r.model_name, usage=usage, result_ref=f"requirements:{len(rows)}")
    audit(db, request, "requirement.extracted", actor_id=principal.user_id, org_id=project.organization_id, target_type="application", target_id=app.id,
          meta={"created": len(rows), "skipped_duplicates": r.skipped_duplicates, "ai_job": str(job.id)})
    db.commit()
    return {"job_id": str(job.id), "requirements": [req_out(x) for x in rows], "skipped_duplicates": r.skipped_duplicates, "usage": usage,
            "note": "AI-extracted requirements are drafts; review, edit or reject them."}


@router.post("/applications/{app_id}/requirements/extract", status_code=201)
def extract_requirements(app_id: uuid.UUID, body: ExtractIn, request: Request, principal: Principal = Depends(current_principal),
                         db: Session = Depends(get_db)) -> dict[str, Any]:
    """Turn a document (notes, a brief, an e-mail thread) into *draft* requirements. Titles already on the project are skipped."""
    provider, app, project = _guard(request, db, principal, app_id, "story.manage")
    try:
        return register_requirements(db, request, provider, principal, app, project, body.text)
    except BuilderError as e:
        raise api_error(422, "ai_output_invalid", "the AI did not produce usable requirements") from e
    except LLMError as e:
        raise _provider_failed(e) from e


# --- stories ----------------------------------------------------------------------------------------


class StoryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=300)
    body: str = Field(default="", max_length=10_000)
    acceptance: str = Field(default="", max_length=10_000)
    requirement_id: uuid.UUID | None = None
    status: str = Field(default="backlog", pattern=STORY_STATUS)
    position: int | None = Field(default=None, ge=0, le=1_000_000)


class StoryPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = Field(default=None, min_length=1, max_length=300)
    body: str | None = Field(default=None, max_length=10_000)
    acceptance: str | None = Field(default=None, max_length=10_000)
    requirement_id: uuid.UUID | None = None
    status: str | None = Field(default=None, pattern=STORY_STATUS)
    position: int | None = Field(default=None, ge=0, le=1_000_000)


def story_out(s: Story) -> dict[str, Any]:
    return {"id": str(s.id), "title": s.title, "body": s.body, "acceptance": s.acceptance, "status": s.status, "source": s.source,
            "position": s.position, "requirement_id": str(s.requirement_id) if s.requirement_id else None, "created_at": s.created_at.isoformat()}


def _next_position(db: Session, project_id: uuid.UUID) -> int:
    return (db.scalar(select(func.max(Story.position)).where(Story.project_id == project_id)) or 0) + 1


@router.post("/projects/{project_id}/stories", status_code=201)
def create_story(project_id: uuid.UUID, body: StoryIn, request: Request, principal: Principal = Depends(current_principal),
                 db: Session = Depends(get_db)) -> dict[str, Any]:
    project = load_project(db, principal, project_id, "story.manage")
    if body.requirement_id is not None:
        _owned(db, Requirement, project, body.requirement_id, "requirement")
    s = Story(project_id=project.id, title=body.title, body=body.body, acceptance=body.acceptance, requirement_id=body.requirement_id,
              status=body.status, position=body.position if body.position is not None else _next_position(db, project.id), created_by=principal.user_id)
    db.add(s)
    db.flush()
    audit(db, request, "story.created", actor_id=principal.user_id, org_id=project.organization_id, target_type="story", target_id=s.id)
    db.commit()
    return story_out(s)


@router.get("/projects/{project_id}/stories")
def list_stories(project_id: uuid.UUID, status: str | None = Query(None, pattern=STORY_STATUS), requirement_id: uuid.UUID | None = None,
                 principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict[str, Any]:
    project = load_project(db, principal, project_id, "project.read")
    stmt = select(Story).where(Story.project_id == project.id).order_by(Story.position, Story.created_at, Story.id)
    if status:
        stmt = stmt.where(Story.status == status)
    if requirement_id:
        stmt = stmt.where(Story.requirement_id == requirement_id)
    return {"items": [story_out(s) for s in db.scalars(stmt)]}


@router.get("/projects/{project_id}/stories/{story_id}")
def get_story(project_id: uuid.UUID, story_id: uuid.UUID, principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict[str, Any]:
    return story_out(_owned(db, Story, load_project(db, principal, project_id, "project.read"), story_id, "story"))


@router.patch("/projects/{project_id}/stories/{story_id}")
def update_story(project_id: uuid.UUID, story_id: uuid.UUID, body: StoryPatch, request: Request, principal: Principal = Depends(current_principal),
                 db: Session = Depends(get_db)) -> dict[str, Any]:
    project = load_project(db, principal, project_id, "story.manage")
    s = _owned(db, Story, project, story_id, "story")
    given = body.model_fields_set
    if "requirement_id" in given and body.requirement_id is not None:
        _owned(db, Requirement, project, body.requirement_id, "requirement")
    for k in given:
        setattr(s, k, getattr(body, k))
    audit(db, request, "story.updated", actor_id=principal.user_id, org_id=project.organization_id, target_type="story", target_id=s.id,
          meta={"fields": sorted(given), "status": s.status})
    db.commit()
    return story_out(s)


@router.delete("/projects/{project_id}/stories/{story_id}", status_code=204)
def delete_story(project_id: uuid.UUID, story_id: uuid.UUID, request: Request, principal: Principal = Depends(current_principal),
                 db: Session = Depends(get_db)) -> Response:
    project = load_project(db, principal, project_id, "story.manage")
    s = _owned(db, Story, project, story_id, "story")
    _drop_links(db, project, "story", s.id)
    db.delete(s)
    audit(db, request, "story.deleted", actor_id=principal.user_id, org_id=project.organization_id, target_type="story", target_id=s.id)
    db.commit()
    return Response(status_code=204)


# --- trace links ------------------------------------------------------------------------------------

_ALLOWED = {("requirement", "story"), ("requirement", "element"), ("requirement", "revision"), ("requirement", "test"),
            ("story", "element"), ("story", "revision"), ("story", "test")}


class LinkIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    from_type: str = Field(pattern="^(requirement|story)$")
    from_id: uuid.UUID
    to_type: str = Field(pattern="^(story|element|revision|test)$")
    to_id: str = Field(min_length=1, max_length=200)


def link_out(link: TraceLink) -> dict[str, Any]:
    return {"id": str(link.id), "from_type": link.from_type, "from_id": link.from_id, "to_type": link.to_type, "to_id": link.to_id}


def project_elements(db: Session, project_id: uuid.UUID) -> dict[str, str]:
    """element id -> 'kind name' for the current models of the project's applications."""
    out: dict[str, str] = {}
    for app in db.scalars(select(Application).where(Application.project_id == project_id)):
        rev = db.get(AppRevision, app.current_revision_id) if app.current_revision_id else None
        if rev is not None:
            out.update({el.element_id: f"{key[0]} {'.'.join(key[1:])}" for key, el in iter_elements(to_model(rev)) if el.element_id})
    return out


def project_tests(db: Session, project_id: uuid.UUID) -> dict[str, str]:
    """Generated test case id -> title, for the current models of the project's applications."""
    out: dict[str, str] = {}
    for app in db.scalars(select(Application).where(Application.project_id == project_id)):
        rev = db.get(AppRevision, app.current_revision_id) if app.current_revision_id else None
        if rev is not None:
            out.update({c.id: c.title for c in generate_cases(to_model(rev))})
    return out


@router.get("/projects/{project_id}/test-cases")
def list_test_cases(project_id: uuid.UUID, q: str | None = Query(None, max_length=100), limit: int = Query(200, ge=1, le=1000),
                    principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict[str, Any]:
    """The generated test cases a story or requirement can be linked to (ids are stable while the model keeps the element)."""
    project = load_project(db, principal, project_id, "project.read")
    needle = (q or "").casefold()
    items = [{"id": k, "title": v} for k, v in sorted(project_tests(db, project.id).items()) if needle in k.casefold() or needle in v.casefold()]
    return {"items": items[:limit], "total": len(items)}


def _drop_links(db: Session, project: Project, kind: str, item_id: uuid.UUID) -> None:
    for link in db.scalars(select(TraceLink).where(TraceLink.project_id == project.id)):
        if (link.from_type == kind and link.from_id == str(item_id)) or (link.to_type == kind and link.to_id == str(item_id)):
            db.delete(link)


@router.post("/projects/{project_id}/trace-links", status_code=201)
def create_link(project_id: uuid.UUID, body: LinkIn, request: Request, principal: Principal = Depends(current_principal),
                db: Session = Depends(get_db)) -> dict[str, Any]:
    project = load_project(db, principal, project_id, "story.manage")
    if (body.from_type, body.to_type) not in _ALLOWED:
        raise api_error(422, "invalid_link", f"a {body.from_type} cannot be linked to a {body.to_type}")
    _owned(db, Requirement if body.from_type == "requirement" else Story, project, body.from_id, body.from_type)
    to_id = body.to_id
    if body.to_type in ("story", "revision"):
        try:
            uid = uuid.UUID(to_id)
        except ValueError as e:
            raise api_error(422, "invalid_link", "the target id must be a UUID") from e
        to_id = str(uid)
        if body.to_type == "story":
            _owned(db, Story, project, uid, "story")
        else:
            rev = db.get(AppRevision, uid)
            app = db.get(Application, rev.application_id) if rev else None
            if app is None or app.project_id != project.id:
                raise not_found("revision")
    elif body.to_type == "element":
        if to_id not in project_elements(db, project.id):
            raise api_error(422, "unknown_element", "no application of this project has an element with this id in its current model")
    elif to_id not in project_tests(db, project.id):
        raise api_error(422, "unknown_test", "no generated test case of this project has this id (see GET /projects/{id}/test-cases)")
    link = TraceLink(project_id=project.id, from_type=body.from_type, from_id=str(body.from_id), to_type=body.to_type, to_id=to_id,
                     created_by=principal.user_id)
    db.add(link)
    try:
        db.flush()
    except IntegrityError as e:
        db.rollback()
        raise api_error(409, "link_exists", "this link already exists") from e
    audit(db, request, "trace_link.created", actor_id=principal.user_id, org_id=project.organization_id, target_type="trace_link", target_id=link.id,
          meta={"from": link.from_type, "to": link.to_type})
    db.commit()
    return link_out(link)


@router.get("/projects/{project_id}/trace-links")
def list_links(project_id: uuid.UUID, from_id: uuid.UUID | None = None, to_id: str | None = Query(None, max_length=200),
               principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict[str, Any]:
    project = load_project(db, principal, project_id, "project.read")
    stmt = select(TraceLink).where(TraceLink.project_id == project.id).order_by(TraceLink.created_at, TraceLink.id)
    if from_id:
        stmt = stmt.where(TraceLink.from_id == str(from_id))
    if to_id:
        stmt = stmt.where(TraceLink.to_id == to_id)
    return {"items": [link_out(link) for link in db.scalars(stmt)]}


@router.delete("/projects/{project_id}/trace-links/{link_id}", status_code=204)
def delete_link(project_id: uuid.UUID, link_id: uuid.UUID, request: Request, principal: Principal = Depends(current_principal),
                db: Session = Depends(get_db)) -> Response:
    project = load_project(db, principal, project_id, "story.manage")
    link = _owned(db, TraceLink, project, link_id, "trace link")
    db.delete(link)
    audit(db, request, "trace_link.deleted", actor_id=principal.user_id, org_id=project.organization_id, target_type="trace_link", target_id=link.id)
    db.commit()
    return Response(status_code=204)


def _cell(value: Any) -> str:
    s = "" if value is None else str(value)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s  # spreadsheets must not execute requirement text


@router.get("/projects/{project_id}/traceability")
def traceability(project_id: uuid.UUID, format: str = Query("json", pattern="^(json|csv)$"), principal: Principal = Depends(current_principal),
                 db: Session = Depends(get_db)) -> Response:
    """Every requirement and story with what it is linked to, whether linked elements still exist, and coverage counts."""
    project = load_project(db, principal, project_id, "project.read")
    elements = project_elements(db, project.id)
    cases = project_tests(db, project.id)
    reqs = list(db.scalars(select(Requirement).where(Requirement.project_id == project.id).order_by(Requirement.created_at, Requirement.id)))
    stories = list(db.scalars(select(Story).where(Story.project_id == project.id).order_by(Story.position, Story.id)))
    by_from: dict[tuple[str, str], list[TraceLink]] = defaultdict(list)
    for link in db.scalars(select(TraceLink).where(TraceLink.project_id == project.id).order_by(TraceLink.created_at, TraceLink.id)):
        by_from[(link.from_type, link.from_id)].append(link)

    def targets(kind: str, item_id: uuid.UUID, extra_story_ids: list[str] = ()) -> dict[str, Any]:
        links = by_from[(kind, str(item_id))]
        out: dict[str, Any] = {"stories": [], "elements": [], "revisions": [], "tests": []}
        for sid in extra_story_ids:
            out["stories"].append(sid)
        for link in links:
            if link.to_type == "element":
                out["elements"].append({"id": link.to_id, "name": elements.get(link.to_id), "exists": link.to_id in elements})
            elif link.to_type == "story":
                if link.to_id not in out["stories"]:
                    out["stories"].append(link.to_id)
            elif link.to_type == "test":
                out["tests"].append({"id": link.to_id, "title": cases.get(link.to_id), "exists": link.to_id in cases})
            else:
                out["revisions"].append(link.to_id)
        return out

    by_req: dict[uuid.UUID, list[str]] = defaultdict(list)
    for s in stories:
        if s.requirement_id:
            by_req[s.requirement_id].append(str(s.id))
    req_rows = [{**req_out(r), **targets("requirement", r.id, by_req.get(r.id, []))} for r in reqs]
    story_rows = [{**story_out(s), **{k: v for k, v in targets("story", s.id).items() if k != "stories"}} for s in stories]
    live = [r for r in req_rows if r["status"] != "rejected"]
    covered = lambda key: sum(1 for r in live if r[key])  # noqa: E731
    doc = {"requirements": req_rows, "stories": story_rows,
           "coverage": {"requirements": len(live), "with_stories": covered("stories"), "with_elements": covered("elements"),
                        "with_tests": covered("tests"), "with_revisions": covered("revisions"),
                        "dangling_elements": sum(1 for r in req_rows + story_rows for e in r["elements"] if not e["exists"]),
                        "dangling_tests": sum(1 for r in req_rows + story_rows for t in r["tests"] if not t["exists"])}}
    if format == "json":
        return Response(json.dumps(doc), media_type="application/json")
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["kind", "id", "title", "status", "linked_stories", "linked_elements", "missing_elements", "revisions", "tests"])
    for kind, rows in (("requirement", req_rows), ("story", story_rows)):
        for r in rows:
            w.writerow([kind, r["id"], _cell(r["title"]), r["status"], len(r.get("stories", [])),
                        _cell("; ".join(e["name"] or e["id"] for e in r["elements"] if e["exists"])),
                        _cell("; ".join(e["id"] for e in r["elements"] if not e["exists"])), _cell("; ".join(r["revisions"])), _cell("; ".join(t["id"] for t in r["tests"]))])
    return Response("﻿" + buf.getvalue(), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": 'attachment; filename="traceability.csv"'})


# --- preview feedback ---------------------------------------------------------------------------------


class FeedbackIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    page_key: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=2000)
    element_id: str | None = Field(default=None, max_length=64)
    screenshot_base64: str | None = Field(default=None, max_length=MAX_SCREENSHOT * 2)


def feedback_out(f: FeedbackItem) -> dict[str, Any]:
    return {"id": str(f.id), "page_key": f.page_key, "element_id": f.element_id, "body": f.body, "status": f.status,
            "has_screenshot": f.screenshot is not None, "story_id": str(f.story_id) if f.story_id else None,
            "created_by": str(f.created_by) if f.created_by else None, "created_at": f.created_at.isoformat()}


def _decode_image(data: str) -> tuple[bytes, str]:
    try:
        raw = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError) as e:
        raise api_error(422, "invalid_screenshot", "the screenshot is not valid base64") from e
    if len(raw) > MAX_SCREENSHOT:
        raise api_error(413, "screenshot_too_large", f"screenshots are limited to {MAX_SCREENSHOT // 1024} KiB")
    for magic, kind in _IMAGES:
        if raw.startswith(magic):
            return raw, kind
    raise api_error(422, "invalid_screenshot", "only PNG and JPEG screenshots are accepted")


def _app_elements(db: Session, app: Application) -> dict[str, str]:
    rev = db.get(AppRevision, app.current_revision_id) if app.current_revision_id else None
    if rev is None:
        return {}
    return {el.element_id: f"{key[0]} {'.'.join(key[1:])}" for key, el in iter_elements(to_model(rev)) if el.element_id}


@router.post("/applications/{app_id}/feedback", status_code=201)
def submit_feedback(app_id: uuid.UUID, body: FeedbackIn, request: Request, principal: Principal = Depends(current_principal),
                    db: Session = Depends(get_db)) -> dict[str, Any]:
    """Anyone who can open the project (viewers included) may leave feedback from the preview."""
    app, project = load_application(db, principal, app_id, "project.read")
    recent = db.scalar(select(func.count()).select_from(FeedbackItem).where(
        FeedbackItem.application_id == app.id, FeedbackItem.created_by == principal.user_id, FeedbackItem.created_at >= utcnow() - timedelta(hours=1)))
    if (recent or 0) >= FEEDBACK_PER_HOUR:
        raise api_error(429, "too_much_feedback", f"at most {FEEDBACK_PER_HOUR} feedback items per hour")
    if body.element_id is not None and body.element_id not in _app_elements(db, app):
        raise api_error(422, "unknown_element", "this element is not in the application's current model")
    shot, shot_type = _decode_image(body.screenshot_base64) if body.screenshot_base64 else (None, None)
    f = FeedbackItem(application_id=app.id, page_key=body.page_key, element_id=body.element_id, body=body.body, screenshot=shot,
                     screenshot_type=shot_type, created_by=principal.user_id)
    db.add(f)
    db.flush()
    audit(db, request, "feedback.submitted", actor_id=principal.user_id, org_id=project.organization_id, target_type="feedback", target_id=f.id,
          meta={"has_screenshot": shot is not None})
    db.commit()
    return feedback_out(f)


@router.get("/applications/{app_id}/feedback")
def list_feedback(app_id: uuid.UUID, status: str | None = Query(None, pattern="^(new|clustered|dismissed)$"), limit: int = Query(100, ge=1, le=500),
                  principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict[str, Any]:
    load_application(db, principal, app_id, "story.manage")
    stmt = select(FeedbackItem).where(FeedbackItem.application_id == app_id).order_by(FeedbackItem.created_at.desc(), FeedbackItem.id).limit(limit)
    if status:
        stmt = stmt.where(FeedbackItem.status == status)
    return {"items": [feedback_out(f) for f in db.scalars(stmt)]}


def _feedback(db: Session, app_id: uuid.UUID, fid: uuid.UUID) -> FeedbackItem:
    f = db.get(FeedbackItem, fid)
    if f is None or f.application_id != app_id:
        raise not_found("feedback")
    return f


@router.get("/applications/{app_id}/feedback/{feedback_id}/screenshot")
def screenshot(app_id: uuid.UUID, feedback_id: uuid.UUID, principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> Response:
    load_application(db, principal, app_id, "story.manage")
    f = _feedback(db, app_id, feedback_id)
    if f.screenshot is None:
        raise not_found("screenshot")
    return Response(f.screenshot, media_type=f.screenshot_type or "application/octet-stream",
                    headers={"X-Content-Type-Options": "nosniff", "Content-Security-Policy": "default-src 'none'; sandbox", "Cache-Control": "private, no-store"})


class FeedbackPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: str = Field(pattern="^(new|dismissed)$")


@router.patch("/applications/{app_id}/feedback/{feedback_id}")
def triage_feedback(app_id: uuid.UUID, feedback_id: uuid.UUID, body: FeedbackPatch, request: Request, principal: Principal = Depends(current_principal),
                    db: Session = Depends(get_db)) -> dict[str, Any]:
    _, project = load_application(db, principal, app_id, "story.manage")
    f = _feedback(db, app_id, feedback_id)
    if f.status == "clustered":
        raise api_error(409, "already_clustered", "this feedback already belongs to a story")
    f.status = body.status
    audit(db, request, "feedback.triaged", actor_id=principal.user_id, org_id=project.organization_id, target_type="feedback", target_id=f.id,
          meta={"status": f.status})
    db.commit()
    return feedback_out(f)


@router.post("/applications/{app_id}/feedback/cluster", status_code=201)
def cluster_feedback(app_id: uuid.UUID, request: Request, principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Ask the AI to group *new* feedback into proposed stories. Nothing is applied: stories start as ``proposed``."""
    provider, app, project = _guard(request, db, principal, app_id, "story.manage")
    items = list(db.scalars(select(FeedbackItem).where(FeedbackItem.application_id == app.id, FeedbackItem.status == "new")
                            .order_by(FeedbackItem.created_at, FeedbackItem.id).limit(MAX_CLUSTERED)))
    if not items:
        raise api_error(409, "no_feedback", "there is no new feedback to cluster")
    elements = _app_elements(db, app)
    sent = [{"number": i + 1, "page": f.page_key, "element_id": f.element_id, "text": f.body} for i, f in enumerate(items)]
    job = start_ai_job(db, project_id=project.id, application_id=app.id, type="cluster_feedback", created_by=principal.user_id,
                       input_text="feedback:" + ",".join(str(f.id) for f in items), template="cluster_feedback@1")
    try:
        r = AIBuilder(provider).cluster_feedback(sent, elements)
    except BuilderError as e:
        finish_ai_job(job, ok=False, usage=_usage(e.usage, e.attempts), error=str(e))
        db.commit()
        raise api_error(422, "ai_output_invalid", "the AI did not produce usable stories") from e
    except LLMError as e:
        finish_ai_job(job, ok=False, error=str(e))
        db.commit()
        raise _provider_failed(e) from e
    created = []
    position = _next_position(db, project.id)
    for ps in r.stories:
        story = Story(project_id=project.id, title=ps.title, body=ps.body, acceptance=ps.acceptance, status="proposed", source="feedback",
                      position=position, created_by=principal.user_id)
        position += 1
        db.add(story)
        db.flush()
        for n in ps.feedback_numbers:
            items[n - 1].status, items[n - 1].story_id = "clustered", story.id
        for eid in dict.fromkeys(ps.element_ids):
            db.add(TraceLink(project_id=project.id, from_type="story", from_id=str(story.id), to_type="element", to_id=eid, created_by=principal.user_id))
        created.append({**story_out(story), "feedback_ids": [str(items[n - 1].id) for n in ps.feedback_numbers], "element_ids": list(dict.fromkeys(ps.element_ids))})
    usage = _usage(r.usage, r.attempts)
    finish_ai_job(job, ok=True, provider=r.provider, model=r.model_name, usage=usage, result_ref=f"stories:{len(created)}")
    audit(db, request, "feedback.clustered", actor_id=principal.user_id, org_id=project.organization_id, target_type="application", target_id=app.id,
          meta={"feedback": len(items), "stories": len(created), "ai_job": str(job.id)})
    db.commit()
    return {"job_id": str(job.id), "stories": created, "unclustered": sum(1 for f in items if f.status == "new"), "dropped_stories": r.dropped,
            "usage": usage, "note": "AI-proposed stories start as 'proposed'; review them before moving them to the backlog."}
