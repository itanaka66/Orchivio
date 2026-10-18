"""OpenID Connect sign-in for the end users of a generated application (FR-067, P1).

Authorization code flow with PKCE. The browser is bound to its sign-in attempt by a cookie (login CSRF / fixation
defence); ``state`` and the PKCE verifier live server-side, are single use and expire after ten minutes; the ID token is
verified completely (``orchivio.integrations.oidc``). The identity is the pair (issuer, subject), never the e-mail
address, so a provider that lets users choose or change their e-mail cannot take over another account. Roles of
federated users are controlled by the identity provider through the configured claim mapping and are re-applied at
every sign-in; local (password) accounts are never touched by this flow.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import uuid
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from orchivio.auth import hash_token
from orchivio.db import to_model
from orchivio.db.models import AppOidcProvider, AppRevision, AppUser, AppUserIdentity, AppUserRole, OidcLoginState
from orchivio.integrations import DestinationError, SecretUnavailable, check_url, destination_policy, resolve_secret
from orchivio.integrations.dispatcher import SECRET_REF
from orchivio.integrations.oidc import OidcError, authorization_url, pkce_pair

from ..deps import Principal, api_error, audit, current_principal, get_db, load_application, not_found, utcnow
from .appauth import _app, _audit, cookie_path, issue_session

router = APIRouter(tags=["oidc"])

STATE_TTL = timedelta(minutes=10)
USERNAME_OK = re.compile(r"[^a-z0-9._@+-]+")
CLAIM = r"^[A-Za-z][A-Za-z0-9_:.\-]{0,63}$"


class OidcIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    issuer: str = Field(max_length=500)
    client_id: str = Field(min_length=1, max_length=300)
    client_secret_ref: str | None = Field(default=None, max_length=300, description="env:NAME or file:/path; omit for a public client")
    scopes: str = Field(default="openid email profile", max_length=300)
    username_claim: str = Field(default="email", pattern=CLAIM)
    role_claim: str = Field(default="groups", pattern=CLAIM)
    role_mapping: dict[str, str] = Field(default_factory=dict, max_length=100, description="provider claim value -> application role")
    default_roles: list[str] = Field(default_factory=list, max_length=20)
    auto_provision: bool = False
    allowed_email_domains: list[str] | None = Field(default=None, max_length=50)
    post_login_url: str = Field(default="/", max_length=500)
    status: str = Field(default="active", pattern="^(active|disabled)$")


def provider_out(p: AppOidcProvider) -> dict[str, Any]:
    return {"application_id": str(p.application_id), "issuer": p.issuer, "client_id": p.client_id, "client_secret_ref": p.client_secret_ref,
            "scopes": p.scopes, "username_claim": p.username_claim, "role_claim": p.role_claim, "role_mapping": p.role_mapping,
            "default_roles": p.default_roles, "auto_provision": p.auto_provision, "allowed_email_domains": p.allowed_email_domains,
            "post_login_url": p.post_login_url, "status": p.status,
            "login_path": f"/api/v1/apps/{p.application_id}/auth/oidc/login"}


def safe_next(path: str | None) -> str | None:
    """Only same-site absolute paths: no scheme, no host, no protocol-relative or backslash tricks."""
    if path and path.startswith("/") and not path.startswith("//") and "\\" not in path and len(path) <= 300 and all(ord(c) >= 32 for c in path):
        return path
    return None


def valid_post_login(url: str) -> bool:
    return safe_next(url) is not None or (url.startswith("https://") and len(url) <= 500 and "@" not in url.split("/", 3)[2])


def redirect_uri(request: Request, app_id: uuid.UUID) -> str:
    settings = request.app.state.settings
    base = settings.public_url
    if not base:
        if settings.env == "prod":
            raise api_error(503, "oidc_not_available", "set ORCHIVIO_PUBLIC_URL to use OIDC sign-in")
        base = str(request.base_url)
    return f"{base.rstrip('/')}/api/v1/apps/{app_id}/auth/oidc/callback"


def oidc_failure(e: OidcError):
    status = {"provider_unreachable": 502, "provider_error": 502, "issuer_mismatch": 502, "unavailable": 503}.get(e.code, 401)
    return api_error(status, e.code, e.message)


# --- administration (Studio) --------------------------------------------------------------------


@router.put("/applications/{app_id}/oidc")
def put_provider(app_id: uuid.UUID, body: OidcIn, request: Request, principal: Principal = Depends(current_principal),
                 db: Session = Depends(get_db)) -> dict[str, Any]:
    """Configure (or replace) the identity provider of an application. Admin only."""
    app, project = load_application(db, principal, app_id, "appuser.manage")
    try:
        check_url(body.issuer, destination_policy(request.app.state.settings))
    except DestinationError as e:
        raise api_error(422, "destination_refused", str(e)) from e
    if body.issuer.endswith("/") or "?" in body.issuer or "#" in body.issuer:
        raise api_error(422, "invalid_issuer", "give the issuer exactly as the provider reports it, without a trailing slash, query or fragment")
    if body.client_secret_ref is not None:
        if not SECRET_REF.match(body.client_secret_ref):
            raise api_error(422, "invalid_secret_ref", "use env:NAME or file:/path")
        try:
            resolve_secret(body.client_secret_ref)
        except SecretUnavailable as e:
            raise api_error(422, "secret_unavailable", str(e)) from e
    if "openid" not in body.scopes.split():
        raise api_error(422, "invalid_scopes", "the scopes must include openid")
    if not valid_post_login(body.post_login_url):
        raise api_error(422, "invalid_redirect", "post_login_url must be a path on this site (/...) or an https URL")
    roles: set[str] = set()
    if app.current_revision_id is not None:
        roles = set(to_model(db.get(AppRevision, app.current_revision_id)).security.roles)
    named = set(body.default_roles) | set(body.role_mapping.values())
    if app.current_revision_id is None or named - roles:
        raise api_error(422, "unknown_role", "the model does not define these roles", {"unknown": sorted(named - roles), "defined": sorted(roles)})
    domains = [d.lower().strip().lstrip("@") for d in body.allowed_email_domains] if body.allowed_email_domains is not None else None
    if domains is not None and (not domains or any(not re.fullmatch(r"[a-z0-9.-]{1,253}", d) for d in domains)):
        raise api_error(422, "invalid_domains", "give plain domain names such as example.com")
    row = db.scalar(select(AppOidcProvider).where(AppOidcProvider.application_id == app.id))
    if row is None:
        row = AppOidcProvider(application_id=app.id, issuer=body.issuer, client_id=body.client_id, created_by=principal.user_id)
        db.add(row)
    row.issuer, row.client_id, row.client_secret_ref, row.scopes = body.issuer, body.client_id, body.client_secret_ref, body.scopes
    row.username_claim, row.role_claim, row.role_mapping = body.username_claim, body.role_claim, body.role_mapping
    row.default_roles, row.auto_provision, row.allowed_email_domains = sorted(set(body.default_roles)), body.auto_provision, domains
    row.post_login_url, row.status = body.post_login_url, body.status
    db.flush()
    audit(db, request, "oidc.configured", actor_id=principal.user_id, org_id=project.organization_id, target_type="application", target_id=app.id,
          meta={"issuer_host": body.issuer.split("/")[2], "auto_provision": body.auto_provision, "status": body.status})
    db.commit()
    return provider_out(row)


@router.get("/applications/{app_id}/oidc")
def get_provider(app_id: uuid.UUID, principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict[str, Any]:
    app, _ = load_application(db, principal, app_id, "appuser.manage")
    row = db.scalar(select(AppOidcProvider).where(AppOidcProvider.application_id == app.id))
    if row is None:
        raise not_found("oidc provider")
    return provider_out(row)


@router.delete("/applications/{app_id}/oidc", status_code=204)
def delete_provider(app_id: uuid.UUID, request: Request, principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> Response:
    app, project = load_application(db, principal, app_id, "appuser.manage")
    row = db.scalar(select(AppOidcProvider).where(AppOidcProvider.application_id == app.id))
    if row is None:
        raise not_found("oidc provider")
    audit(db, request, "oidc.removed", actor_id=principal.user_id, org_id=project.organization_id, target_type="application", target_id=app.id, meta={})
    db.delete(row)
    db.commit()
    return Response(status_code=204)


@router.post("/applications/{app_id}/oidc/test")
def test_provider(app_id: uuid.UUID, request: Request, principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Fetch the provider's discovery document and keys now, to catch typos before users do."""
    app, _ = load_application(db, principal, app_id, "appuser.manage")
    row = db.scalar(select(AppOidcProvider).where(AppOidcProvider.application_id == app.id))
    if row is None:
        raise not_found("oidc provider")
    client = request.app.state.oidc
    try:
        meta = client.discover(row.issuer)
        client._jwks(meta.jwks_uri, force=True)
    except OidcError as e:
        raise oidc_failure(e) from e
    return {"ok": True, "authorization_endpoint": meta.authorization_endpoint, "token_endpoint": meta.token_endpoint, "jwks_uri": meta.jwks_uri,
            "redirect_uri": redirect_uri(request, app.id)}


