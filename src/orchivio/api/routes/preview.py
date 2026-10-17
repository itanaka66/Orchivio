"""Preview environment: run the model as an application with synthetic data.

The preview is the only place where Studio users touch application data, and only through an
explicit *simulation*: the caller names the end-user id and roles to act as ("view as role",
FR-068). The caller's Studio role decides whether they may use the preview at all; it never
grants rights inside the simulated application, which are decided by the model's access rules.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Header, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from orchivio.db import to_model
from orchivio.logic import FlowError, FlowHooks, FlowRunner
from orchivio.db.models import Application, AppRevision
from orchivio.runtime import (
    AppData,
    AppUser,
    DataError,
    PreviewNotBuilt,
    PreviewStale,
    generate,
)

from ..deps import Principal, api_error, audit, current_principal, get_db, load_application

router = APIRouter(tags=["preview"])


class SeedIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rows_per_entity: int = Field(default=10, ge=0, le=200)
    seed: int = Field(default=0, ge=0, le=2**31)
    users: list[str] = Field(default_factory=lambda: ["preview-user"], min_length=1, max_length=10)


class ResetIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    seed: SeedIn | None = None


def _current(db: Session, app: Application) -> AppRevision:
    rev = db.get(AppRevision, app.current_revision_id) if app.current_revision_id else None
    if rev is None:
        raise api_error(409, "no_revision", "the application has no model yet")
    return rev


@router.post("/applications/{app_id}/preview/reset")
def reset(app_id: uuid.UUID, body: ResetIn, request: Request, principal: Principal = Depends(current_principal),
          db: Session = Depends(get_db)) -> dict[str, Any]:
    """Rebuild the preview tables from the current revision (all preview data is discarded)."""
    app, project = load_application(db, principal, app_id, "model.propose")
    rev = _current(db, app)
    model = to_model(rev)
    store = request.app.state.preview_store
    sink = request.app.state.audit_sink
    schema, data = store.reset(app.id, model, rev.model_hash, audit=sink, organization_id=project.organization_id,
                               hooks=FlowHooks(model))
    seeded = None
    if body.seed is not None:
        users = tuple(u for u in body.seed.users if 1 <= len(u) <= 64)
        seeded = generate(schema, data, rows_per_entity=body.seed.rows_per_entity, seed=body.seed.seed, users=users)
    audit(db, request, "preview.reset", actor_id=principal.user_id, org_id=project.organization_id,
          target_type="application", target_id=app.id, meta={"revision_no": rev.revision_no, "seeded": seeded is not None})
    db.commit()
    return {"revision_id": str(rev.id), "model_hash": rev.model_hash, "entities": sorted(schema.entities),
            "seeded": seeded}


@router.get("/applications/{app_id}/preview/status")
def status(app_id: uuid.UUID, request: Request, principal: Principal = Depends(current_principal),
           db: Session = Depends(get_db)) -> dict[str, Any]:
    app, _ = load_application(db, principal, app_id, "model.read")
    st = request.app.state.preview_store.status(app.id)
    rev = db.get(AppRevision, app.current_revision_id) if app.current_revision_id else None
    if st is None:
        return {"built": False, "stale": False, "built_at": None}
    return {"built": True, "stale": rev is None or st["model_hash"] != rev.model_hash, "built_at": st["built_at"].isoformat()}


# --- flows ---------------------------------------------------------------------------------


class RunFlowIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    args: dict[str, Any] = Field(default_factory=dict)


@router.post("/applications/{app_id}/preview/flows/{flow}/run")
def run_flow(app_id: uuid.UUID, flow: str, body: RunFlowIn, request: Request,
             x_preview_user: str | None = Header(default=None, alias="X-Preview-User"),
             x_preview_roles: str | None = Header(default=None, alias="X-Preview-Roles"),
             principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Run a Server Flow in the preview as the simulated user. The run is one transaction under that
    user's application security; the response carries the step-by-step trace."""
    data, user = _open(request, db, principal, app_id, "model.propose", x_preview_user, x_preview_roles)
    q = flow if "." in flow else f"{data.model.modules[0].key}.{flow}"
    target = next(((m, f) for m in data.model.modules for f in m.flows if f"{m.key}.{f.key}" == q), None)
    if target is None:
        raise api_error(404, "unknown_flow", "unknown flow")
    mod, fl = target
    if fl.kind != "server":
        raise api_error(422, "not_runnable", "client flows run in the browser, not in the preview server")
    if any(q == f"{m.key}.{h}" for m in data.model.modules for e in m.entities for h in e.hooks.values()):
        raise api_error(422, "not_runnable", "this flow is an entity hook; it runs when its event happens")
    try:
        result = FlowRunner(data.model, data).run(q, body.args, user)
    except FlowError as e:  # e.g. an unknown flow referenced inside the run
        raise api_error(422, "flow_error", e.message) from e
    _, project = load_application(db, principal, app_id, "model.read")
    audit(db, request, "flow.run", actor_id=principal.user_id, org_id=project.organization_id, target_type="flow",
          target_id=q, meta={"status": result.status, "steps": result.steps_executed, "as_user": user.id})
    db.commit()
    return result.as_dict()


