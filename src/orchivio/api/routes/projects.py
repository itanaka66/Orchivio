"""Projects, applications and model revisions."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from orchivio.db import ModelInvalid, ProposalRepository, RevisionRepository, to_model
from orchivio.codegen import build_openapi
from orchivio.db.models import Application, AppRevision, Project
from orchivio.db.proposals import NoChanges
from orchivio.model import CanonicalError, ModelDiff, build_archive, read_archive
from orchivio.policy import ApprovalLevel

from ..deps import (
    Principal,
    api_error,
    audit,
    authorize,
    current_principal,
    get_db,
    load_application,
    load_project,
    load_revision,
    not_found,
)
from ..modelio import load_model
from .proposals import proposal_detail
from ..schemas import (
    AppIn,
    AppOut,
    Page,
    ProjectIn,
    ProjectOut,
    ProjectPatch,
    RevisionDetail,
    RevisionIn,
    RevisionOut,
    RollbackIn,
)

router = APIRouter(tags=["projects"])


# --- projects ----------------------------------------------------------------


@router.post("/organizations/{org_id}/projects", status_code=201, response_model=ProjectOut)
def create_project(org_id: uuid.UUID, body: ProjectIn, request: Request,
                   principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> Project:
    authorize(db, principal, org_id, "project.create")
    if db.scalar(select(Project.id).where(Project.organization_id == org_id, Project.slug == body.slug)):
        raise api_error(409, "slug_taken", "project slug already in use in this organization")
    project = Project(organization_id=org_id, name=body.name, slug=body.slug, settings=body.settings)
    db.add(project)
    db.flush()
    audit(db, request, "project.created", actor_id=principal.user_id, org_id=org_id,
          target_type="project", target_id=project.id, meta={"slug": project.slug})
    try:
        db.commit()
    except IntegrityError as e:
        db.rollback()
        raise api_error(409, "slug_taken", "project slug already in use in this organization") from e
    return project


@router.get("/organizations/{org_id}/projects", response_model=Page[ProjectOut])
def list_projects(org_id: uuid.UUID, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                  principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> Page[Any]:
    authorize(db, principal, org_id, "project.read")
    q = select(Project).where(Project.organization_id == org_id)
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    items = db.scalars(q.order_by(Project.name).limit(limit).offset(offset)).all()
    return Page(items=[ProjectOut.model_validate(p) for p in items], limit=limit, offset=offset, total=total)


@router.get("/projects/{project_id}", response_model=ProjectOut)
def get_project(project_id: uuid.UUID, principal: Principal = Depends(current_principal),
                db: Session = Depends(get_db)) -> Project:
    return load_project(db, principal, project_id, "project.read")


@router.patch("/projects/{project_id}", response_model=ProjectOut)
def update_project(project_id: uuid.UUID, body: ProjectPatch, request: Request,
                   principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> Project:
    project = load_project(db, principal, project_id, "project.update")
    changed = []
    if body.name is not None:
        project.name = body.name
        changed.append("name")
    if body.settings is not None:
        project.settings = body.settings
        changed.append("settings")
    audit(db, request, "project.updated", actor_id=principal.user_id, org_id=project.organization_id,
          target_type="project", target_id=project.id, meta={"fields": changed})
    db.commit()
    return project


@router.delete("/projects/{project_id}", status_code=204)
def delete_project(project_id: uuid.UUID, request: Request,
                   confirm: str | None = Query(None, description="Must equal the project slug"),
                   principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> Response:
    project = load_project(db, principal, project_id, "project.delete")
    if confirm != project.slug:
        raise api_error(400, "confirmation_required", "pass ?confirm=<project slug> to delete this project")
    audit(db, request, "project.deleted", actor_id=principal.user_id, org_id=project.organization_id,
          target_type="project", target_id=project.id, meta={"slug": project.slug})
    db.delete(project)
    db.commit()
    return Response(status_code=204)


# --- applications ------------------------------------------------------------


def _app_out(db: Session, app: Application) -> AppOut:
    no = None
    if app.current_revision_id:
        no = db.scalar(select(AppRevision.revision_no).where(AppRevision.id == app.current_revision_id))
    return AppOut(id=app.id, project_id=app.project_id, key=app.key, name=app.name,
                  current_revision_id=app.current_revision_id, current_revision_no=no, created_at=app.created_at)


def _model_invalid(e: ModelInvalid) -> Any:
    return api_error(422, "model_invalid", "the model has validation errors",
                     [i.as_dict() for i in e.issues if i.severity == "error"])


def _gate_risks(diff: ModelDiff | None, acknowledged: bool) -> None:
    """Changes needing A3 or above are applied only with an explicit acknowledgement."""
    if diff is None or diff.is_empty:
        return
    if diff.level >= ApprovalLevel.A3 and not acknowledged:
        raise api_error(409, "approval_required",
                        f"this change needs {diff.level.name} approval; resend with acknowledge_risks=true",
                        {"level": diff.level.name, "risky_changes": [c.as_dict() for c in diff.risky],
                         "security_changed": diff.security_changed})


@router.post("/projects/{project_id}/applications", status_code=201, response_model=AppOut)
def create_application(project_id: uuid.UUID, body: AppIn, request: Request,
                       principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> AppOut:
    project = load_project(db, principal, project_id, "application.create")
    if db.scalar(select(Application.id).where(Application.project_id == project.id, Application.key == body.key)):
        raise api_error(409, "key_taken", "application key already in use in this project")
    model = load_model(body.model) if body.model is not None else None
    app = Application(project_id=project.id, key=body.key, name=body.name)
    db.add(app)
    db.flush()
    audit(db, request, "application.created", actor_id=principal.user_id, org_id=project.organization_id,
          target_type="application", target_id=app.id, meta={"key": app.key})
    if model is not None:
        authorize(db, principal, project.organization_id, "model.apply", project_id=project.id)
        try:
            RevisionRepository(db).create(app.id, model, created_by=principal.user_id, message=body.message,
                                          organization_id=project.organization_id)
        except ModelInvalid as e:
            db.rollback()
            raise _model_invalid(e) from e
    db.commit()
    return _app_out(db, app)


@router.get("/projects/{project_id}/applications", response_model=Page[AppOut])
def list_applications(project_id: uuid.UUID, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                      principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> Page[AppOut]:
    project = load_project(db, principal, project_id, "project.read")
    q = select(Application).where(Application.project_id == project.id)
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    apps = db.scalars(q.order_by(Application.name).limit(limit).offset(offset)).all()
    return Page(items=[_app_out(db, a) for a in apps], limit=limit, offset=offset, total=total)


@router.get("/applications/{app_id}", response_model=AppOut)
def get_application(app_id: uuid.UUID, principal: Principal = Depends(current_principal),
                    db: Session = Depends(get_db)) -> AppOut:
    app, _ = load_application(db, principal, app_id, "project.read")
    return _app_out(db, app)


# --- revisions ---------------------------------------------------------------


@router.post("/applications/{app_id}/revisions", status_code=201, response_model=RevisionOut)
def create_revision(app_id: uuid.UUID, body: RevisionIn, request: Request,
                    principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> AppRevision:
    app, project = load_application(db, principal, app_id, "model.apply")
    repo = RevisionRepository(db)
    plan = repo.plan(app.id, load_model(body.model))
    if not plan.valid:
        raise _model_invalid(ModelInvalid(plan.issues))
    if plan.diff is not None and plan.diff.is_empty:
        raise api_error(409, "no_changes", "the model is identical to the current revision")
    _gate_risks(plan.diff, body.acknowledge_risks)
    rev = repo.create(app.id, plan.model, created_by=principal.user_id, message=body.message,
                      organization_id=project.organization_id)
    db.commit()
    return rev


@router.get("/applications/{app_id}/revisions", response_model=Page[RevisionOut])
def list_revisions(app_id: uuid.UUID, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                   principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> Page[Any]:
    app, _ = load_application(db, principal, app_id, "model.read")
    total = db.scalar(select(func.count()).select_from(AppRevision).where(AppRevision.application_id == app.id)) or 0
    revs = RevisionRepository(db).list(app.id, limit=limit, offset=offset)
    return Page(items=[RevisionOut.model_validate(r) for r in revs], limit=limit, offset=offset, total=total)


@router.get("/revisions/{revision_id}", response_model=RevisionDetail)
def get_revision(revision_id: uuid.UUID, principal: Principal = Depends(current_principal),
                 db: Session = Depends(get_db)) -> AppRevision:
    rev, _, _ = load_revision(db, principal, revision_id, "model.read")
    return rev


@router.get("/revisions/{revision_id}/diff/{base_revision_id}")
def diff_revisions(revision_id: uuid.UUID, base_revision_id: uuid.UUID,
                   principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict[str, Any]:
    rev, app, _ = load_revision(db, principal, revision_id, "model.read")
    base = db.get(AppRevision, base_revision_id)
    if base is None or base.application_id != app.id:
        raise not_found("revision")
    d = RevisionRepository(db).diff(rev.id, base.id)
    return {"level": d.level.name, "security_changed": d.security_changed,
            "changes": [c.as_dict() for c in d.changes]}


@router.get("/revisions/{revision_id}/export")
def export_revision(revision_id: uuid.UUID, request: Request, principal: Principal = Depends(current_principal),
                    db: Session = Depends(get_db)) -> Response:
    """Download the revision as a reproducible archive of canonical per-element files. Admin only:
    a model describes the application's data and rules, so exporting it is a distinct permission."""
    rev, app, project = load_revision(db, principal, revision_id, "project.export")
    data = build_archive(to_model(rev))
    audit(db, request, "revision.exported", actor_id=principal.user_id, org_id=project.organization_id,
          target_type="app_revision", target_id=rev.id, meta={"revision_no": rev.revision_no, "bytes": len(data)})
    db.commit()
    name = f"{app.key}-r{rev.revision_no}.orchivio.zip"
    return Response(data, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.post("/applications/{app_id}/import", status_code=201)
async def import_archive(app_id: uuid.UUID, request: Request, title: str = Query("Import from archive", max_length=300),
                         principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> Any:
    """Upload an archive (raw ``application/zip`` body). It is verified and becomes a *proposal*; an import
    never changes the model directly."""
    limit = request.app.state.settings.max_import_bytes
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise api_error(413, "payload_too_large", "archive too large")
        chunks.append(chunk)
    body = b"".join(chunks)

    def work() -> Any:
        app, project = load_application(db, principal, app_id, "model.propose")
        try:
            model = read_archive(body)
        except CanonicalError as e:
            raise api_error(422, "invalid_archive", e.message, {"path": e.path}) from e
        try:
            p = ProposalRepository(db).create(app.id, model, source="human", title=title, created_by=principal.user_id,
                                              organization_id=project.organization_id)
        except ModelInvalid as e:
            raise _model_invalid(e) from e
        except NoChanges as e:
            raise api_error(409, "no_changes", str(e)) from e
        db.commit()
        return proposal_detail(p)

    return await run_in_threadpool(work)


@router.get("/revisions/{revision_id}/openapi.json")
def revision_openapi(revision_id: uuid.UUID, principal: Principal = Depends(current_principal),
                     db: Session = Depends(get_db)) -> dict[str, Any]:
    """OpenAPI 3.1 document of the application's data API as defined by this revision."""
    rev, app, _ = load_revision(db, principal, revision_id, "model.read")
    spec = build_openapi(to_model(rev), version=f"{to_model(rev).application.version}+r{rev.revision_no}")
    spec["info"]["x-orchivio-model-hash"] = rev.model_hash
    return spec


@router.get("/revisions/{revision_id}/lint")
def revision_lint(revision_id: uuid.UUID, include_suppressed: bool = Query(False),
                  principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict[str, Any]:
    rev, _, _ = load_revision(db, principal, revision_id, "model.read")
    rows = RevisionRepository(db).lint_findings(rev.id)
    items = [{"rule_id": f.rule_id, "severity": f.severity, "element_id": str(f.element_id) if f.element_id else None,
              "message": f.message, "suppressed": f.suppressed, "justification": f.justification}
             for f in rows if include_suppressed or not f.suppressed]
    return {"items": items, "total": len(items)}


@router.get("/revisions/{revision_id}/elements/{element_id}/usages")
def element_usages(revision_id: uuid.UUID, element_id: uuid.UUID,
                   principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Find usages: which elements of this revision depend on ``element_id``."""
    rev, _, _ = load_revision(db, principal, revision_id, "model.read")
    refs = RevisionRepository(db).usages(rev.id, element_id)
    return {"element_id": str(element_id),
            "used_by": [{"element_id": str(r.from_element_id), "kind": r.ref_kind} for r in refs]}


@router.post("/applications/{app_id}/rollback", status_code=201, response_model=RevisionOut)
def rollback(app_id: uuid.UUID, body: RollbackIn, request: Request,
             principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> AppRevision:
    app, project = load_application(db, principal, app_id, "model.rollback")
    target = db.get(AppRevision, body.revision_id)
    if target is None or target.application_id != app.id:
        raise not_found("revision")
    repo = RevisionRepository(db)
    plan = repo.plan(app.id, to_model(target))
    if plan.diff is not None and plan.diff.is_empty:
        raise api_error(409, "no_changes", "the current revision already matches the target")
    _gate_risks(plan.diff, body.acknowledge_risks)
    rev = repo.rollback(app.id, target.id, created_by=principal.user_id, organization_id=project.organization_id)
    db.commit()
    return rev

