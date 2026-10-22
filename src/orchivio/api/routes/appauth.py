"""Application plane: end-user sign-in and the data/flow API of a generated application (FR-067).

Everything under ``/apps/{app_id}`` authenticates *application* users with their own cookie, scoped to this
application's path. Studio sessions and API tokens are not accepted here and app sessions are not accepted
anywhere else (``current_principal`` only knows the Studio cookie and ``orv_`` tokens), which keeps the two
security planes apart (FR-060). Roles are read from the database on every request, so a change takes effect at
once, and are intersected with the roles the current model defines.

Data is served by the preview runtime for now: there is no separate production data plane yet.
"""

from __future__ import annotations

import base64
import binascii
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from orchivio.auth import (
    DUMMY_HASH,
    hash_password,
    hash_token,
    matches,
    needs_rehash,
    new_token,
    password_problem,
    verify_password,
)
from orchivio.db import to_model
from orchivio.db.models import (
    Application,
    AppRevision,
    AppSession,
    AppUser,
    AppUserRole,
    AuditEvent,
    Environment,
    EnvironmentConstant,
    FeedbackItem,
    Project,
)
from orchivio.delivery import resolve_constants
from orchivio.logic import FlowError, FlowHooks, FlowRunner
from orchivio.model import AppModel
from orchivio.runtime import AppData, DataError, PreviewNotBuilt, PreviewStale
from orchivio.runtime import AppUser as RuntimeUser
from orchivio.runtime.production import ProductionStore

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


class RegisterIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(max_length=1024)
    display_name: str | None = Field(None, max_length=200)


class ResetRequestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=100)


class ResetConfirmIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(min_length=1, max_length=200)
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


def _load_app_constants(db: Session, app_id: uuid.UUID) -> dict[str, Any]:
    """Load environment constants from the highest-stage environment of this application (FR-096)."""
    env = db.scalar(
        select(Environment)
        .where(Environment.application_id == app_id)
        .order_by(Environment.stage_order.desc())
    )
    if env is None:
        return {}
    rows = list(db.scalars(select(EnvironmentConstant).where(EnvironmentConstant.environment_id == env.id)))
    return resolve_constants(rows)


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


def _production_env(db: Session, application_id: uuid.UUID) -> Environment | None:
    """Highest-stage environment that has a data_url (FR-030)."""
    from sqlalchemy import select as _select
    envs = db.scalars(
        _select(Environment).where(Environment.application_id == application_id, Environment.data_url.isnot(None))
        .order_by(Environment.stage_order.desc())
    ).all()
    return envs[0] if envs else None


def _open(request: Request, c: Caller, db: Session,
          constants: dict[str, Any] | None = None) -> AppData:
    if constants is None:
        constants = _load_app_constants(db, c.application.id)
    env = _production_env(db, c.application.id)
    if env is not None:
        prod = ProductionStore(env.data_url)
        return prod.open(c.application.id, c.model, c.model_hash, audit=request.app.state.audit_sink,
                         organization_id=c.project.organization_id, hooks=FlowHooks(c.model, constants=constants or None),
                         integrations=request.app.state.integration_clients(c.application.id))
    try:
        return request.app.state.preview_store.open(
            c.application.id, c.model, c.model_hash, audit=request.app.state.audit_sink,
            organization_id=c.project.organization_id, hooks=FlowHooks(c.model, constants=constants or None),
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
              order_by: str | None = Query(None, max_length=63), desc: bool = False,
              c: Caller = Depends(usable), db: Session = Depends(get_db)) -> dict[str, Any]:
    data = _open(request, c, db)
    filters = {k[2:]: v for k, v in request.query_params.items() if k.startswith("f.")}
    return _run(lambda: data.list(entity, c.runtime_user, filters=filters, order_by=order_by, descending=desc,
                                  limit=limit, offset=offset))


@router.post("/data/{entity}", status_code=201)
def create_row(entity: str, body: dict[str, Any], request: Request,
               c: Caller = Depends(usable), db: Session = Depends(get_db)) -> dict[str, Any]:
    data = _open(request, c, db)
    return _run(lambda: data.create(entity, c.runtime_user, body))


@router.get("/data/{entity}/{row_id}")
def get_row(entity: str, row_id: str, request: Request,
            c: Caller = Depends(usable), db: Session = Depends(get_db)) -> dict[str, Any]:
    data = _open(request, c, db)
    return _run(lambda: data.get(entity, c.runtime_user, row_id))


@router.patch("/data/{entity}/{row_id}")
def patch_row(entity: str, row_id: str, body: dict[str, Any], request: Request,
              c: Caller = Depends(usable), db: Session = Depends(get_db)) -> dict[str, Any]:
    data = _open(request, c, db)
    return _run(lambda: data.update(entity, c.runtime_user, row_id, body))