# --- the sign-in flow (end users) ---------------------------------------------------------------------


def _browser_cookie(request: Request) -> str:
    return request.app.state.settings.session_cookie_name + "_oidc"


def _provider(db: Session, app_id: uuid.UUID) -> AppOidcProvider:
    p = db.scalar(select(AppOidcProvider).where(AppOidcProvider.application_id == app_id, AppOidcProvider.status == "active"))
    if p is None:
        raise not_found("oidc provider")
    return p


@router.get("/apps/{app_id}/auth/oidc/login")
def oidc_login(app_id: uuid.UUID, request: Request, next: str | None = Query(None, max_length=300), db: Session = Depends(get_db)) -> RedirectResponse:
    app, project, model, _ = _app(db, app_id)
    provider = _provider(db, app.id)
    try:
        meta = request.app.state.oidc.discover(provider.issuer)
    except OidcError as e:
        raise oidc_failure(e) from e
    state, browser, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(32), secrets.token_urlsafe(24)
    verifier, challenge = pkce_pair()
    now = utcnow()
    db.execute(delete(OidcLoginState).where(OidcLoginState.expires_at < now - timedelta(hours=1)))
    db.add(OidcLoginState(application_id=app.id, state_hash=hash_token(state), browser_hash=hash_token(browser), nonce=nonce, code_verifier=verifier,
                          next_path=safe_next(next), created_at=now, expires_at=now + STATE_TTL))
    db.commit()
    url = authorization_url(meta, client_id=provider.client_id, redirect_uri=redirect_uri(request, app.id), scopes=provider.scopes, state=state,
                            nonce=nonce, challenge=challenge)
    resp = RedirectResponse(url, status_code=302)
    resp.set_cookie(_browser_cookie(request), browser, max_age=int(STATE_TTL.total_seconds()), httponly=True, samesite="lax",
                    secure=request.app.state.settings.secure_cookies, path=f"/api/v1/apps/{app.id}/auth/oidc")
    return resp


