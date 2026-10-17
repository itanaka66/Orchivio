"""Request and response bodies."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

T = TypeVar("T")

Email = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, to_lower=True, max_length=320, pattern=r"^[^@\s]{1,64}@[^@\s]{1,255}$"
    ),
]
Slug = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]{0,61}[a-z0-9]$")]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
AppKey = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]{0,62}$")]
MemberRole = Annotated[str, StringConstraints(pattern=r"^(owner|admin|editor|viewer)$")]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Page(BaseModel, Generic[T]):
    items: list[T]
    limit: int
    offset: int
    total: int


# --- auth --------------------------------------------------------------------


class RegisterIn(_In):
    email: Email
    password: str = Field(max_length=1024)  # length rules are applied in the handler
    display_name: Name


class LoginIn(_In):
    email: Email
    password: str = Field(max_length=1024)


class UserOut(_Out):
    id: uuid.UUID
    email: str
    display_name: str


class LoginOut(BaseModel):
    user: UserOut
    csrf_token: str
    expires_at: datetime


class MembershipOut(BaseModel):
    organization_id: uuid.UUID
    slug: str
    name: str
    role: str


class MeOut(BaseModel):
    user: UserOut
    organizations: list[MembershipOut]
    auth: str


# --- organizations and projects ----------------------------------------------


class OrgIn(_In):
    name: Name
    slug: Slug


class OrgOut(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    role: str


class MemberIn(_In):
    email: Email
    role: MemberRole


class MemberPatch(_In):
    role: MemberRole


class MemberOut(BaseModel):
    user_id: uuid.UUID
    email: str
    display_name: str
    role: str


class ProjectIn(_In):
    name: Name
    slug: Slug
    settings: dict[str, Any] = Field(default_factory=dict)


class ProjectPatch(_In):
    name: Name | None = None
    settings: dict[str, Any] | None = None


class ProjectOut(_Out):
    id: uuid.UUID
    organization_id: uuid.UUID
    name: str
    slug: str
    settings: dict[str, Any]
    created_at: datetime


# --- applications and revisions ----------------------------------------------


class AppIn(_In):
    key: AppKey
    name: Name
    model: dict[str, Any] | None = None
    message: str | None = Field(default=None, max_length=1000)


class AppOut(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    key: str
    name: str
    current_revision_id: uuid.UUID | None
    current_revision_no: int | None
    created_at: datetime


class RevisionIn(_In):
    model: dict[str, Any]
    branch: str = Field(default="main", max_length=61)
    message: str | None = Field(default=None, max_length=1000)
    acknowledge_risks: bool = False


class BranchIn(_In):
    name: str = Field(min_length=1, max_length=61)
    from_revision_id: uuid.UUID | None = None


class BranchOut(_Out):
    id: uuid.UUID
    application_id: uuid.UUID
    name: str
    status: str
    head_revision_id: uuid.UUID | None
    created_at: datetime


class MergeIn(_In):
    into: str = Field(default="main", max_length=61)
    resolutions: dict[str, str] = Field(default_factory=dict)
    dry_run: bool = False
    message: str | None = Field(default=None, max_length=1000)
    acknowledge_risks: bool = False


class RollbackIn(_In):
    revision_id: uuid.UUID
    acknowledge_risks: bool = False


class RevisionOut(_Out):
    id: uuid.UUID
    application_id: uuid.UUID
    revision_no: int
    parent_revision_ids: list[uuid.UUID]
    schema_version: str
    model_hash: str
    message: str | None
    created_by: uuid.UUID | None
    created_at: datetime


class RevisionDetail(RevisionOut):
    model_json: dict[str, Any]


# --- tokens and audit --------------------------------------------------------


class TokenIn(_In):
    name: Name
    organization_id: uuid.UUID
    project_id: uuid.UUID | None = None
    scopes: list[str] = Field(default_factory=lambda: ["*"], max_length=50)
    expires_in_days: int = Field(default=30, ge=1, le=365)


class TokenOut(_Out):
    id: uuid.UUID
    name: str
    organization_id: uuid.UUID
    project_id: uuid.UUID | None
    scopes: list[str]
    expires_at: datetime
    revoked_at: datetime | None
    created_at: datetime


class TokenCreated(TokenOut):
    token: str


class AuditOut(BaseModel):
    id: int
    created_at: datetime
    actor_id: uuid.UUID | None
    action: str
    plane: str
    channel: str
    target_type: str | None
    target_id: str | None
    metadata: dict[str, Any]
