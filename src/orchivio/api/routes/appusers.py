"""Managing the end users of a generated application (Studio side; admin only).

These are application-plane identities. They are stored apart from Studio accounts and can never be used to
sign in to the Studio (docs/APP-SECURITY.md §6).
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from orchivio.auth import hash_password, password_problem
from orchivio.db import to_model
from orchivio.db.models import AppRevision, AppSession, AppUser, AppUserRole

from ..deps import Principal, api_error, audit, current_principal, get_db, load_application, not_found, utcnow
from ..schemas import Page

router = APIRouter(tags=["app users"])

USERNAME = r"^[a-z0-9][a-z0-9._@+-]{1,99}$"


class AppUserIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(pattern=USERNAME, description="lower-case; letters, digits and . _ @ + -")
    display_name: str | None = Field(default=None, max_length=200)
    password: str = Field(max_length=1024, description="initial password; the user must change it at first login")
    roles: list[str] = Field(default_factory=list, max_length=50)


class AppUserPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    display_name: str | None = Field(default=None, max_length=200)
    roles: list[str] | None = Field(default=None, max_length=50)
    status: str | None = Field(default=None, pattern="^(active|disabled)$")


class PasswordReset(BaseModel):
    model_config = ConfigDict(extra="forbid")
    password: str = Field(max_length=1024)


def user_out(db: Session, u: AppUser) -> dict[str, Any]:
    roles = sorted(db.scalars(select(AppUserRole.role_key).where(AppUserRole.app_user_id == u.id)))
    return {"id": str(u.id), "application_id": str(u.application_id), "username": u.username, "display_name": u.display_name,
            "status": u.status, "roles": roles, "must_change_password": u.must_change_password,
            "created_at": u.created_at.isoformat() if u.created_at else None,
            "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None}


def defined_roles(db: Session, app) -> set[str]:
    if app.current_revision_id is None:
        raise api_error(409, "no_revision", "the application has no model yet; roles come from its security model")
    return set(to_model(db.get(AppRevision, app.current_revision_id)).security.roles)


def _check_roles(db: Session, app, roles: list[str]) -> list[str]:
    unknown = sorted(set(roles) - defined_roles(db, app))
    if unknown:
        raise api_error(422, "unknown_role", "the model does not define these roles", {"unknown": unknown})
    return sorted(set(roles))


def _load(db: Session, principal: Principal, user_id: uuid.UUID):
    u = db.get(AppUser, user_id)
    if u is None:
        raise not_found("app user")
    app, project = load_application(db, principal, u.application_id, "appuser.manage")
    return u, app, project


def revoke_sessions(db: Session, user_id: uuid.UUID) -> None:
    now = utcnow()
    for s in db.scalars(select(AppSession).where(AppSession.app_user_id == user_id, AppSession.revoked_at.is_(None))):
        s.revoked_at = now


@router.post("/applications/{app_id}/app-users", status_code=201)
def create_user(app_id: uuid.UUID, body: AppUserIn, request: Request, principal: Principal = Depends(current_principal),
                db: Session = Depends(get_db)) -> dict[str, Any]:
    app, project = load_application(db, principal, app_id, "appuser.manage")
    problem = password_problem(body.password)
    if problem:
        raise api_error(422, "weak_password", problem)
    roles = _check_roles(db, app, body.roles)
    u = AppUser(application_id=app.id, username=body.username, display_name=body.display_name,
                password_hash=hash_password(body.password), must_change_password=True)
    db.add(u)
    try:
        db.flush()
    except IntegrityError as e:
        db.rollback()
        raise api_error(409, "username_taken", "this username already exists in the application") from e
    db.add_all(AppUserRole(app_user_id=u.id, role_key=r) for r in roles)
    audit(db, request, "appuser.created", actor_id=principal.user_id, org_id=project.organization_id,
          target_type="app_user", target_id=u.id, meta={"application_id": str(app.id), "username": u.username, "roles": roles})
    db.commit()
    return user_out(db, u)


@router.get("/applications/{app_id}/app-users", response_model=Page[dict])
def list_users(app_id: uuid.UUID, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
               principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> Page[Any]:
    app, _ = load_application(db, principal, app_id, "appuser.manage")
    q = select(AppUser).where(AppUser.application_id == app.id)
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    rows = db.scalars(q.order_by(AppUser.username).limit(limit).offset(offset)).all()
    return Page(items=[user_out(db, u) for u in rows], limit=limit, offset=offset, total=total)


@router.get("/app-users/{user_id}")
def get_user(user_id: uuid.UUID, principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict[str, Any]:
    u, _, _ = _load(db, principal, user_id)
    return user_out(db, u)


@router.patch("/app-users/{user_id}")
def update_user(user_id: uuid.UUID, body: AppUserPatch, request: Request, principal: Principal = Depends(current_principal),
                db: Session = Depends(get_db)) -> dict[str, Any]:
    u, app, project = _load(db, principal, user_id)
    meta: dict[str, Any] = {"username": u.username}
    if body.display_name is not None:
        u.display_name = body.display_name
    if body.roles is not None:
        roles = _check_roles(db, app, body.roles)
        db.execute(delete(AppUserRole).where(AppUserRole.app_user_id == u.id))
        db.add_all(AppUserRole(app_user_id=u.id, role_key=r) for r in roles)
        meta["roles"] = roles
    if body.status is not None:
        u.status = body.status
        meta["status"] = body.status
        if body.status == "disabled":
            revoke_sessions(db, u.id)
    audit(db, request, "appuser.updated", actor_id=principal.user_id, org_id=project.organization_id,
          target_type="app_user", target_id=u.id, meta=meta)
    db.commit()
    return user_out(db, u)


@router.post("/app-users/{user_id}/reset-password")
def reset_password(user_id: uuid.UUID, body: PasswordReset, request: Request, principal: Principal = Depends(current_principal),
                   db: Session = Depends(get_db)) -> dict[str, Any]:
    """Set a new temporary password; every session of the user ends and a change is required at next login."""
    u, _, project = _load(db, principal, user_id)
    problem = password_problem(body.password)
    if problem:
        raise api_error(422, "weak_password", problem)
    u.password_hash, u.must_change_password = hash_password(body.password), True
    revoke_sessions(db, u.id)
    audit(db, request, "appuser.password_reset", actor_id=principal.user_id, org_id=project.organization_id,
          target_type="app_user", target_id=u.id, meta={"username": u.username})
    db.commit()
    return user_out(db, u)


@router.delete("/app-users/{user_id}", status_code=204)
def delete_user(user_id: uuid.UUID, request: Request, principal: Principal = Depends(current_principal),
                db: Session = Depends(get_db)) -> Response:
    u, _, project = _load(db, principal, user_id)
    audit(db, request, "appuser.deleted", actor_id=principal.user_id, org_id=project.organization_id,
          target_type="app_user", target_id=u.id, meta={"username": u.username})
    db.delete(u)
    db.commit()
    return Response(status_code=204)
