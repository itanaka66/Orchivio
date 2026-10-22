"""Registration, login, logout, current user."""

from __future__ import annotations

import secrets
from datetime import timedelta

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from orchivio import telemetry
from orchivio.auth import (
    DUMMY_HASH,
    hash_password,
    hash_token,
    needs_rehash,
    new_token,
    password_problem,
    verify_password,
)
from orchivio.db.models import Membership, Organization, User, UserSession

from ..deps import Principal, api_error, audit, current_principal, get_db, utcnow
from ..schemas import LoginIn, LoginOut, MembershipOut, MeOut, RegisterIn, UserOut

# Short-lived MFA tokens stored in memory: {token: user_id}. Not distributed; use Redis for multi-node.
_mfa_pending: dict[str, str] = {}

router = APIRouter(tags=["auth"])


def _client_ip(request: Request) -> str:
    # The direct peer only. Behind a proxy, configure the server to rewrite the client
    # address instead of trusting a spoofable header here.
    return request.client.host if request.client else "unknown"


@router.post("/auth/bootstrap", status_code=201, response_model=LoginOut)
def bootstrap(body: RegisterIn, request: Request, response: Response, db: Session = Depends(get_db)) -> LoginOut:
    """Create the very first Studio user and open a session.

    Fails with 409 when any user already exists. Use when
    ``ORCHIVIO_ALLOW_REGISTRATION=false`` and the CLI (``orchivio user add``)
    is not available. This endpoint is unauthenticated; its guard is the empty
    users table.
    """
    if db.scalar(select(func.count()).select_from(User)):
        raise api_error(409, "bootstrap_done", "a user already exists; use /auth/register or the CLI")
    problem = password_problem(body.password)
    if problem:
        raise api_error(422, "weak_password", problem)
    if db.scalar(select(User.id).where(func.lower(User.email) == body.email)):
        raise api_error(409, "email_taken", "an account with this email already exists")
    user = User(email=body.email, display_name=body.display_name, password_hash=hash_password(body.password))
    db.add(user)
    db.flush()
    audit(db, request, "user.bootstrapped", actor_id=user.id, target_type="user", target_id=user.id)
    return _create_session(user, request, response, db, request.app.state.settings)


@router.post("/auth/register", status_code=201, response_model=UserOut)
def register(body: RegisterIn, request: Request, db: Session = Depends(get_db)) -> User:
    if not request.app.state.settings.allow_registration:
        raise api_error(403, "registration_disabled", "registration is disabled")
    problem = password_problem(body.password)
    if problem:
        raise api_error(422, "weak_password", problem)
    if db.scalar(select(User.id).where(func.lower(User.email) == body.email)):
        raise api_error(409, "email_taken", "an account with this email already exists")
    user = User(email=body.email, display_name=body.display_name, password_hash=hash_password(body.password))
    db.add(user)
    db.flush()
    audit(db, request, "user.registered", actor_id=user.id, target_type="user", target_id=user.id)
    db.commit()
    return user


@router.post("/auth/login", response_model=LoginOut)
def login(body: LoginIn, request: Request, response: Response, db: Session = Depends(get_db)) -> LoginOut:
    with telemetry.span("auth.login"):
        return _login(body, request, response, db)


def _login(body: LoginIn, request: Request, response: Response, db: Session) -> LoginOut:
    settings = request.app.state.settings
    throttle = request.app.state.throttle
    ip_key, email_key = f"ip:{_client_ip(request)}", f"email:{body.email}"
    if throttle.blocked(ip_key, email_key):
        raise api_error(
            429, "too_many_attempts", "too many failed attempts; try again later",
            headers={"Retry-After": str(settings.login_window_seconds)},
        )

    user = db.scalar(select(User).where(func.lower(User.email) == body.email))
    # Always run one scrypt verification so timing doesn't reveal whether the account exists.
    with telemetry.span("auth.password_verify"):
        ok = verify_password(body.password, user.password_hash if user else DUMMY_HASH)
    if user is None or not ok or user.status != "active":
        throttle.fail(ip_key, email_key)
        audit(db, request, "auth.login_failed", actor_id=user.id if user else None,
              target_type="user", target_id=user.id if user else None, meta={"ip": _client_ip(request)})
        db.commit()
        raise api_error(401, "invalid_credentials", "invalid email or password")

    throttle.succeed(email_key)
    if needs_rehash(user.password_hash or ""):
        user.password_hash = hash_password(body.password)
    db.commit()

    if user.mfa_enabled:
        mfa_token = secrets.token_urlsafe(32)
        _mfa_pending[mfa_token] = str(user.id)
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=202, content={"mfa_required": True, "mfa_token": mfa_token})

    return _create_session(user, request, response, db, settings)


