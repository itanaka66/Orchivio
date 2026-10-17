"""Request-scoped dependencies: database session, authentication, authorization, audit."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchivio.auth import API_TOKEN_PREFIX, hash_token, matches
from orchivio.db.models import (
    Application,
    ApiToken,
    AppRevision,
    AuditEvent,
    Membership,
    Project,
    User,
    UserSession,
)
from orchivio.policy import is_allowed

from .observability import request_id_var

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def api_error(status: int, code: str, message: str, details: Any = None, headers: dict[str, str] | None = None) -> HTTPException:
    return HTTPException(status, {"error": {"code": code, "message": message, "details": details}}, headers=headers)


def not_found(what: str = "resource") -> HTTPException:
    """404 with an identical body whether the resource is missing or hidden from the caller.

    ``what`` is for call-site readability only and is deliberately not put in the response.
    """
    return api_error(404, "not_found", "not found")


def utcnow() -> datetime:
    return datetime.now(UTC)


def aware(dt: datetime) -> datetime:
    """SQLite returns naive datetimes; the application stores UTC."""
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def get_db(request: Request) -> Iterator[Session]:
    with request.app.state.session_factory() as session:
        yield session


@dataclass(frozen=True)
class Principal:
    user: User
    via: str  # "session" | "token"
    session: UserSession | None = None
    token: ApiToken | None = None

    @property
    def user_id(self) -> uuid.UUID:
        return self.user.id


def _active_user(db: Session, user_id: uuid.UUID) -> User | None:
    user = db.get(User, user_id)
    return user if user is not None and user.status == "active" else None


def current_principal(request: Request, db: Session = Depends(get_db)) -> Principal:
    settings = request.app.state.settings
    auth = request.headers.get("authorization")
    if auth is not None:
        scheme, _, value = auth.partition(" ")
        if scheme.lower() != "bearer" or not value.startswith(API_TOKEN_PREFIX):
            raise api_error(401, "unauthorized", "invalid credentials")
        tok = db.scalar(select(ApiToken).where(ApiToken.token_hash == hash_token(value)))
        if (
            tok is None
            or not matches(value, tok.token_hash)
            or tok.revoked_at is not None
            or aware(tok.expires_at) <= utcnow()
        ):
            raise api_error(401, "unauthorized", "invalid credentials")
        user = _active_user(db, tok.owner_id)
        if user is None:
            raise api_error(401, "unauthorized", "invalid credentials")
        return Principal(user, "token", token=tok)

    cookie = request.cookies.get(settings.session_cookie_name)
    if not cookie:
        raise api_error(401, "unauthorized", "authentication required")
    sess = db.scalar(select(UserSession).where(UserSession.token_hash == hash_token(cookie)))
    if sess is None or sess.revoked_at is not None or aware(sess.expires_at) <= utcnow():
        raise api_error(401, "unauthorized", "authentication required")
    user = _active_user(db, sess.user_id)
    if user is None:
        raise api_error(401, "unauthorized", "authentication required")
    if request.method not in _SAFE_METHODS:
        sent = request.headers.get("x-csrf-token", "")
        if not sent or not matches(sent, sess.csrf_hash):
            raise api_error(403, "csrf_failed", "missing or invalid CSRF token")
    return Principal(user, "session", session=sess)


# --- authorization -----------------------------------------------------------


def member_role(db: Session, user_id: uuid.UUID, org_id: uuid.UUID) -> str | None:
    return db.scalar(
        select(Membership.role).where(Membership.user_id == user_id, Membership.organization_id == org_id)
    )


def authorize(
    db: Session,
    principal: Principal,
    org_id: uuid.UUID,
    action: str,
    *,
    project_id: uuid.UUID | None = None,
) -> str:
    """Return the caller's role in ``org_id`` or raise.

    404 when the caller is not a member (existence is not revealed), 403 when they are a
    member but lack the role or the token lacks the scope.
    """
    tok = principal.token
    if tok is not None:
        if tok.organization_id != org_id:
            raise not_found()
        if tok.project_id is not None and tok.project_id != project_id:
            raise api_error(403, "forbidden", "token is limited to a different project")
    role = member_role(db, principal.user_id, org_id)
    if role is None:
        raise not_found()
    if tok is not None and "*" not in tok.scopes and action not in tok.scopes:
        raise api_error(403, "forbidden", "token scope does not allow this action")
    if not is_allowed(role, action):
        raise api_error(403, "forbidden", "your role does not allow this action")
    return role


def load_project(db: Session, principal: Principal, project_id: uuid.UUID, action: str) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise not_found("project")
    authorize(db, principal, project.organization_id, action, project_id=project.id)
    return project


def load_application(
    db: Session, principal: Principal, app_id: uuid.UUID, action: str
) -> tuple[Application, Project]:
    app = db.get(Application, app_id)
    if app is None:
        raise not_found("application")
    project = load_project(db, principal, app.project_id, action)
    return app, project


def load_revision(
    db: Session, principal: Principal, revision_id: uuid.UUID, action: str
) -> tuple[AppRevision, Application, Project]:
    rev = db.get(AppRevision, revision_id)
    if rev is None:
        raise not_found("revision")
    app, project = load_application(db, principal, rev.application_id, action)
    return rev, app, project


# --- audit -------------------------------------------------------------------


def audit(
    db: Session,
    request: Request,
    action: str,
    *,
    actor_id: uuid.UUID | None,
    org_id: uuid.UUID | None = None,
    target_type: str | None = None,
    target_id: object = None,
    meta: dict[str, Any] | None = None,
) -> None:
    """Add an audit row to the current transaction; the caller commits."""
    db.add(
        AuditEvent(
            organization_id=org_id,
            actor_id=actor_id,
            plane="platform",
            channel="api",
            action=action,
            target_type=target_type,
            target_id=None if target_id is None else str(target_id),
            meta={"request_id": request_id_var.get(), **(meta or {})},
        )
    )
