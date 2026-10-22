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
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
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
    mfa_secret: Mapped[str | None] = mapped_column(Text, nullable=True)
    mfa_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
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


REGISTRY_KINDS = ("module", "template")


class RegistryEntry(Base):
    """One published version of a module or template (FR-126). Immutable; withdrawn by yanking, never deleted or replaced."""

    __tablename__ = "registry_entries"
    __table_args__ = (
        UniqueConstraint("organization_id", "name", "version", name="uq_registry_entries_version"),
        _in("kind", REGISTRY_KINDS, "kind"),
        _in("visibility", ("org", "instance"), "visibility"),
    )

    id: Mapped[uuid.UUID] = _id()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(63), nullable=False)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(10), nullable=False)
    description: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    license: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    visibility: Mapped[str] = mapped_column(String(10), nullable=False, default="org")
    digest: Mapped[str] = mapped_column(String(64), nullable=False)
    key_id: Mapped[str] = mapped_column(String(32), nullable=False)
    artifact_key: Mapped[str] = mapped_column(String(200), nullable=False)
    manifest: Mapped[dict[str, Any]] = mapped_column(PortableJSON, nullable=False)
    imported: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    yanked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    yanked_reason: Mapped[str | None] = mapped_column(String(300))
    published_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    created_at: Mapped[datetime] = _created()


class RegistryInstall(Base):
    """A module version offered to an application as a proposal; used to tell upgrades from local changes."""

    __tablename__ = "registry_installs"

    id: Mapped[uuid.UUID] = _id()
    application_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), nullable=False, index=True
    )
    package_ref: Mapped[str] = mapped_column(String(130), nullable=False)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    module_key: Mapped[str] = mapped_column(String(63), nullable=False)  # the key the module has in the application
    source_key: Mapped[str] = mapped_column(String(63), nullable=False, default="")  # the key it has in the package ("" = the same)
    action: Mapped[str] = mapped_column(String(10), nullable=False, default="install")  # install | uninstall
    dependency_of: Mapped[str | None] = mapped_column(String(130))  # the package whose install pulled this one in
    module_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    proposal_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    installed_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    created_at: Mapped[datetime] = _created()


class RegistryRemote(Base):
    """Another Orchivio server's registry that this organization can search, pull from and push to (docs/REGISTRY.md §5)."""

    __tablename__ = "registry_remotes"
    __table_args__ = (UniqueConstraint("organization_id", "name", name="uq_registry_remotes_name"),)

    id: Mapped[uuid.UUID] = _id()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(63), nullable=False)
    base_url: Mapped[str] = mapped_column(String(500), nullable=False)
    token_ref: Mapped[str | None] = mapped_column(String(300))
    remote_organization_id: Mapped[str | None] = mapped_column(String(64))  # where pushes go on the remote
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    created_at: Mapped[datetime] = _created()


class GitSync(Base):
    """How an application is synchronised with a Git repository (docs/GIT-SYNC.md). One per application; no secrets, only a reference."""

    __tablename__ = "git_syncs"

    id: Mapped[uuid.UUID] = _id()
    application_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    remote_url: Mapped[str] = mapped_column(String(500), nullable=False)
    base_branch: Mapped[str] = mapped_column(String(100), nullable=False, default="main")
    push_branch: Mapped[str] = mapped_column(String(100), nullable=False)
    path: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    token_ref: Mapped[str | None] = mapped_column(String(300))
    provider: Mapped[str] = mapped_column(String(10), nullable=False, default="git")
    last_push_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_push_commit: Mapped[str | None] = mapped_column(String(40))
    last_push_revision_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    last_pull_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_pull_commit: Mapped[str | None] = mapped_column(String(40))
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    created_at: Mapped[datetime] = _created()


class IdempotencyKey(Base):
    """A client-chosen key naming one request, so a retry after a lost answer does not do the work twice (docs/OFFLINE.md). Kept 24 hours."""

    __tablename__ = "idempotency_keys"
    __table_args__ = (UniqueConstraint("user_id", "key", name="uq_idempotency_keys_user_key"), Index("ix_idempotency_keys_created", "created_at"))

    id: Mapped[uuid.UUID] = _id()
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(12), nullable=False, default="running")
    status_code: Mapped[int | None] = mapped_column(Integer)
    response_body: Mapped[str | None] = mapped_column(Text)
    content_type: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = _created()