@router.post("/auth/logout", status_code=204)
def logout(request: Request, response: Response, principal: Principal = Depends(current_principal),
           db: Session = Depends(get_db)) -> Response:
    if principal.session is not None:
        principal.session.revoked_at = utcnow()
        audit(db, request, "auth.logout", actor_id=principal.user_id, target_type="user", target_id=principal.user_id)
        db.commit()
    response.delete_cookie(request.app.state.settings.session_cookie_name, path="/")
    response.status_code = 204
    return response


def _create_session(user: User, request: Request, response: Response, db: Session, settings) -> LoginOut:
    token, token_hash = new_token()
    csrf, csrf_hash = new_token()
    expires = utcnow() + timedelta(hours=settings.session_ttl_hours)
    db.add(UserSession(
        user_id=user.id, token_hash=token_hash, csrf_hash=csrf_hash, expires_at=expires,
        user_agent=(request.headers.get("user-agent") or "")[:300] or None, ip=_client_ip(request),
    ))
    audit(db, request, "auth.login", actor_id=user.id, target_type="user", target_id=user.id)
    db.commit()
    response.set_cookie(
        settings.session_cookie_name, token, max_age=settings.session_ttl_hours * 3600,
        httponly=True, secure=settings.secure_cookies, samesite="lax", path="/",
    )
    return LoginOut(user=UserOut.model_validate(user), csrf_token=csrf, expires_at=expires)


