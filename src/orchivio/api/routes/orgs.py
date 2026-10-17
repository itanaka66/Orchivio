"""Organizations, members, audit log."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from orchivio.db.models import AuditEvent, Membership, Organization, User

from ..deps import Principal, api_error, audit, authorize, current_principal, get_db, not_found
from ..schemas import AuditOut, MemberIn, MemberOut, MemberPatch, OrgIn, OrgOut, Page

router = APIRouter(tags=["organizations"])


def _org_or_404(db: Session, org_id: uuid.UUID) -> Organization:
    org = db.get(Organization, org_id)
    if org is None:
        raise not_found("organization")
    return org


@router.post("/organizations", status_code=201, response_model=OrgOut)
def create_organization(body: OrgIn, request: Request, principal: Principal = Depends(current_principal),
                        db: Session = Depends(get_db)) -> OrgOut:
    if principal.token is not None:
        raise api_error(403, "forbidden", "API tokens cannot create organizations")
    if db.scalar(select(Organization.id).where(Organization.slug == body.slug)):
        raise api_error(409, "slug_taken", "organization slug already in use")
    org = Organization(name=body.name, slug=body.slug)
    db.add(org)
    db.flush()
    db.add(Membership(organization_id=org.id, user_id=principal.user_id, role="owner"))
    audit(db, request, "organization.created", actor_id=principal.user_id, org_id=org.id,
          target_type="organization", target_id=org.id, meta={"slug": org.slug})
    try:
        db.commit()
    except IntegrityError as e:  # lost a race with another request using the same slug
        db.rollback()
        raise api_error(409, "slug_taken", "organization slug already in use") from e
    return OrgOut(id=org.id, name=org.name, slug=org.slug, role="owner")


@router.get("/organizations", response_model=Page[OrgOut])
def list_organizations(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                       principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> Page[OrgOut]:
    q = (select(Organization, Membership.role).join(Membership, Membership.organization_id == Organization.id)
         .where(Membership.user_id == principal.user_id))
    if principal.token is not None:
        q = q.where(Organization.id == principal.token.organization_id)
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    rows = db.execute(q.order_by(Organization.name).limit(limit).offset(offset)).all()
    return Page(items=[OrgOut(id=o.id, name=o.name, slug=o.slug, role=r) for o, r in rows],
                limit=limit, offset=offset, total=total)


@router.get("/organizations/{org_id}", response_model=OrgOut)
def get_organization(org_id: uuid.UUID, principal: Principal = Depends(current_principal),
                     db: Session = Depends(get_db)) -> OrgOut:
    org = _org_or_404(db, org_id)
    role = authorize(db, principal, org.id, "project.read")
    return OrgOut(id=org.id, name=org.name, slug=org.slug, role=role)


# --- members -----------------------------------------------------------------


def _owner_count(db: Session, org_id: uuid.UUID) -> int:
    return db.scalar(select(func.count()).select_from(Membership)
                     .where(Membership.organization_id == org_id, Membership.role == "owner")) or 0


def _require_owner_to_touch_owners(caller_role: str, *roles: str) -> None:
    if "owner" in roles and caller_role != "owner":
        raise api_error(403, "forbidden", "only an owner can grant, change or remove the owner role")


@router.get("/organizations/{org_id}/members", response_model=Page[MemberOut])
def list_members(org_id: uuid.UUID, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                 principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> Page[MemberOut]:
    org = _org_or_404(db, org_id)
    authorize(db, principal, org.id, "project.read")
    q = (select(User, Membership.role).join(Membership, Membership.user_id == User.id)
         .where(Membership.organization_id == org.id))
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    rows = db.execute(q.order_by(User.email).limit(limit).offset(offset)).all()
    return Page(items=[MemberOut(user_id=u.id, email=u.email, display_name=u.display_name, role=r) for u, r in rows],
                limit=limit, offset=offset, total=total)


@router.post("/organizations/{org_id}/members", status_code=201, response_model=MemberOut)
def add_member(org_id: uuid.UUID, body: MemberIn, request: Request,
               principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> MemberOut:
    org = _org_or_404(db, org_id)
    caller = authorize(db, principal, org.id, "member.manage")
    _require_owner_to_touch_owners(caller, body.role)
    user = db.scalar(select(User).where(func.lower(User.email) == body.email))
    if user is None or user.status != "active":
        raise api_error(404, "user_not_found", "no active user with that email")
    if db.get(Membership, (org.id, user.id)) is not None:
        raise api_error(409, "already_member", "user is already a member")
    db.add(Membership(organization_id=org.id, user_id=user.id, role=body.role))
    audit(db, request, "member.added", actor_id=principal.user_id, org_id=org.id,
          target_type="user", target_id=user.id, meta={"role": body.role})
    db.commit()
    return MemberOut(user_id=user.id, email=user.email, display_name=user.display_name, role=body.role)


@router.patch("/organizations/{org_id}/members/{user_id}", response_model=MemberOut)
def change_member_role(org_id: uuid.UUID, user_id: uuid.UUID, body: MemberPatch, request: Request,
                       principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> MemberOut:
    org = _org_or_404(db, org_id)
    caller = authorize(db, principal, org.id, "member.manage")
    m = db.get(Membership, (org.id, user_id))
    if m is None:
        raise not_found("member")
    _require_owner_to_touch_owners(caller, body.role, m.role)
    if m.role == "owner" and body.role != "owner" and _owner_count(db, org.id) <= 1:
        raise api_error(409, "last_owner", "an organization must keep at least one owner")
    old, m.role = m.role, body.role
    audit(db, request, "member.role_changed", actor_id=principal.user_id, org_id=org.id,
          target_type="user", target_id=user_id, meta={"from": old, "to": body.role})
    db.commit()
    u = db.get(User, user_id)
    return MemberOut(user_id=u.id, email=u.email, display_name=u.display_name, role=m.role)


@router.delete("/organizations/{org_id}/members/{user_id}", status_code=204)
def remove_member(org_id: uuid.UUID, user_id: uuid.UUID, request: Request,
                  principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> Response:
    org = _org_or_404(db, org_id)
    me = user_id == principal.user_id
    # Leaving an organization needs no admin rights; removing someone else does.
    caller = authorize(db, principal, org.id, "project.read" if me else "member.manage")
    m = db.get(Membership, (org.id, user_id))
    if m is None:
        raise not_found("member")
    if not me:
        _require_owner_to_touch_owners(caller, m.role)
    if m.role == "owner" and _owner_count(db, org.id) <= 1:
        raise api_error(409, "last_owner", "an organization must keep at least one owner")
    db.delete(m)
    audit(db, request, "member.removed", actor_id=principal.user_id, org_id=org.id,
          target_type="user", target_id=user_id, meta={"role": m.role, "self": me})
    db.commit()
    return Response(status_code=204)


# --- audit log ---------------------------------------------------------------


@router.get("/organizations/{org_id}/audit-events", response_model=Page[AuditOut])
def list_audit_events(org_id: uuid.UUID, action_prefix: str | None = Query(None, max_length=100),
                      limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                      principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> Page[AuditOut]:
    org = _org_or_404(db, org_id)
    authorize(db, principal, org.id, "audit.read")
    q = select(AuditEvent).where(AuditEvent.organization_id == org.id)
    if action_prefix:
        escaped = action_prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        q = q.where(AuditEvent.action.like(escaped + "%", escape="\\"))
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    rows = db.scalars(q.order_by(AuditEvent.id.desc()).limit(limit).offset(offset)).all()
    return Page(items=[AuditOut(id=e.id, created_at=e.created_at, actor_id=e.actor_id, action=e.action,
                                plane=e.plane, channel=e.channel, target_type=e.target_type,
                                target_id=e.target_id, metadata=e.meta) for e in rows],
                limit=limit, offset=offset, total=total)