REQUIREMENT_STATUS = ("draft", "accepted", "implemented", "rejected")
STORY_STATUS = ("proposed", "backlog", "ready", "in_progress", "done", "rejected")
ITEM_SOURCE = ("manual", "ai", "feedback")
LINK_TYPES = ("requirement", "story", "element", "revision", "test")
FEEDBACK_STATUS = ("new", "clustered", "dismissed")


class Requirement(Base):
    """A requirement of a project (FR-098); traced to stories, model elements, revisions and tests."""

    __tablename__ = "requirements"
    __table_args__ = (_in("status", REQUIREMENT_STATUS, "status"), _in("source", ITEM_SOURCE, "source"))

    id: Mapped[uuid.UUID] = _id()
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="manual")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    created_at: Mapped[datetime] = _created()


class Story(Base):
    """A story on the board. AI-created stories start as ``proposed`` and need a person to move them to the backlog."""

    __tablename__ = "stories"
    __table_args__ = (_in("status", STORY_STATUS, "status"), _in("source", ITEM_SOURCE, "source"))

    id: Mapped[uuid.UUID] = _id()
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    requirement_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("requirements.id", ondelete="SET NULL"))
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    acceptance: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="backlog")
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="manual")
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    created_at: Mapped[datetime] = _created()


class TraceLink(Base):
    __tablename__ = "trace_links"
    __table_args__ = (
        UniqueConstraint("project_id", "from_type", "from_id", "to_type", "to_id", name="uq_trace_links_edge"),
        _in("from_type", ("requirement", "story"), "from_type"),
        _in("to_type", LINK_TYPES, "to_type"),
    )

    id: Mapped[uuid.UUID] = _id()
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    from_type: Mapped[str] = mapped_column(String(16), nullable=False)
    from_id: Mapped[str] = mapped_column(String(200), nullable=False)
    to_type: Mapped[str] = mapped_column(String(16), nullable=False)
    to_id: Mapped[str] = mapped_column(String(200), nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    created_at: Mapped[datetime] = _created()


class FeedbackItem(Base):
    """Feedback captured while previewing an application (FR-099). The text is untrusted."""

    __tablename__ = "feedback_items"
    __table_args__ = (_in("status", FEEDBACK_STATUS, "status"),)

    id: Mapped[uuid.UUID] = _id()
    application_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("applications.id", ondelete="CASCADE"), nullable=False, index=True)
    page_key: Mapped[str] = mapped_column(String(200), nullable=False)
    element_id: Mapped[str | None] = mapped_column(String(64))
    body: Mapped[str] = mapped_column(Text, nullable=False)
    screenshot: Mapped[bytes | None] = mapped_column(LargeBinary)
    screenshot_type: Mapped[str | None] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="new")
    story_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("stories.id", ondelete="SET NULL"))
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    created_at: Mapped[datetime] = _created()


APP_LOG_LEVELS = ("debug", "info", "warning", "error")