@router.get("/me", response_model=MeOut)
def me(principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> MeOut:
    rows = db.execute(
        select(Organization, Membership.role)
        .join(Membership, Membership.organization_id == Organization.id)
        .where(Membership.user_id == principal.user_id)
        .order_by(Organization.name)
    ).all()
    orgs = [MembershipOut(organization_id=o.id, slug=o.slug, name=o.name, role=r) for o, r in rows]
    if principal.token is not None:
        orgs = [o for o in orgs if o.organization_id == principal.token.organization_id]
    return MeOut(user=UserOut.model_validate(principal.user), organizations=orgs, auth=principal.via)


# --- MFA (FR-067) -----------------------------------------------------------------------------


class MfaSetupOut(BaseModel):
    provisioning_uri: str
    secret: str


class MfaConfirmIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(min_length=6, max_length=8)


class MfaVerifyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mfa_token: str = Field(min_length=1, max_length=200)
    code: str = Field(min_length=6, max_length=8)


def _totp(secret: str):
    try:
        import pyotp
        return pyotp.TOTP(secret)
    except ImportError as e:
        raise api_error(503, "mfa_unavailable", "pyotp is not installed; install orchivio[mfa]") from e


@router.post("/auth/mfa/setup", response_model=MfaSetupOut)
def mfa_setup(request: Request, principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> MfaSetupOut:
    """Generate a new TOTP secret for the current user. Call /auth/mfa/confirm to activate it."""
    import pyotp
    secret = pyotp.random_base32()
    totp = pyotp.TOTP(secret)
    uri = totp.provisioning_uri(name=principal.user.email, issuer_name=request.app.state.settings.app_title if hasattr(request.app.state.settings, "app_title") else "Orchivio")
    principal.user.mfa_secret = secret
    principal.user.mfa_enabled = False
    db.commit()
    return MfaSetupOut(provisioning_uri=uri, secret=secret)


@router.post("/auth/mfa/confirm", status_code=204)
def mfa_confirm(body: MfaConfirmIn, principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> Response:
    """Verify first TOTP code and enable MFA."""
    user = principal.user
    if not user.mfa_secret:
        raise api_error(409, "mfa_not_setup", "call /auth/mfa/setup first")
    totp = _totp(user.mfa_secret)
    if not totp.verify(body.code, valid_window=1):
        raise api_error(401, "invalid_code", "invalid or expired TOTP code")
    user.mfa_enabled = True
    db.commit()
    return Response(status_code=204)


@router.post("/auth/mfa/disable", status_code=204)
def mfa_disable(body: MfaConfirmIn, principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> Response:
    """Disable MFA (requires current TOTP code)."""
    user = principal.user
    if not user.mfa_enabled or not user.mfa_secret:
        raise api_error(409, "mfa_not_enabled", "MFA is not enabled")
    totp = _totp(user.mfa_secret)
    if not totp.verify(body.code, valid_window=1):
        raise api_error(401, "invalid_code", "invalid or expired TOTP code")
    user.mfa_secret, user.mfa_enabled = None, False
    db.commit()
    return Response(status_code=204)


@router.post("/auth/mfa/verify")
def mfa_verify(body: MfaVerifyIn, request: Request, response: Response, db: Session = Depends(get_db)) -> LoginOut:
    """Complete login by verifying the TOTP code after a 202 MFA challenge."""
    user_id = _mfa_pending.pop(body.mfa_token, None)
    if user_id is None:
        raise api_error(401, "invalid_mfa_token", "MFA token is invalid or expired")
    user = db.get(User, user_id)
    if user is None or not user.mfa_enabled or not user.mfa_secret:
        raise api_error(401, "invalid_mfa_token", "MFA token is invalid or expired")
    totp = _totp(user.mfa_secret)
    if not totp.verify(body.code, valid_window=1):
        raise api_error(401, "invalid_code", "invalid or expired TOTP code")
    settings = request.app.state.settings
    return _create_session(user, request, response, db, settings)


@router.post("/auth/logout-all", status_code=204)
def logout_all(request: Request, response: Response, principal: Principal = Depends(current_principal),
               db: Session = Depends(get_db)) -> Response:
    """Revoke all active sessions for the current user."""
    from sqlalchemy import update as _update
    db.execute(_update(UserSession).where(UserSession.user_id == principal.user_id, UserSession.revoked_at.is_(None))
               .values(revoked_at=utcnow()))
    audit(db, request, "auth.logout_all", actor_id=principal.user_id, target_type="user", target_id=principal.user_id)
    db.commit()
    response.delete_cookie(request.app.state.settings.session_cookie_name, path="/")
    response.status_code = 204
    return response


# --- Password reset (FR-067) ------------------------------------------------------------------


class PasswordResetRequestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(min_length=1, max_length=320)


class PasswordResetConfirmIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(min_length=1, max_length=200)
    new_password: str = Field(min_length=1, max_length=200)


@router.post("/auth/password-reset", status_code=204)
def password_reset_request(body: PasswordResetRequestIn, request: Request, db: Session = Depends(get_db)) -> Response:
    """Issue a password-reset token for a platform user.

    Returns 204 regardless of whether the email exists (prevents enumeration).
    The token is included in the ``X-Reset-Token`` response header — the caller
    is responsible for delivering it to the user (e.g. via email).
    Token expires in 1 hour and can only be used once.
    """
    user = db.scalar(select(User).where(func.lower(User.email) == body.email.lower()))
    if user is None or user.status != "active" or user.password_hash is None:
        return Response(status_code=204)
    plain, tok_hash = new_token("prt_")
    user.reset_token_hash = tok_hash
    user.reset_token_expires_at = utcnow() + timedelta(hours=1)
    audit(db, request, "auth.password_reset_request", actor_id=None, target_type="user", target_id=str(user.id))
    db.commit()
    return Response(status_code=204, headers={"X-Reset-Token": plain})


@router.post("/auth/password-reset/confirm", status_code=204)
def password_reset_confirm(body: PasswordResetConfirmIn, request: Request, db: Session = Depends(get_db)) -> Response:
    """Consume a password-reset token and set a new password. Revokes all active sessions."""
    tok_hash = hash_token(body.token)
    now = utcnow()
    user = db.scalar(
        select(User).where(
            User.reset_token_hash == tok_hash,
            User.reset_token_expires_at > now,
        )
    )
    if user is None:
        raise api_error(400, "invalid_token", "token is invalid or expired")
    problem = password_problem(body.new_password)
    if problem:
        raise api_error(422, "weak_password", problem)
    user.password_hash = hash_password(body.new_password)
    user.reset_token_hash = None
    user.reset_token_expires_at = None
    from sqlalchemy import update as _update
    db.execute(_update(UserSession).where(UserSession.user_id == user.id, UserSession.revoked_at.is_(None))
               .values(revoked_at=now))
    audit(db, request, "auth.password_reset_confirm", actor_id=None, target_type="user", target_id=str(user.id))
    db.commit()
    return Response(status_code=204)