def _claim_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    return [v for v in value[:100] if isinstance(v, str)] if isinstance(value, list) else []


def _username(db: Session, app_id: uuid.UUID, claims: dict[str, Any], provider: AppOidcProvider) -> str:
    raw = claims.get(provider.username_claim)
    suffix = hashlib.sha256(f"{claims['iss']}|{claims['sub']}".encode()).hexdigest()[:8]
    name = USERNAME_OK.sub("-", raw.lower()).strip("-._@+") if isinstance(raw, str) else ""
    name = name[:80] if len(name) >= 2 and name[0].isalnum() else f"user-{suffix}"
    if db.scalar(select(AppUser.id).where(AppUser.application_id == app_id, AppUser.username == name)) is not None:
        name = f"{name[:80]}-{suffix}"  # a local account already uses it: never merge, always a new account
    return name


def _sync_roles(db: Session, user: AppUser, provider: AppOidcProvider, claims: dict[str, Any], defined: set[str]) -> set[str]:
    mapped = {provider.role_mapping[v] for v in _claim_list(claims.get(provider.role_claim)) if v in provider.role_mapping}
    roles = (set(provider.default_roles) | mapped) & defined
    db.execute(delete(AppUserRole).where(AppUserRole.app_user_id == user.id))
    db.add_all(AppUserRole(app_user_id=user.id, role_key=r) for r in sorted(roles))
    return roles