class AppLog(Base):
    """Operational log lines of a running application (FR-123): background jobs, webhook deliveries, integration calls.

    Short fixed messages and allow-listed scalar fields only; never record data, URLs, headers or error texts. Pruned by age.
    """

    __tablename__ = "app_logs"
    __table_args__ = (
        _in("level", APP_LOG_LEVELS, "level"),
        Index("ix_app_logs_app_time", "application_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    application_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("applications.id", ondelete="CASCADE"), nullable=False)
    level: Mapped[str] = mapped_column(String(8), nullable=False)
    source: Mapped[str] = mapped_column(String(24), nullable=False)
    message: Mapped[str] = mapped_column(String(300), nullable=False)
    request_id: Mapped[str | None] = mapped_column(String(64))
    trace_id: Mapped[str | None] = mapped_column(String(32))
    fields: Mapped[dict[str, Any]] = mapped_column(PortableJSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = _created()


JOB_RUN_STATUS = ("queued", "running", "succeeded", "dead", "cancelled")


class JobRun(Base):
    """Background task queue and the history of scheduled runs (FR-055/056).

    ``(application_id, flow, scheduled_for)`` is unique, so however many nodes plan the same cron occurrence
    only one row exists. Manual and retry rows have no ``scheduled_for`` and are not constrained.
    """

    __tablename__ = "job_runs"
    __table_args__ = (
        _in("status", JOB_RUN_STATUS, "status"),
        _in("trigger", ("schedule", "manual", "webhook"), "trigger"),
        UniqueConstraint("application_id", "flow", "scheduled_for", name="uq_job_runs_occurrence"),
        Index("ix_job_runs_due", "status", "available_at"),
    )

    id: Mapped[uuid.UUID] = _id()
    application_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), nullable=False, index=True
    )
    flow: Mapped[str] = mapped_column(String(130), nullable=False)  # module.flow
    args: Mapped[dict[str, Any]] = mapped_column(PortableJSON, nullable=False, default=dict)
    run_as_role: Mapped[str] = mapped_column(String(64), nullable=False)
    trigger: Mapped[str] = mapped_column(String(16), nullable=False)
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    backoff_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=60)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    locked_by: Mapped[str | None] = mapped_column(String(100))
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    environment_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    model_hash: Mapped[str | None] = mapped_column(String(80))
    error: Mapped[str | None] = mapped_column(Text)
    result: Mapped[dict[str, Any] | None] = mapped_column(PortableJSON)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    source_ref: Mapped[str | None] = mapped_column(String(200))  # e.g. the inbound webhook and event that caused it
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ScheduleState(Base):
    """Where the scheduler stopped planning for one scheduled flow. Advanced by compare-and-set."""

    __tablename__ = "schedule_state"
    __table_args__ = (UniqueConstraint("application_id", "flow", name="uq_schedule_state_flow"),)

    id: Mapped[uuid.UUID] = _id()
    application_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), nullable=False, index=True
    )
    flow: Mapped[str] = mapped_column(String(130), nullable=False)
    last_planned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


DEPLOYMENT_STATUS = ("pending_approval", "deployed", "rejected", "superseded")


class Environment(Base):
    """A delivery stage of an application (dev/test/prod...), ordered by ``stage_order``."""

    __tablename__ = "environments"
    __table_args__ = (
        UniqueConstraint("application_id", "name", name="uq_environments_app_name"),
        UniqueConstraint("application_id", "stage_order", name="uq_environments_app_stage"),
    )

    id: Mapped[uuid.UUID] = _id()
    application_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(40), nullable=False)
    stage_order: Mapped[int] = mapped_column(Integer, nullable=False)
    requires_approval: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    require_tests: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    data_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = _created()


class EnvironmentConstant(Base):
    """Per-environment configuration. A constant holds a value *or* a reference to a secret store, never a secret."""

    __tablename__ = "environment_constants"
    __table_args__ = (
        UniqueConstraint("environment_id", "key", name="uq_environment_constants_key"),
        CheckConstraint("(value IS NULL) <> (secret_ref IS NULL)", name="value_xor_secret_ref"),
    )

    id: Mapped[uuid.UUID] = _id()
    environment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("environments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[str | None] = mapped_column(String(2000))
    secret_ref: Mapped[str | None] = mapped_column(String(300))


class DeploymentPackage(Base):
    """Immutable, signed build of one revision (bytes live in the artifact store under ``artifact_key``)."""

    __tablename__ = "deployment_packages"
    __table_args__ = (UniqueConstraint("digest", name="uq_deployment_packages_digest"),)

    id: Mapped[uuid.UUID] = _id()
    application_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), nullable=False, index=True
    )
    revision_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("app_revisions.id"), nullable=False, index=True)
    runtime_version: Mapped[str] = mapped_column(String(40), nullable=False)
    model_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    digest: Mapped[str] = mapped_column(String(64), nullable=False)
    key_id: Mapped[str] = mapped_column(String(32), nullable=False)
    signature: Mapped[str] = mapped_column(String(200), nullable=False)
    artifact_key: Mapped[str] = mapped_column(String(200), nullable=False)
    manifest: Mapped[dict[str, Any]] = mapped_column(PortableJSON, nullable=False, default=dict)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    created_at: Mapped[datetime] = _created()


