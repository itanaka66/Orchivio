"""Create Studio users from the command line (``orchivio user add``), for bootstrapping a server or adding people when open registration is off.

It writes to the platform database directly, so it works whether or not ``ORCHIVIO_ALLOW_REGISTRATION`` is on, applies the same password rules as the
API, and records an audit event. The password is never accepted as a command-line argument (it would show up in process lists and shell history).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from orchivio.auth import hash_password, password_problem
from orchivio.db.models import MEMBER_ROLES, AuditEvent, Membership, Organization, User

_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}$")


class UserError(Exception):
    """Something the operator can fix (bad input, duplicate, unknown organization)."""


@dataclass(frozen=True)
class UserResult:
    user_id: str
    email: str
    organization: str | None
    role: str | None


def add_user(session: Session, *, email: str, display_name: str, password: str, org_slug: str | None = None, role: str | None = None) -> UserResult:
    """Add one user (and optionally an organization membership). Commits on success; raises ``UserError`` and changes nothing otherwise."""
    email = email.strip().lower()
    display_name = display_name.strip()
    if not _EMAIL.match(email) or len(email) > 320:
        raise UserError("that does not look like an e-mail address")
    if not display_name or len(display_name) > 200:
        raise UserError("the name must be 1-200 characters")
    problem = password_problem(password)
    if problem:
        raise UserError(problem)
    if (org_slug is None) != (role is None):
        raise UserError("give both --org and --role, or neither")
    if role is not None and role not in MEMBER_ROLES:
        raise UserError(f"role must be one of {', '.join(MEMBER_ROLES)}")
    org = None
    if org_slug is not None:
        org = session.scalar(select(Organization).where(Organization.slug == org_slug))
        if org is None:
            raise UserError(f"there is no organization with the slug '{org_slug}'")
    if session.scalar(select(User.id).where(func.lower(User.email) == email)):
        raise UserError("an account with this e-mail address already exists")
    user = User(email=email, display_name=display_name, password_hash=hash_password(password))
    session.add(user)
    session.flush()
    if org is not None:
        session.add(Membership(organization_id=org.id, user_id=user.id, role=role))
    session.add(AuditEvent(organization_id=org.id if org else None, actor_id=None, plane="platform", channel="api", action="user.created_cli",
                           target_type="user", target_id=str(user.id), meta={"organization": org_slug, "role": role}))
    session.commit()
    return UserResult(str(user.id), email, org_slug, role)