# --- data ----------------------------------------------------------------------------------


def _open(request: Request, db: Session, principal: Principal, app_id: uuid.UUID, action: str,
          x_preview_user: str | None, x_preview_roles: str | None) -> tuple[AppData, AppUser]:
    app, project = load_application(db, principal, app_id, action)
    rev = _current(db, app)
    model = to_model(rev)
    roles = frozenset(r.strip() for r in (x_preview_roles or "").split(",") if r.strip())
    unknown = sorted(roles - set(model.security.roles))
    if unknown:
        raise api_error(422, "unknown_role", "X-Preview-Roles names roles the model does not define",
                        {"unknown": unknown, "defined": model.security.roles})
    user_id = (x_preview_user or f"studio:{principal.user_id}").strip()
    if not 1 <= len(user_id) <= 64:
        raise api_error(422, "invalid_request", "X-Preview-User must be 1-64 characters")
    try:
        data = request.app.state.preview_store.open(app.id, model, rev.model_hash, audit=request.app.state.audit_sink,
                                                   organization_id=project.organization_id, hooks=FlowHooks(model),
                                                   integrations=request.app.state.integration_clients(app.id))
    except PreviewNotBuilt as e:
        raise api_error(409, "preview_not_built", "reset the preview first") from e
    except PreviewStale as e:
        raise api_error(409, "preview_stale", "the model changed since the preview was built; reset it") from e
    return data, AppUser(user_id, roles)


def _run(fn):
    try:
        return fn()
    except DataError as e:
        raise api_error(e.status, e.code, e.message, e.details) from e


_USER = Header(default=None, alias="X-Preview-User", description="End-user id to act as")
_ROLES = Header(default=None, alias="X-Preview-Roles", description="Comma-separated app roles to act with")


@router.get("/applications/{app_id}/preview/data/{entity}")
def list_rows(app_id: uuid.UUID, entity: str, request: Request, limit: int = Query(50, ge=1, le=200),
              offset: int = Query(0, ge=0), order_by: str | None = Query(None, max_length=63), desc: bool = False,
              x_preview_user: str | None = _USER, x_preview_roles: str | None = _ROLES,
              principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict[str, Any]:
    data, user = _open(request, db, principal, app_id, "model.read", x_preview_user, x_preview_roles)
    filters = {k[2:]: v for k, v in request.query_params.items() if k.startswith("f.")}
    return _run(lambda: data.list(entity, user, filters=filters, order_by=order_by, descending=desc,
                                  limit=limit, offset=offset))


@router.post("/applications/{app_id}/preview/data/{entity}", status_code=201)
def create_row(app_id: uuid.UUID, entity: str, body: dict[str, Any], request: Request,
               x_preview_user: str | None = _USER, x_preview_roles: str | None = _ROLES,
               principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict[str, Any]:
    data, user = _open(request, db, principal, app_id, "model.propose", x_preview_user, x_preview_roles)
    return _run(lambda: data.create(entity, user, body))


@router.get("/applications/{app_id}/preview/data/{entity}/{row_id}")
def get_row(app_id: uuid.UUID, entity: str, row_id: str, request: Request,
            x_preview_user: str | None = _USER, x_preview_roles: str | None = _ROLES,
            principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict[str, Any]:
    data, user = _open(request, db, principal, app_id, "model.read", x_preview_user, x_preview_roles)
    return _run(lambda: data.get(entity, user, row_id))


@router.patch("/applications/{app_id}/preview/data/{entity}/{row_id}")
def patch_row(app_id: uuid.UUID, entity: str, row_id: str, body: dict[str, Any], request: Request,
              x_preview_user: str | None = _USER, x_preview_roles: str | None = _ROLES,
              principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict[str, Any]:
    data, user = _open(request, db, principal, app_id, "model.propose", x_preview_user, x_preview_roles)
    return _run(lambda: data.update(entity, user, row_id, body))


@router.delete("/applications/{app_id}/preview/data/{entity}/{row_id}", status_code=204)
def delete_row(app_id: uuid.UUID, entity: str, row_id: str, request: Request,
               x_preview_user: str | None = _USER, x_preview_roles: str | None = _ROLES,
               principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> None:
    data, user = _open(request, db, principal, app_id, "model.propose", x_preview_user, x_preview_roles)
    _run(lambda: data.delete(entity, user, row_id))