class PackageDeployment(Base):
    __tablename__ = "package_deployments"
    __table_args__ = (_in("status", DEPLOYMENT_STATUS, "status"),)

    id: Mapped[uuid.UUID] = _id()
    environment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("environments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    package_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("deployment_packages.id"), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    requested_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    decided_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    comment: Mapped[str | None] = mapped_column(String(1000))
    risks_acknowledged: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AppUser(Base):
    """An end user of a generated application (application plane). Unrelated to Studio ``users``."""

    __tablename__ = "app_users"
    __table_args__ = (
        UniqueConstraint("application_id", "username", name="uq_app_users_username"),
        _in("status", USER_STATUS, "status"),
    )

    id: Mapped[uuid.UUID] = _id()
    application_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), nullable=False, index=True
    )
    username: Mapped[str] = mapped_column(String(100), nullable=False)  # stored lower-case
    display_name: Mapped[str | None] = mapped_column(String(200))
    password_hash: Mapped[str | None] = mapped_column(String(255))  # null for federated users (OIDC, later)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    must_change_password: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    reset_token_hash: Mapped[str | None] = mapped_column(String(64))  # SHA-256 hex of the reset token (FR-067)
    reset_token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created()
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AppUserRole(Base):
    __tablename__ = "app_user_roles"

    app_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("app_users.id", ondelete="CASCADE"), primary_key=True
    )
    role_key: Mapped[str] = mapped_column(String(64), primary_key=True)


class AppSession(Base):
    """Login session of an application end user; the cookie holds an opaque token, only its hash is stored."""

    __tablename__ = "app_sessions"

    id: Mapped[uuid.UUID] = _id()
    app_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("app_users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    application_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    token_hash: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    csrf_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created()


WEBHOOK_STATUS = ("active", "paused")
DELIVERY_STATUS = ("queued", "sending", "delivered", "dead")


class WebhookSubscription(Base):
    """An outbound webhook: which business events of an application go to which URL (FR-082)."""

    __tablename__ = "webhook_subscriptions"
    __table_args__ = (
        UniqueConstraint("application_id", "name", name="uq_webhook_subscriptions_name"),
        _in("status", WEBHOOK_STATUS, "status"),
    )

    id: Mapped[uuid.UUID] = _id()
    application_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    url: Mapped[str] = mapped_column(String(2000), nullable=False)
    secret_ref: Mapped[str] = mapped_column(String(300), nullable=False)  # a reference, never the secret
    events: Mapped[list[Any]] = mapped_column(PortableJSON, nullable=False, default=list)  # glob patterns
    fields: Mapped[list[Any] | None] = mapped_column(PortableJSON)  # None = ids only; ["*"] = whole record
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="active")
    paused_reason: Mapped[str | None] = mapped_column(String(300))
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    created_at: Mapped[datetime] = _created()


class WebhookDelivery(Base):
    """One event for one subscription, delivered at least once. Unique per (subscription, event)."""

    __tablename__ = "webhook_deliveries"
    __table_args__ = (
        UniqueConstraint("subscription_id", "event_id", name="uq_webhook_deliveries_event"),
        _in("status", DELIVERY_STATUS, "status"),
        Index("ix_webhook_deliveries_due", "status", "available_at"),
    )

    id: Mapped[uuid.UUID] = _id()
    subscription_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("webhook_subscriptions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    event_id: Mapped[str] = mapped_column(String(36), nullable=False)
    event_type: Mapped[str] = mapped_column(String(200), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(PortableJSON, nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="queued")
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=6)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    locked_by: Mapped[str | None] = mapped_column(String(100))
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    response_status: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class InboundWebhook(Base):
    """A signed HTTP endpoint that starts a server flow (FR-082, inbound). Authenticated by its signature alone."""

    __tablename__ = "inbound_webhooks"
    __table_args__ = (
        UniqueConstraint("application_id", "key", name="uq_inbound_webhooks_key"),
        _in("status", WEBHOOK_STATUS, "status"),
    )

    id: Mapped[uuid.UUID] = _id()
    application_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), nullable=False, index=True
    )
    key: Mapped[str] = mapped_column(String(63), nullable=False)  # path segment of the public URL
    flow: Mapped[str] = mapped_column(String(130), nullable=False)  # module.flow
    run_as_role: Mapped[str] = mapped_column(String(64), nullable=False)
    secret_ref: Mapped[str] = mapped_column(String(300), nullable=False)
    arg_map: Mapped[dict[str, Any]] = mapped_column(PortableJSON, nullable=False, default=dict)  # param -> JSON pointer
    defaults: Mapped[dict[str, Any]] = mapped_column(PortableJSON, nullable=False, default=dict)  # param -> literal
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="active")
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    created_at: Mapped[datetime] = _created()


