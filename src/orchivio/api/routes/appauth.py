"""Application plane: end-user sign-in and the data/flow API of a generated application (FR-067).

Everything under ``/apps/{app_id}`` authenticates *application* users with their own cookie, scoped to this
application's path. Studio sessions and API tokens are not accepted here and app sessions are not accepted
anywhere else (``current_principal`` only knows the Studio cookie and ``orv_`` tokens), which keeps the two
security planes apart (FR-060). Roles are read from the database on every request, so a change takes effect at
once, and are intersected with the roles the current model defines.

Data is served by the preview runtime for now: there is no separate production data plane yet.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchivio.auth import DUMMY_HASH, hash_password, hash_token, matches, needs_rehash, new_token, password_problem, verify_password
from orchivio.db import to_model
from orchivio.db.models import Application, AppRevision, AppSession, AppUser, AppUserRole, AuditEvent, Project
from orchivio.logic import FlowError, FlowHooks, FlowRunner
from orchivio.model import AppModel
from orchivio.runtime import AppData, AppUser as RuntimeUser, DataError, PreviewNotBuilt, PreviewStale

from ..deps import api_error, get_db, not_found, utcnow
from ..observability import request_id_var

router = APIRouter(tags=["application plane"], prefix="/apps/{app_id}")

ANONYMOUS_ROLE = "anonymous"
_SAFE = {"GET", "HEAD", "OPTIONS"}


class LoginIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(max_length=1024)


class PasswordChangeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    current_password: str = Field(max_length=1024)
    new_password: str = Field(max_length=1024)


class RunIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    args: dict[str, Any] = Field(default_factory=dict)


@dataclass
class Caller:
    application: Application
    project: Project
    model: AppModel
    model_hash: str
    user: AppUser | None  # None = anonymous
    roles: frozenset[str]
    session: AppSession | None

    @property
    def runtime_user(self) -> RuntimeUser:
        return RuntimeUser(str(self.user.id) if self.user else "anonymous", self.roles)


def cookie_name(request: Request) -> str:
    return request.app.state.settings.session_cookie_name + "_app"


def cookie_path(app_id: uuid.UUID) -> str:
    return f"/api/v1/apps/{app_id}"


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _app(db: Session, app_id: uuid.UUID) -> tuple[Application, Project, AppModel, str]:
    app = db.get(Application, app_id)
    rev = db.get(AppRevision, app.current_revision_id) if app and app.current_revision_id else None
    if app is None or rev is None:
        raise not_found("application")  # an unknown application and one without a model look the same
    project = db.get(Project, app.project_id)
    assert project is not None
    return app, project, to_model(rev), rev.model_hash


def _audit(db: Session, request: Request, action: str, caller_app: Application, project: Project, user_id: uuid.UUID | None,
           **meta: Any) -> None:
    db.add(AuditEvent(organization_id=project.organization_id, actor_id=user_id, plane="application", channel="api", action=action,
                      target_type="app_user", target_id=str(user_id) if user_id else None,
                      meta={"request_id": request_id_var.get(), "application_id": str(caller_app.id), **meta}))


def _roles(db: Session, user: AppUser, model: AppModel) -> frozenset[str]:
    held = set(db.scalars(select(AppUserRole.role_key).where(AppUserRole.app_user_id == user.id)))
    return frozenset(held & set(model.security.roles))


def caller(app_id: uuid.UUID, request: Request, db: Session = Depends(get_db)) -> Caller:
    """The authenticated end user, or the anonymous caller when the model explicitly allows it."""
    app, project, model, mhash = _app(db, app_id)
    token = request.cookies.get(cookie_name(request))
    if token:
        sess = db.scalar(select(AppSession).where(AppSession.token_hash == hash_token(token)))
        if (sess is None or sess.application_id != app.id or sess.revoked_at is not None
                or (sess.expires_at if sess.expires_at.tzinfo else sess.expires_at.replace(tzinfo=utcnow().tzinfo)) <= utcnow()):
            raise api_error(401, "unauthorized", "sign in again")
        user = db.get(AppUser, sess.app_user_id)
        if user is None or user.status != "active":
            raise api_error(401, "unauthorized", "sign in again")
        if request.method not in _SAFE:
            sent = request.headers.get("x-csrf-token", "")
            if not sent or not matches(sent, sess.csrf_hash):
                raise api_error(403, "csrf_failed", "missing or invalid CSRF token")
        return Caller(app, project, model, mhash, user, _roles(db, user, model), sess)
    if model.security.anonymous:
        return Caller(app, project, model, mhash, None, frozenset({ANONYMOUS_ROLE} & set(model.security.roles)), None)
    raise api_error(401, "unauthorized", "authentication required")


def signed_in(c: Caller = Depends(caller)) -> Caller:
    """Endpoints that need a real account even when anonymous access is on."""
    if c.user is None:
        raise api_error(401, "unauthorized", "authentication required")
    return c


def usable(c: Caller = Depends(caller)) -> Caller:
    if c.user is not None and c.user.must_change_password:
        raise api_error(403, "password_change_required", "change your password first (POST auth/change-password)")
    return c


# --- authentication ----------------------------------------------------------------------------


def _user_json(c: Caller) -> dict[str, Any]:
    if c.user is None:
        return {"anonymous": True, "roles": sorted(c.roles)}
    return {"id": str(c.user.id), "username": c.user.username, "display_name": c.user.display_name,
            "roles": sorted(c.roles), "must_change_password": c.user.must_change_password, "anonymous": False}


def issue_session(db: Session, request: Request, response: Response, app: Application, user: AppUser) -> tuple[str, Any, AppSession]:
    """A new session for ``user`` (cookie on the response, CSRF token returned). The caller commits."""
    settings = request.app.state.settings
    tok, tok_hash = new_token("oas_")
    csrf, csrf_hash = new_token()
    expires = utcnow() + timedelta(hours=settings.session_ttl_hours)
    sess = AppSession(app_user_id=user.id, application_id=app.id, token_hash=tok_hash, csrf_hash=csrf_hash, expires_at=expires)
    db.add(sess)
    user.last_login_at = utcnow()
    response.set_cookie(cookie_name(request), tok, max_age=settings.session_ttl_hours * 3600, httponly=True,
                        secure=settings.secure_cookies, samesite="lax", path=cookie_path(app.id))
    return csrf, expires, sess


@router.post("/auth/login")
def login(app_id: uuid.UUID, body: LoginIn, request: Request, response: Response, db: Session = Depends(get_db)) -> dict[str, Any]:
    settings = request.app.state.settings
    throttle = request.app.state.throttle
    app, project, model, mhash = _app(db, app_id)
    username = body.username.strip().lower()
    ip_key, user_key = f"app:{app.id}:ip:{_client_ip(request)}", f"app:{app.id}:user:{username}"
    if throttle.blocked(ip_key, user_key):
        raise api_error(429, "too_many_attempts", "too many failed attempts; try again later",
                        headers={"Retry-After": str(settings.login_window_seconds)})
    user = db.scalar(select(AppUser).where(AppUser.application_id == app.id, AppUser.username == username))
    ok = verify_password(body.password, user.password_hash if user and user.password_hash else DUMMY_HASH)  # constant work
    if user is None or not ok or user.status != "active":
        throttle.fail(ip_key, user_key)
        _audit(db, request, "appauth.login_failed", app, project, user.id if user else None, username=username[:100])
        db.commit()
        raise api_error(401, "invalid_credentials", "invalid username or password")
    throttle.succeed(user_key)
    if needs_rehash(user.password_hash or ""):
        user.password_hash = hash_password(body.password)
    csrf, expires, sess = issue_session(db, request, response, app, user)
    _audit(db, request, "appauth.login", app, project, user.id)
    db.commit()
    c = Caller(app, project, model, mhash, user, _roles(db, user, model), sess)
    return {"user": _user_json(c), "csrf_token": csrf, "expires_at": expires.isoformat()}


@router.post("/auth/logout", status_code=204)
def logout(request: Request, response: Response, c: Caller = Depends(signed_in), db: Session = Depends(get_db)) -> Response:
    assert c.session is not None and c.user is not None
    c.session.revoked_at = utcnow()
    _audit(db, request, "appauth.logout", c.application, c.project, c.user.id)
    db.commit()
    response.delete_cookie(cookie_name(request), path=cookie_path(c.application.id))
    response.status_code = 204
    return response


@router.get("/auth/csrf")
def csrf_token(c: Caller = Depends(signed_in), db: Session = Depends(get_db)) -> dict[str, str]:
    """A fresh CSRF token for the current session (the previous one stops working). Needed after a sign-in that ended in a
    redirect, such as OIDC, where there was no response body to carry it. A safe method: cross-site pages cannot read it."""
    assert c.session is not None
    csrf, csrf_hash = new_token()
    c.session.csrf_hash = csrf_hash
    db.commit()
    return {"csrf_token": csrf}


@router.get("/auth/me")
def me(c: Caller = Depends(caller)) -> dict[str, Any]:
    return _user_json(c)


@router.post("/auth/change-password", status_code=204)
def change_password(body: PasswordChangeIn, request: Request, response: Response, c: Caller = Depends(signed_in),
                    db: Session = Depends(get_db)) -> Response:
    """Change the password, end every other session and keep this one."""
    assert c.user is not None and c.session is not None
    if not verify_password(body.current_password, c.user.password_hash or DUMMY_HASH):
        raise api_error(401, "invalid_credentials", "the current password is wrong")
    problem = password_problem(body.new_password)
    if problem:
        raise api_error(422, "weak_password", problem)
    if body.new_password == body.current_password:
        raise api_error(422, "weak_password", "choose a password different from the current one")
    c.user.password_hash, c.user.must_change_password = hash_password(body.new_password), False
    now = utcnow()
    for s in db.scalars(select(AppSession).where(AppSession.app_user_id == c.user.id, AppSession.revoked_at.is_(None),
                                                 AppSession.id != c.session.id)):
        s.revoked_at = now
    _audit(db, request, "appauth.password_changed", c.application, c.project, c.user.id)
    db.commit()
    response.status_code = 204
    return response


# --- data and flows ----------------------------------------------------------------------------


def _open(request: Request, c: Caller) -> AppData:
    try:
        return request.app.state.preview_store.open(
            c.application.id, c.model, c.model_hash, audit=request.app.state.audit_sink,
            organization_id=c.project.organization_id, hooks=FlowHooks(c.model),
            integrations=request.app.state.integration_clients(c.application.id))
    except PreviewNotBuilt as e:
        raise api_error(409, "not_available", "the application's data is not available yet") from e
    except PreviewStale as e:
        raise api_error(409, "not_available", "the application's data is being updated") from e


def _run(fn):
    try:
        return fn()
    except DataError as e:
        raise api_error(e.status, e.code, e.message, e.details) from e


@router.get("/data/{entity}")
def list_rows(entity: str, request: Request, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
              order_by: str | None = Query(None, max_length=63), desc: bool = False, c: Caller = Depends(usable)) -> dict[str, Any]:
    data = _open(request, c)
    filters = {k[2:]: v for k, v in request.query_params.items() if k.startswith("f.")}
    return _run(lambda: data.list(entity, c.runtime_user, filters=filters, order_by=order_by, descending=desc,
                                  limit=limit, offset=offset))


@router.post("/data/{entity}", status_code=201)
def create_row(entity: str, body: dict[str, Any], request: Request, c: Caller = Depends(usable)) -> dict[str, Any]:
    data = _open(request, c)
    return _run(lambda: data.create(entity, c.runtime_user, body))


@router.get("/data/{entity}/{row_id}")
def get_row(entity: str, row_id: str, request: Request, c: Caller = Depends(usable)) -> dict[str, Any]:
    data = _open(request, c)
    return _run(lambda: data.get(entity, c.runtime_user, row_id))


@router.patch("/data/{entity}/{row_id}")
def patch_row(entity: str, row_id: str, body: dict[str, Any], request: Request, c: Caller = Depends(usable)) -> dict[str, Any]:
    data = _open(request, c)
    return _run(lambda: data.update(entity, c.runtime_user, row_id, body))


@router.delete("/data/{entity}/{row_id}", status_code=204)
def delete_row(entity: str, row_id: str, request: Request, c: Caller = Depends(usable)) -> None:
    data = _open(request, c)
    _run(lambda: data.delete(entity, c.runtime_user, row_id))


@router.post("/flows/{flow}/run")
def run_flow(flow: str, body: RunIn, request: Request, c: Caller = Depends(usable)) -> dict[str, Any]:
    """Run a server flow as the caller; their data permissions apply to every step."""
    data = _open(request, c)
    q = flow if "." in flow else f"{c.model.modules[0].key}.{flow}"
    target = next(((m, f) for m in c.model.modules for f in m.flows if f"{m.key}.{f.key}" == q), None)
    if target is None or target[1].kind != "server":
        raise api_error(404, "unknown_flow", "unknown flow")
    if any(q == f"{m.key}.{h}" for m in c.model.modules for e in m.entities for h in e.hooks.values()):
        raise api_error(422, "not_runnable", "this flow is an entity hook; it runs when its event happens")
    try:
        result = FlowRunner(data.model, data).run(q, body.args, c.runtime_user)
    except FlowError as e:
        raise api_error(422, "flow_error", e.message) from e
    out = result.as_dict()
    out["trace"] = [{"step": s["step"], "status": s["status"]} for s in out.get("trace", [])]  # no internals for end users
    return out
