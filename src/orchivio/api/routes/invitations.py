"""Organization member invitations: invite by email, accept via token."""

from __future__ import annotations

import uuid
from datetime import timedelta

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, EmailStr, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from orchivio.auth import hash_password, hash_token, new_token, password_problem
from orchivio.db.models import Invitation, Membership, Organization, User

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

router = APIRouter(tags=["invitations"])

_INVITE_TTL_DAYS = 7
_VALID_ROLES = {"owner", "admin", "editor", "viewer"}


class InviteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: EmailStr
    role: str = Field(default="editor", pattern="^(owner|admin|editor|viewer)$")


class InviteOut(BaseModel):
    id: uuid.UUID
    organization_id: uuid.UUID
    email: str
    role: str
    status: str
    expires_at: str
    created_at: str


class AcceptIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(min_length=1, max_length=200)
    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    password: str | None = Field(default=None, min_length=1, max_length=200)


def _inv_out(inv: Invitation) -> InviteOut:
    return InviteOut(
        id=inv.id, organization_id=inv.organization_id, email=inv.email, role=inv.role,
        status=inv.status, expires_at=inv.expires_at.isoformat(), created_at=inv.created_at.isoformat(),
    )


@router.post("/organizations/{org_id}/invitations", status_code=201, response_model=InviteOut)
def create_invitation(org_id: uuid.UUID, body: InviteIn, request: Request,
                      principal: Principal = Depends(current_principal),
                      db: Session = Depends(get_db)) -> InviteOut:
    """Invite someone to join the organization. Admin/owner only. Returns the invitation token in X-Invite-Token."""
    org = db.get(Organization, org_id)
    if org is None:
        raise not_found("organization")
    authorize(db, principal, org_id, "member.manage")

    # Check the invitee isn't already a member.
    existing_user = db.scalar(select(User).where(func.lower(User.email) == body.email.lower()))
    if existing_user:
        already = db.scalar(
            select(Membership.user_id).where(
                Membership.organization_id == org_id, Membership.user_id == existing_user.id
            )
        )
        if already:
            raise api_error(409, "already_member", "this user is already a member of the organization")

    # One pending invite per email per org is enough.
    pending = db.scalar(
        select(Invitation).where(
            Invitation.organization_id == org_id,
            func.lower(Invitation.email) == body.email.lower(),
            Invitation.status == "pending",
        )
    )
    if pending:
        raise api_error(409, "invite_exists", "a pending invitation already exists for this email")

    plain, tok_hash = new_token("inv_")
    now = utcnow()
    inv = Invitation(
        organization_id=org_id, email=body.email.lower(), role=body.role,
        token_hash=tok_hash, created_by=principal.user_id,
        expires_at=now + timedelta(days=_INVITE_TTL_DAYS),
    )
    db.add(inv)
    audit(db, request, "invitation.created", actor_id=principal.user_id, org_id=org_id,
          target_type="invitation", target_id=str(inv.id), meta={"email": body.email, "role": body.role})
    db.commit()
    response = Response(status_code=201)
    response.headers["X-Invite-Token"] = plain
    return _inv_out(inv)


@router.get("/organizations/{org_id}/invitations", response_model=list[InviteOut])
def list_invitations(org_id: uuid.UUID, principal: Principal = Depends(current_principal),
                     db: Session = Depends(get_db)) -> list[InviteOut]:
    """List pending invitations for an organization. Admin/owner only."""
    org = db.get(Organization, org_id)
    if org is None:
        raise not_found("organization")
    authorize(db, principal, org_id, "member.manage")
    rows = db.scalars(
        select(Invitation)
        .where(Invitation.organization_id == org_id, Invitation.status == "pending")
        .order_by(Invitation.created_at.desc())
    ).all()
    return [_inv_out(inv) for inv in rows]


@router.delete("/invitations/{invitation_id}", status_code=204)
def cancel_invitation(invitation_id: uuid.UUID, request: Request,
                      principal: Principal = Depends(current_principal),
                      db: Session = Depends(get_db)) -> Response:
    """Cancel a pending invitation. Admin/owner of the target org only."""
    inv = db.get(Invitation, invitation_id)
    if inv is None or inv.status != "pending":
        raise not_found("invitation")
    authorize(db, principal, inv.organization_id, "member.manage")
    inv.status = "cancelled"
    audit(db, request, "invitation.cancelled", actor_id=principal.user_id, org_id=inv.organization_id,
          target_type="invitation", target_id=str(inv.id))
    db.commit()
    return Response(status_code=204)


@router.post("/invitations/accept", status_code=200)
def accept_invitation(body: AcceptIn, request: Request, db: Session = Depends(get_db)) -> dict:
    """Accept an invitation token.

    If no account exists for the invited email, ``display_name`` and ``password`` are required
    to create one. If the account already exists, only ``token`` is needed (the existing account
    gains the invited role).
    """
    tok_hash = hash_token(body.token)
    now = utcnow()
    inv = db.scalar(
        select(Invitation).where(
            Invitation.token_hash == tok_hash,
            Invitation.status == "pending",
            Invitation.expires_at > now,
        )
    )
    if inv is None:
        raise api_error(400, "invalid_token", "invitation token is invalid or expired")

    org = db.get(Organization, inv.organization_id)
    if org is None:
        raise api_error(410, "org_gone", "the organization no longer exists")

    user = db.scalar(select(User).where(func.lower(User.email) == inv.email))
    if user is None:
        if not body.display_name or not body.password:
            raise api_error(422, "registration_required",
                            "provide display_name and password to create a new account")
        problem = password_problem(body.password)
        if problem:
            raise api_error(422, "weak_password", problem)
        user = User(email=inv.email, display_name=body.display_name,
                    password_hash=hash_password(body.password))
        db.add(user)
        db.flush()
        audit(db, request, "user.registered", actor_id=None, target_type="user", target_id=str(user.id),
              meta={"via": "invitation"})

    # Upsert membership (the invite might upgrade an existing lower role).
    existing = db.scalar(
        select(Membership).where(Membership.organization_id == inv.organization_id, Membership.user_id == user.id)
    )
    if existing:
        existing.role = inv.role
    else:
        db.add(Membership(organization_id=inv.organization_id, user_id=user.id, role=inv.role))

    inv.status = "accepted"
    inv.accepted_by = user.id
    inv.accepted_at = now
    audit(db, request, "invitation.accepted", actor_id=user.id, target_type="invitation",
          target_id=str(inv.id), org_id=inv.organization_id, meta={"user_id": str(user.id)})
    db.commit()
    return {"user_id": str(user.id), "organization_id": str(org.id), "role": inv.role}
