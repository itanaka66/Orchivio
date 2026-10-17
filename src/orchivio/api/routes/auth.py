"""Registration, login, logout, current user."""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from orchivio.auth import (
    DUMMY_HASH,
    hash_password,
    needs_rehash,
    new_token,
    password_problem,
    verify_password,
)
from orchivio.db.models import Membership, Organization, User, UserSession

from ..deps import Principal, api_error, audit, current_principal, get_db, utcnow
from ..schemas import LoginIn, LoginOut, MembershipOut, MeOut, RegisterIn, UserOut

router = APIRouter(tags=["auth"])


def _client_ip(request: Request) -> str:
    # The direct peer only. Behind a proxy, configure the server to rewrite the client
    # address instead of trusting a spoofable header here.
    return request.client.host if request.client else "unknown"


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
