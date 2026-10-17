"""Platform-plane metadata schema (DESIGN.md section 9, docs/DATABASE.md).

Portable across PostgreSQL (production) and SQLite (unit tests): JSON uses JSONB on
PostgreSQL, and ``parent_revision_ids`` is a native UUID[] there.
Application data of generated apps never lives in these tables.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    TypeDecorator,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeEngine

PortableJSON = JSON().with_variant(JSONB(), "postgresql")


class UuidList(TypeDecorator[list[uuid.UUID]]):
    """``list[UUID]``: native UUID[] on PostgreSQL, a JSON array of strings elsewhere."""

    impl = JSON
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[Any]:
        if dialect.name == "postgresql":
            return dialect.type_descriptor(ARRAY(Uuid()))
        return dialect.type_descriptor(JSON())

    def process_bind_param(self, value: list[Any] | None, dialect: Dialect) -> Any:
        if value is None:
            return None
        ids = [v if isinstance(v, uuid.UUID) else uuid.UUID(str(v)) for v in value]
        return ids if dialect.name == "postgresql" else [str(i) for i in ids]

    def process_result_value(self, value: Any, dialect: Dialect) -> list[uuid.UUID] | None:
        return None if value is None else [uuid.UUID(str(v)) for v in value]


class Base(DeclarativeBase):
    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(column_0_label)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )


def _id() -> Mapped[uuid.UUID]:
    return mapped_column(Uuid, primary_key=True, default=uuid.uuid4)


def _created() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


def _in(column: str, values: tuple[str, ...], name: str) -> CheckConstraint:
    quoted = ", ".join(f"'{v}'" for v in values)
    return CheckConstraint(f"{column} IN ({quoted})", name=name)


MEMBER_ROLES = ("owner", "admin", "editor", "viewer")
USER_STATUS = ("active", "disabled")
JOB_STATUS = ("queued", "running", "succeeded", "failed", "cancelled")
PROPOSAL_STATUS = ("pending", "approved", "rejected", "applied", "superseded")


class User(Base):
    __tablename__ = "users"
    __table_args__ = (_in("status", USER_STATUS, "status"),)

    id: Mapped[uuid.UUID] = _id()
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    password_hash: Mapped[str | None] = mapped_column(String(255))  # null for SSO-only users
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    created_at: Mapped[datetime] = _created()


# Emails are unique case-insensitively (DESIGN.md section 9.1).
Index("uq_users_email_lower", func.lower(User.email), unique=True)


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[uuid.UUID] = _id()
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(63), nullable=False, unique=True)
    created_at: Mapped[datetime] = _created()


class Membership(Base):
    __tablename__ = "memberships"
    __table_args__ = (_in("role", MEMBER_ROLES, "role"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = _created()


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (UniqueConstraint("organization_id", "slug", name="uq_projects_org_slug"),)

    id: Mapped[uuid.UUID] = _id()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(63), nullable=False)
    settings: Mapped[dict[str, Any]] = mapped_column(PortableJSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = _created()


class Application(Base):
    __tablename__ = "applications"
    __table_args__ = (UniqueConstraint("project_id", "key", name="uq_applications_project_key"),)

    id: Mapped[uuid.UUID] = _id()
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    key: Mapped[str] = mapped_column(String(63), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    current_revision_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_revisions.id", use_alter=True, name="fk_applications_current_revision")
    )
    created_at: Mapped[datetime] = _created()


class Branch(Base):
    """Model branches. The MVP exposes only ``main`` but the table exists from day one."""

    __tablename__ = "branches"
    __table_args__ = (
        UniqueConstraint("application_id", "name", name="uq_branches_app_name"),
        _in("status", ("open", "merged", "abandoned"), "status"),
    )

    id: Mapped[uuid.UUID] = _id()
    application_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    head_revision_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_revisions.id", use_alter=True, name="fk_branches_head_revision")
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    created_at: Mapped[datetime] = _created()


class AppRevision(Base):
    """Immutable model revision. A PostgreSQL trigger rejects UPDATEs (migration 0001)."""

    __tablename__ = "app_revisions"
    __table_args__ = (
        UniqueConstraint("application_id", "revision_no", name="uq_app_revisions_app_no"),
        Index("ix_app_revisions_branch", "branch_id", "revision_no"),
    )

    id: Mapped[uuid.UUID] = _id()
    application_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), nullable=False
    )
    branch_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("branches.id"))
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False)
    parent_revision_ids: Mapped[list[uuid.UUID]] = mapped_column(UuidList, nullable=False, default=list)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False)
    model_json: Mapped[dict[str, Any]] = mapped_column(PortableJSON, nullable=False)
    model_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    message: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = _created()


class AIConversation(Base):
    __tablename__ = "ai_conversations"

    id: Mapped[uuid.UUID] = _id()
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    created_at: Mapped[datetime] = _created()


class AIMessage(Base):
    __tablename__ = "ai_messages"
    __table_args__ = (_in("role", ("user", "assistant", "tool", "system"), "role"),)

    id: Mapped[uuid.UUID] = _id()
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ai_conversations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    meta: Mapped[dict[str, Any]] = mapped_column("metadata", PortableJSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = _created()


class AIJob(Base):
    __tablename__ = "ai_jobs"
    __table_args__ = (_in("status", JOB_STATUS, "status"),)

    id: Mapped[uuid.UUID] = _id()
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    application_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("applications.id"))
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ai_conversations.id"))
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    input_ref: Mapped[str | None] = mapped_column(String(500))
    result_ref: Mapped[str | None] = mapped_column(String(500))
    provider: Mapped[str | None] = mapped_column(String(64))
    model: Mapped[str | None] = mapped_column(String(128))
    prompt_template_version: Mapped[str | None] = mapped_column(String(64))
    usage: Mapped[dict[str, Any]] = mapped_column(PortableJSON, nullable=False, default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = _created()
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Proposal(Base):
    """Unified change proposal: AI Builder, MCP agents and humans share this pipeline."""

    __tablename__ = "proposals"
    __table_args__ = (
        _in("status", PROPOSAL_STATUS, "status"),
        _in("source", ("ai_builder", "mcp", "human"), "source"),
        CheckConstraint("approval_level BETWEEN 0 AND 4", name="approval_level"),
    )

    id: Mapped[uuid.UUID] = _id()
    application_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), nullable=False, index=True
    )
    ai_job_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ai_jobs.id"))
    base_revision_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("app_revisions.id"))  # None: first revision
    result_revision_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("app_revisions.id"))
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    change_set_json: Mapped[dict[str, Any]] = mapped_column(PortableJSON, nullable=False)
    approval_level: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    approved_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = _created()
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Build(Base):
    __tablename__ = "builds"
    __table_args__ = (_in("status", JOB_STATUS, "status"),)

    id: Mapped[uuid.UUID] = _id()
    revision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("app_revisions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    artifact_ref: Mapped[str | None] = mapped_column(String(500))
    logs_ref: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = _created()


class TestRun(Base):
    __tablename__ = "test_runs"
    __test__ = False  # not a pytest class
    __table_args__ = (_in("status", JOB_STATUS, "status"),)

    id: Mapped[uuid.UUID] = _id()
    build_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("builds.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    summary: Mapped[dict[str, Any]] = mapped_column(PortableJSON, nullable=False, default=dict)
    report_ref: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = _created()


class Deployment(Base):
    __tablename__ = "deployments"
    __table_args__ = (_in("status", JOB_STATUS, "status"),)

    id: Mapped[uuid.UUID] = _id()
    application_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), nullable=False, index=True
    )
    environment: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    artifact_ref: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = _created()


class LintFinding(Base):
    __tablename__ = "lint_findings"
    __table_args__ = (_in("severity", ("error", "warning", "info"), "severity"),)

    id: Mapped[uuid.UUID] = _id()
    revision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("app_revisions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    rule_id: Mapped[str] = mapped_column(String(64), nullable=False)
    severity: Mapped[str] = mapped_column(String(8), nullable=False)
    element_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    suppressed: Mapped[bool] = mapped_column(nullable=False, default=False)
    justification: Mapped[str | None] = mapped_column(Text)


class ElementRef(Base):
    """Dependency-graph cache: which element references which (find usages)."""

    __tablename__ = "element_refs"

    revision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("app_revisions.id", ondelete="CASCADE"), primary_key=True
    )
    from_element_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    to_element_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, index=True)
    ref_kind: Mapped[str] = mapped_column(String(32), primary_key=True)


class UserSession(Base):
    """Server-side login session. The cookie holds an opaque token; only its hash is stored."""

    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = _id()
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    csrf_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    user_agent: Mapped[str | None] = mapped_column(String(300))
    ip: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = _created()


class ApiToken(Base):
    __tablename__ = "api_tokens"

    id: Mapped[uuid.UUID] = _id()
    owner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    scopes: Mapped[list[Any]] = mapped_column(PortableJSON, nullable=False, default=list)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created()


class AuditEvent(Base):
    """Append-only. No foreign keys: audit history must outlive the rows it describes.

    PostgreSQL triggers reject UPDATE and DELETE (migration 0001); retention purges run
    through a dedicated function owned by a separate database role.
    """

    __tablename__ = "audit_events"
    __table_args__ = (
        _in("plane", ("platform", "application"), "plane"),
        _in("channel", ("studio", "api", "mcp"), "channel"),
        Index("ix_audit_events_org_time", "organization_id", "created_at"),
        Index("ix_audit_events_target", "target_type", "target_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    organization_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    plane: Mapped[str] = mapped_column(String(16), nullable=False, default="platform")
    channel: Mapped[str] = mapped_column(String(8), nullable=False, default="api")
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    target_type: Mapped[str | None] = mapped_column(String(64))
    target_id: Mapped[str | None] = mapped_column(String(64))
    meta: Mapped[dict[str, Any]] = mapped_column("metadata", PortableJSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = _created()