class InboundDelivery(Base):
    """Receipt of one accepted request; unique per (webhook, event) so a replay never runs a flow twice."""

    __tablename__ = "inbound_deliveries"
    __table_args__ = (UniqueConstraint("webhook_id", "event_id", name="uq_inbound_deliveries_event"),)

    id: Mapped[uuid.UUID] = _id()
    webhook_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("inbound_webhooks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class IntegrationDestination(Base):
    """Where a logical integration destination lives for one application: base URL and how to authenticate.
    Configured outside the model so the model stays portable and every target is explicitly allowlisted."""

    __tablename__ = "integration_destinations"
    __table_args__ = (
        UniqueConstraint("application_id", "name", name="uq_integration_destinations_name"),
        _in("auth_type", ("none", "bearer", "header"), "auth_type"),
    )

    id: Mapped[uuid.UUID] = _id()
    application_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(63), nullable=False)
    base_url: Mapped[str] = mapped_column(String(2000), nullable=False)
    auth_type: Mapped[str] = mapped_column(String(10), nullable=False, default="none")
    auth_header: Mapped[str | None] = mapped_column(String(64))
    secret_ref: Mapped[str | None] = mapped_column(String(300))  # a reference, never the credential
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    created_at: Mapped[datetime] = _created()


class McpServer(Base):
    """An external MCP server the AI Builder may use for background information (FR-087). Organization level:
    registering one is an admin act, and tools are usable only after an admin enables them explicitly."""

    __tablename__ = "mcp_servers"
    __table_args__ = (
        UniqueConstraint("organization_id", "name", name="uq_mcp_servers_name"),
        _in("status", ("active", "disabled"), "status"),
    )

    id: Mapped[uuid.UUID] = _id()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(63), nullable=False)
    url: Mapped[str] = mapped_column(String(2000), nullable=False)
    secret_ref: Mapped[str | None] = mapped_column(String(300))  # bearer credential, by reference only
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="active")
    tools: Mapped[list[Any]] = mapped_column(PortableJSON, nullable=False, default=list)  # last discovery, normalised
    tool_policy: Mapped[dict[str, Any]] = mapped_column(PortableJSON, nullable=False, default=dict)  # name -> {enabled, level}
    discovered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    created_at: Mapped[datetime] = _created()


PROCESS_STATUS = ("running", "completed", "failed", "cancelled")
TASK_STATUS = ("open", "completed", "cancelled")


class ProcessInstance(Base):
    """A running (or finished) process. ``state`` is the engine's JSON state; ``version`` makes updates compare-and-set.
    The instance is pinned to the revision whose process definition it started with."""

    __tablename__ = "process_instances"
    __table_args__ = (_in("status", PROCESS_STATUS, "status"), Index("ix_process_instances_wake", "next_wake_at"))

    id: Mapped[uuid.UUID] = _id()
    application_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), nullable=False, index=True
    )
    revision_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("app_revisions.id"), nullable=False)
    process: Mapped[str] = mapped_column(String(130), nullable=False)  # module.Key
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="running")
    migration_policy: Mapped[str] = mapped_column(String(10), nullable=False, default="pin")  # pin|migrate (FR-112)
    state: Mapped[dict[str, Any]] = mapped_column(PortableJSON, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    next_wake_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    initiator: Mapped[str] = mapped_column(String(64), nullable=False)  # app user id, or studio:<user id>
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class UserTask(Base):
    """A task in the inbox (FR-111): offered to a role (anyone holding it may claim) or assigned to one user."""

    __tablename__ = "user_tasks"
    __table_args__ = (
        _in("status", TASK_STATUS, "status"),
        Index("ix_user_tasks_role", "application_id", "status", "assignee_role"),
        Index("ix_user_tasks_user", "application_id", "status", "assignee_user"),
        Index("ix_user_tasks_escalation", "status", "escalate_at"),
    )

    id: Mapped[uuid.UUID] = _id()
    instance_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("process_instances.id", ondelete="CASCADE"), nullable=False, index=True
    )
    application_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    step_id: Mapped[str] = mapped_column(String(40), nullable=False)
    token_id: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    assignee_role: Mapped[str | None] = mapped_column(String(64))
    assignee_user: Mapped[str | None] = mapped_column(String(64))
    delegated_by: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="open")
    outcomes: Mapped[list[Any]] = mapped_column(PortableJSON, nullable=False)
    form: Mapped[list[Any]] = mapped_column(PortableJSON, nullable=False)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    escalate_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    escalate_to_role: Mapped[str | None] = mapped_column(String(64))
    escalated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_by: Mapped[str | None] = mapped_column(String(64))
    outcome: Mapped[str | None] = mapped_column(String(31))
    result: Mapped[dict[str, Any] | None] = mapped_column(PortableJSON)