@router.get("/apps/{app_id}/auth/oidc/callback")
def oidc_callback(app_id: uuid.UUID, request: Request, code: str | None = Query(None, max_length=2000), state: str | None = Query(None, max_length=200),
                  error: str | None = Query(None, max_length=100), db: Session = Depends(get_db)) -> RedirectResponse:
    app, project, model, _ = _app(db, app_id)
    provider = _provider(db, app.id)

    def fail(status: int, code_: str, message: str, reason: str):
        _audit(db, request, "appauth.oidc_failed", app, project, None, reason=reason)
        db.commit()
        return api_error(status, code_, message)

    if error:
        raise fail(400, "provider_denied", "the identity provider did not sign you in", "provider_error")
    browser = request.cookies.get(_browser_cookie(request))
    if not code or not state or not browser:
        raise fail(400, "invalid_state", "this sign-in attempt is not valid; start again", "missing_parameters")
    row = db.scalar(select(OidcLoginState).where(OidcLoginState.state_hash == hash_token(state), OidcLoginState.application_id == app.id))
    now = utcnow()
    expires = row.expires_at if row is not None and row.expires_at.tzinfo else (row.expires_at.replace(tzinfo=now.tzinfo) if row is not None else None)
    if row is None or row.used_at is not None or expires <= now or not secrets.compare_digest(row.browser_hash, hash_token(browser)):
        raise fail(400, "invalid_state", "this sign-in attempt is not valid; start again", "bad_state")
    claimed = db.execute(update(OidcLoginState).where(OidcLoginState.id == row.id, OidcLoginState.used_at.is_(None)).values(used_at=now)).rowcount
    db.commit()
    if not claimed:
        raise fail(400, "invalid_state", "this sign-in attempt is not valid; start again", "replayed_state")

    client = request.app.state.oidc
    secret = None
    if provider.client_secret_ref:
        try:
            secret = resolve_secret(provider.client_secret_ref)
        except SecretUnavailable:
            raise fail(503, "oidc_not_available", "sign-in is not available right now", "secret_unavailable") from None
    try:
        meta = client.discover(provider.issuer)
        token = client.exchange(meta, code=code, redirect_uri=redirect_uri(request, app.id), client_id=provider.client_id, client_secret=secret,
                                verifier=row.code_verifier)
        claims = client.verify_id_token(token, meta, client_id=provider.client_id, nonce=row.nonce)
    except OidcError as e:
        raise fail(oidc_failure(e).status_code, e.code, e.message, e.code) from e

    email = claims.get("email") if isinstance(claims.get("email"), str) and claims.get("email_verified") is True else None
    if provider.allowed_email_domains is not None and (email is None or email.rsplit("@", 1)[-1].lower() not in provider.allowed_email_domains):
        raise fail(403, "domain_not_allowed", "your account is not allowed to sign in to this application", "domain")
    identity = db.scalar(select(AppUserIdentity).where(AppUserIdentity.application_id == app.id, AppUserIdentity.issuer == claims["iss"],
                                                       AppUserIdentity.subject == claims["sub"]))
    if identity is not None:
        user = db.get(AppUser, identity.app_user_id)
    elif provider.auto_provision:
        user = AppUser(application_id=app.id, username=_username(db, app.id, claims, provider),
                       display_name=claims.get("name")[:200] if isinstance(claims.get("name"), str) else None, password_hash=None,
                       status="active", must_change_password=False)
        db.add(user)
        db.flush()
        identity = AppUserIdentity(application_id=app.id, app_user_id=user.id, issuer=claims["iss"], subject=claims["sub"], email=email)
        db.add(identity)
    else:
        raise fail(403, "no_account", "there is no account for you in this application", "no_account")
    if user is None or user.status != "active":
        raise fail(403, "account_disabled", "this account is disabled", "disabled")
    identity.last_login_at = now
    if email:
        identity.email = email
    roles = _sync_roles(db, user, provider, claims, set(model.security.roles))

    resp = RedirectResponse(row.next_path or provider.post_login_url, status_code=302)
    issue_session(db, request, resp, app, user)
    _audit(db, request, "appauth.oidc_login", app, project, user.id, roles=sorted(roles))
    db.commit()
    resp.delete_cookie(_browser_cookie(request), path=f"/api/v1/apps/{app.id}/auth/oidc")
    return resp