@router.delete("/data/{entity}/{row_id}", status_code=204)
def delete_row(entity: str, row_id: str, request: Request,
               c: Caller = Depends(usable), db: Session = Depends(get_db)) -> None:
    data = _open(request, c, db)
    _run(lambda: data.delete(entity, c.runtime_user, row_id))


class _AggMetric(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field: str = Field(default="*", max_length=63)
    fn: str = Field(default="count", pattern=r"^(count|sum|avg|min|max)$")
    alias: str = Field(max_length=63)


class _AggregateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    group_by: list[str] = Field(default_factory=list)
    metrics: list[_AggMetric]
    filters: dict[str, Any] | None = None


@router.post("/aggregate/{entity}")
def aggregate_rows(entity: str, body: _AggregateIn, request: Request,
                   c: Caller = Depends(usable), db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    """Aggregate query with row-level security applied (FR-060-063)."""
    data = _open(request, c, db)
    return _run(lambda: data.aggregate(entity, c.runtime_user, body.group_by, [m.model_dump() for m in body.metrics], body.filters))


@router.get("/grid/{entity}")
def grid_rows(entity: str, request: Request, limit: int = Query(50, ge=1, le=500),
              cursor: str | None = Query(None, max_length=100),
              sort: str | None = Query(None, max_length=200),
              c: Caller = Depends(usable), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Cursor-paginated, multi-sort data grid endpoint for the application plane (FR-074).

    Pass ``filter.<field>=<value>`` query params for equality filters.
    The response includes ``next_cursor`` for the following page (None when exhausted).
    """
    data = _open(request, c, db)
    offset = _decode_cursor(cursor)
    order_by, desc_sort = _parse_sort(sort)
    filters = {k[7:]: v for k, v in request.query_params.items() if k.startswith("filter.")}
    result = _run(lambda: data.list(entity, c.runtime_user, filters=filters or None, order_by=order_by,
                                    descending=desc_sort, limit=limit, offset=offset))
    items = result["items"]
    next_offset = offset + len(items)
    next_cursor = _encode_cursor(next_offset) if len(items) == limit else None
    return {"items": items, "total": result["total"], "limit": limit, "cursor": cursor, "next_cursor": next_cursor}


def _decode_cursor(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        return max(0, int(base64.b64decode(cursor.encode()).decode()))
    except Exception:
        return 0


def _encode_cursor(offset: int) -> str:
    return base64.b64encode(str(offset).encode()).decode()


def _parse_sort(sort: str | None) -> tuple[str | None, bool]:
    if not sort:
        return None, False
    first = sort.split(",")[0].strip()
    if ":" in first:
        field, direction = first.rsplit(":", 1)
        return field.strip() or None, direction.strip().lower() == "desc"
    return first or None, False


@router.post("/flows/{flow}/run")
def run_flow(flow: str, body: RunIn, request: Request,
             c: Caller = Depends(usable), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Run a server flow as the caller; their data permissions apply to every step."""
    constants = _load_app_constants(db, c.application.id)
    data = _open(request, c, db, constants)
    q = flow if "." in flow else f"{c.model.modules[0].key}.{flow}"
    target = next(((m, f) for m in c.model.modules for f in m.flows if f"{m.key}.{f.key}" == q), None)
    if target is None or target[1].kind != "server" or not (set(target[1].run_roles or []) & set(c.roles)):
        raise api_error(404, "unknown_flow", "unknown flow")  # not runnable by these roles looks the same as not existing
    if any(q == f"{m.key}.{h}" for m in c.model.modules for e in m.entities for h in e.hooks.values()):
        raise api_error(422, "not_runnable", "this flow is an entity hook; it runs when its event happens")
    try:
        result = FlowRunner(data.model, data, constants=constants or None).run(q, body.args, c.runtime_user)
    except FlowError as e:
        raise api_error(422, "flow_error", e.message) from e
    out = result.as_dict()
    out["trace"] = [{"step": s["step"], "status": s["status"]} for s in out.get("trace", [])]  # no internals for end users
    return out


# --- FR-067: self-registration and self-service password reset ----------------------------------------


@router.post("/auth/register", status_code=201)
def register_user(app_id: uuid.UUID, body: RegisterIn, request: Request,
                  db: Session = Depends(get_db)) -> dict[str, Any]:
    """Self-registration (FR-067). Requires model.security.allow_registration = true."""
    app, project, model, _ = _app(db, app_id)
    if not model.security.allow_registration:
        raise api_error(403, "registration_disabled", "self-registration is not enabled for this application")
    username = body.username.strip().lower()
    if db.scalar(select(AppUser).where(AppUser.application_id == app.id, AppUser.username == username)):
        raise api_error(409, "username_taken", "that username is already in use")
    problem = password_problem(body.password)
    if problem:
        raise api_error(422, "weak_password", problem)
    user = AppUser(application_id=app.id, username=username, display_name=body.display_name,
                   password_hash=hash_password(body.password), status="active")
    db.add(user)
    _audit(db, request, "appauth.registered", app, project, user.id, username=username[:100])
    db.commit()
    return {"id": str(user.id), "username": user.username, "display_name": user.display_name}


@router.post("/auth/reset-request", status_code=204)
def reset_request(app_id: uuid.UUID, body: ResetRequestIn,
                  db: Session = Depends(get_db)) -> Response:
    """Issue a self-service password-reset token (FR-067).

    The token is returned in the ``X-Reset-Token`` response header (not the body, to
    reduce accidental logging). The caller is responsible for delivering it to the user
    (e.g. via email). Returns 204 whether or not the username exists to prevent enumeration.
    Token is valid for one hour.
    """
    app, _, _, _ = _app(db, app_id)
    username = body.username.strip().lower()
    user = db.scalar(select(AppUser).where(AppUser.application_id == app.id, AppUser.username == username))
    if user is None or user.status != "active":
        return Response(status_code=204)  # constant-time: no 404 for unknown usernames
    plain, tok_hash = new_token("prt_")
    user.reset_token_hash = tok_hash
    user.reset_token_expires_at = utcnow() + timedelta(hours=1)
    db.commit()
    return Response(status_code=204, headers={"X-Reset-Token": plain})


@router.post("/auth/reset-confirm", status_code=204)
def reset_confirm(app_id: uuid.UUID, body: ResetConfirmIn,
                  db: Session = Depends(get_db)) -> Response:
    """Consume a password-reset token and set a new password (FR-067). Revokes all sessions."""
    app, _, _, _ = _app(db, app_id)
    tok_hash = hash_token(body.token)
    now = utcnow()
    user = db.scalar(
        select(AppUser).where(
            AppUser.application_id == app.id,
            AppUser.reset_token_hash == tok_hash,
            AppUser.reset_token_expires_at > now,
        )
    )
    if user is None:
        raise api_error(400, "invalid_token", "token is invalid or expired")
    problem = password_problem(body.new_password)
    if problem:
        raise api_error(422, "weak_password", problem)
    user.password_hash = hash_password(body.new_password)
    user.must_change_password = False
    user.reset_token_hash = None
    user.reset_token_expires_at = None
    for sess in db.scalars(select(AppSession).where(AppSession.app_user_id == user.id, AppSession.revoked_at.is_(None))):
        sess.revoked_at = now
    db.commit()
    return Response(status_code=204)


# --- end-user feedback (FR-099) ---------------------------------------------------------------

_FEEDBACK_PER_HOUR = 10
_MAX_SCREENSHOT = 512 * 1024
_IMAGES = [(b"\x89PNG", "image/png"), (b"\xff\xd8\xff", "image/jpeg")]


class AppFeedbackIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    page_key: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=2000)
    element_id: str | None = Field(default=None, max_length=64)
    screenshot_base64: str | None = Field(default=None, max_length=_MAX_SCREENSHOT * 2)


def _decode_screenshot(data: str) -> tuple[bytes, str]:
    try:
        raw = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError) as e:
        raise api_error(422, "invalid_screenshot", "the screenshot is not valid base64") from e
    if len(raw) > _MAX_SCREENSHOT:
        raise api_error(413, "screenshot_too_large", f"screenshots are limited to {_MAX_SCREENSHOT // 1024} KiB")
    for magic, kind in _IMAGES:
        if raw.startswith(magic):
            return raw, kind
    raise api_error(422, "invalid_screenshot", "only PNG and JPEG screenshots are accepted")


@router.post("/feedback", status_code=201)
def submit_app_feedback(app_id: uuid.UUID, body: AppFeedbackIn, request: Request,
                        c: Caller = Depends(usable), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Submit feedback from within a generated application (FR-099). Rate-limited per user."""
    user_id = c.user.id if c.user else None
    recent = db.scalar(select(func.count()).select_from(FeedbackItem).where(
        FeedbackItem.application_id == c.application.id,
        FeedbackItem.created_by == user_id,
        FeedbackItem.created_at >= utcnow() - timedelta(hours=1),
    ))
    if (recent or 0) >= _FEEDBACK_PER_HOUR:
        raise api_error(429, "too_much_feedback", f"at most {_FEEDBACK_PER_HOUR} feedback items per hour")
    shot, shot_type = _decode_screenshot(body.screenshot_base64) if body.screenshot_base64 else (None, None)
    f = FeedbackItem(application_id=c.application.id, page_key=body.page_key, element_id=body.element_id,
                     body=body.body, screenshot=shot, screenshot_type=shot_type, created_by=user_id)
    db.add(f)
    db.flush()
    _audit(db, request, "feedback.submitted", c.application, c.project, user_id,
           page_key=body.page_key, has_screenshot=shot is not None)
    db.commit()
    return {"id": str(f.id), "page_key": f.page_key, "status": f.status,
            "created_at": f.created_at.isoformat() if f.created_at else None}