class ProcessEvent(Base):
    """What happened to an instance, in order: the monitoring and audit trail of the process (FR-113, basic)."""

    __tablename__ = "process_events"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    instance_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("process_instances.id", ondelete="CASCADE"), nullable=False, index=True
    )
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    type: Mapped[str] = mapped_column(String(40), nullable=False)
    step_id: Mapped[str | None] = mapped_column(String(40))
    actor: Mapped[str | None] = mapped_column(String(64))
    data: Mapped[dict[str, Any]] = mapped_column(PortableJSON, nullable=False, default=dict)


class AppOidcProvider(Base):
    """OpenID Connect sign-in for the end users of one application (FR-067, P1). One provider per application."""

    __tablename__ = "app_oidc_providers"
    __table_args__ = (UniqueConstraint("application_id", name="uq_app_oidc_providers_app"), _in("status", ("active", "disabled"), "status"))

    id: Mapped[uuid.UUID] = _id()
    application_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), nullable=False, index=True
    )
    issuer: Mapped[str] = mapped_column(String(500), nullable=False)
    client_id: Mapped[str] = mapped_column(String(300), nullable=False)
    client_secret_ref: Mapped[str | None] = mapped_column(String(300))  # reference only; None = public client (PKCE)
    scopes: Mapped[str] = mapped_column(String(300), nullable=False, default="openid email profile")
    username_claim: Mapped[str] = mapped_column(String(64), nullable=False, default="email")
    role_claim: Mapped[str] = mapped_column(String(64), nullable=False, default="groups")
    role_mapping: Mapped[dict[str, Any]] = mapped_column(PortableJSON, nullable=False, default=dict)  # claim value -> app role
    default_roles: Mapped[list[Any]] = mapped_column(PortableJSON, nullable=False, default=list)
    auto_provision: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    allowed_email_domains: Mapped[list[Any] | None] = mapped_column(PortableJSON)
    post_login_url: Mapped[str] = mapped_column(String(500), nullable=False, default="/")
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="active")
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    created_at: Mapped[datetime] = _created()


class OidcLoginState(Base):
    """One pending sign-in: state, nonce and PKCE verifier, single use, bound to the browser by a cookie."""

    __tablename__ = "oidc_login_states"
    __table_args__ = (UniqueConstraint("state_hash", name="uq_oidc_login_states_state"),)

    id: Mapped[uuid.UUID] = _id()
    application_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    state_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    browser_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    nonce: Mapped[str] = mapped_column(String(64), nullable=False)
    code_verifier: Mapped[str] = mapped_column(String(128), nullable=False)
    next_path: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AppUserIdentity(Base):
    """Links an application user to an identity at an external provider; the pair (issuer, subject) is the identity."""

    __tablename__ = "app_user_identities"
    __table_args__ = (UniqueConstraint("application_id", "issuer", "subject", name="uq_app_user_identities_subject"),)

    id: Mapped[uuid.UUID] = _id()
    application_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    app_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("app_users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    issuer: Mapped[str] = mapped_column(String(500), nullable=False)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str | None] = mapped_column(String(320))
    created_at: Mapped[datetime] = _created()
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PolicyPack(Base):
    """An organization's governance rules, enforced on every new revision of every application in it (FR-124)."""

    __tablename__ = "policy_packs"
    __table_args__ = (
        UniqueConstraint("organization_id", "name", name="uq_policy_packs_name"),
        _in("status", ("active", "disabled"), "status"),
    )

    id: Mapped[uuid.UUID] = _id()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500))
    rules: Mapped[list[Any]] = mapped_column(PortableJSON, nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="active")
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    created_at: Mapped[datetime] = _created()
