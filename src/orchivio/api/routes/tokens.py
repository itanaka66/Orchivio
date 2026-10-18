"""Personal API tokens (scoped to one organization, optionally one project)."""

from __future__ import annotations

import uuid
from datetime import timedelta

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from orchivio.auth import API_TOKEN_PREFIX, new_token
from orchivio.db.models import ApiToken, Project
from orchivio.policy import KNOWN_ACTIONS

from ..deps import (
    Principal,
    api_error,
    audit,
    authorize,
    current_principal,
    get_db,
    not_found,
    utcnow,
)
from ..schemas import Page, TokenCreated, TokenIn, TokenOut

router = APIRouter(tags=["tokens"])


def _require_session(principal: Principal) -> None:
    # A token must not be able to mint further tokens (credential propagation).
    if principal.via != "session":
        raise api_error(403, "session_required", "log in with a session to manage API tokens")


@router.post("/tokens", status_code=201, response_model=TokenCreated)
def create_token(body: TokenIn, request: Request, principal: Principal = Depends(current_principal),
                 db: Session = Depends(get_db)) -> TokenCreated:
    _require_session(principal)
    authorize(db, principal, body.organization_id, "project.read")
    unknown = [s for s in body.scopes if s != "*" and s not in KNOWN_ACTIONS]
    if unknown or not body.scopes:
        raise api_error(422, "invalid_scopes", "scopes must be '*' or known actions",
                        {"known": sorted(KNOWN_ACTIONS), "unknown": unknown})
    if body.project_id is not None:
        project = db.get(Project, body.project_id)
        if project is None or project.organization_id != body.organization_id:
            raise not_found("project")
    plain, digest = new_token(API_TOKEN_PREFIX)
    tok = ApiToken(owner_id=principal.user_id, organization_id=body.organization_id, project_id=body.project_id,
                   name=body.name, token_hash=digest, scopes=sorted(set(body.scopes)),
                   expires_at=utcnow() + timedelta(days=body.expires_in_days))
    db.add(tok)
    db.flush()
    audit(db, request, "token.created", actor_id=principal.user_id, org_id=body.organization_id,
          target_type="api_token", target_id=tok.id, meta={"name": tok.name, "scopes": tok.scopes})
    db.commit()
    return TokenCreated(**TokenOut.model_validate(tok).model_dump(), token=plain)


@router.get("/tokens", response_model=Page[TokenOut])
def list_tokens(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> Page[TokenOut]:
    _require_session(principal)
    q = select(ApiToken).where(ApiToken.owner_id == principal.user_id)
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    rows = db.scalars(q.order_by(ApiToken.created_at.desc()).limit(limit).offset(offset)).all()
    return Page(items=[TokenOut.model_validate(t) for t in rows], limit=limit, offset=offset, total=total)


@router.delete("/tokens/{token_id}", status_code=204)
def revoke_token(token_id: uuid.UUID, request: Request, principal: Principal = Depends(current_principal),
                 db: Session = Depends(get_db)) -> Response:
    _require_session(principal)
    tok = db.get(ApiToken, token_id)
    if tok is None or tok.owner_id != principal.user_id:
        raise not_found("token")
    if tok.revoked_at is None:
        tok.revoked_at = utcnow()
        audit(db, request, "token.revoked", actor_id=principal.user_id, org_id=tok.organization_id,
              target_type="api_token", target_id=tok.id)
        db.commit()
    return Response(status_code=204)
