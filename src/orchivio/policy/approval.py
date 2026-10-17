"""Approval levels (DESIGN.md section 12.3) and platform-plane RBAC (FR-003).

Pure functions, no I/O, so the same rules can be enforced from the API, the Studio
MCP server and the AI orchestrator (one pipeline, no privileged side channel).
"""

from __future__ import annotations

from enum import IntEnum, StrEnum


class ApprovalLevel(IntEnum):
    A0 = 0  # read, explain, summarise: may run automatically
    A1 = 1  # non-destructive proposals: generation allowed, application needs approval
    A2 = 2  # apply model changes, add dependencies, create migrations
    A3 = 3  # send data externally, change permissions, delete data
    A4 = 4  # production deployment, secret changes, destructive operations


class PlatformRole(StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    EDITOR = "editor"
    VIEWER = "viewer"


# Action -> minimum platform role. Anything not listed is denied (deny by default).
_MIN_ROLE: dict[str, PlatformRole] = {
    "project.read": PlatformRole.VIEWER,
    "model.read": PlatformRole.VIEWER,
    "audit.read": PlatformRole.ADMIN,
    "model.propose": PlatformRole.EDITOR,
    "model.apply": PlatformRole.EDITOR,
    "model.rollback": PlatformRole.EDITOR,
    "project.create": PlatformRole.ADMIN,
    "application.create": PlatformRole.EDITOR,
    "project.update": PlatformRole.ADMIN,
    "project.export": PlatformRole.ADMIN,
    "member.manage": PlatformRole.ADMIN,
    "token.manage": PlatformRole.ADMIN,
    "appuser.manage": PlatformRole.ADMIN,  # end users of generated applications
    "delivery.manage": PlatformRole.ADMIN,  # environments, constants, secret references
    "package.build": PlatformRole.EDITOR,
    "deployment.request": PlatformRole.EDITOR,
    "deployment.approve": PlatformRole.ADMIN,
    "project.delete": PlatformRole.OWNER,
    "organization.delete": PlatformRole.OWNER,
}
KNOWN_ACTIONS = frozenset(_MIN_ROLE)

_RANK = {
    PlatformRole.VIEWER: 0,
    PlatformRole.EDITOR: 1,
    PlatformRole.ADMIN: 2,
    PlatformRole.OWNER: 3,
}

# Actions that additionally need an explicit approval step at this level.
_ACTION_LEVEL: dict[str, ApprovalLevel] = {
    "model.read": ApprovalLevel.A0,
    "model.propose": ApprovalLevel.A1,
    "model.apply": ApprovalLevel.A2,
    "model.rollback": ApprovalLevel.A2,
    "project.export": ApprovalLevel.A3,
    "project.delete": ApprovalLevel.A3,
    "package.build": ApprovalLevel.A2,
    "deployment.request": ApprovalLevel.A2,
    "deployment.approve": ApprovalLevel.A4,
    "delivery.manage": ApprovalLevel.A3,
    "deployment.production": ApprovalLevel.A4,
    "secret.change": ApprovalLevel.A4,
}


def is_allowed(role: PlatformRole | str | None, action: str) -> bool:
    """May a member with ``role`` perform ``action``? Unknown roles/actions are denied."""
    if role is None:
        return False
    try:
        r = PlatformRole(role)
    except ValueError:
        return False
    need = _MIN_ROLE.get(action)
    return need is not None and _RANK[r] >= _RANK[need]


_LEVEL_ROLE: dict[ApprovalLevel, PlatformRole] = {
    ApprovalLevel.A0: PlatformRole.VIEWER,
    ApprovalLevel.A1: PlatformRole.EDITOR,
    ApprovalLevel.A2: PlatformRole.EDITOR,
    ApprovalLevel.A3: PlatformRole.ADMIN,
    ApprovalLevel.A4: PlatformRole.OWNER,
}


def can_approve(role: PlatformRole | str | None, level: ApprovalLevel | int) -> bool:
    """May a member with ``role`` approve a proposal that needs ``level``? A3+ also needs an
    explicit acknowledgement from the caller (enforced where the decision is taken)."""
    if role is None:
        return False
    try:
        r = PlatformRole(role)
        need = _LEVEL_ROLE[ApprovalLevel(level)]
    except (ValueError, KeyError):
        return False
    return _RANK[r] >= _RANK[need]


def level_for_action(action: str) -> ApprovalLevel:
    """Approval level an action needs. Unknown actions default to the strictest (A4)."""
    return _ACTION_LEVEL.get(action, ApprovalLevel.A4)
