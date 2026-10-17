"""Revision persistence: immutable revisions, hash-checked, branch-ready (FR-025/026/090).

The repository enforces *model* rules (valid, identified, hashed, audited). Who may call
it (platform RBAC, tenant boundaries) is the caller's responsibility; see
``orchivio.policy``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from orchivio.model import (
    AppModel,
    Finding,
    Issue,
    ModelDiff,
    assign_element_ids,
    blocking,
    build_refs,
    diff_models,
    has_errors,
    lint,
    model_hash,
    validate_model,
)

from .models import Application, AppRevision, AuditEvent, Branch, ElementRef, LintFinding

MAIN = "main"


@dataclass(frozen=True)
class Plan:
    """What ``create`` would store: the identified model, its issues and the diff vs current."""

    model: AppModel
    issues: list[Issue]  # validation errors/warnings plus unsuppressed lint errors
    diff: ModelDiff | None  # None for the first revision
    lint: list[Finding]

    @property
    def valid(self) -> bool:
        return not has_errors(self.issues)


class ModelInvalid(Exception):
    def __init__(self, issues: list[Issue]) -> None:
        super().__init__(f"{sum(i.severity == 'error' for i in issues)} validation error(s)")
        self.issues = issues


class NotFound(LookupError):
    pass


def to_model(rev: AppRevision) -> AppModel:
    return AppModel.model_validate(rev.model_json)


class RevisionRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    # -- reads
    def get(self, revision_id: uuid.UUID) -> AppRevision:
        rev = self.session.get(AppRevision, revision_id)
        if rev is None:
            raise NotFound(f"revision {revision_id}")
        return rev

    def list(self, application_id: uuid.UUID, limit: int = 50, offset: int = 0) -> list[AppRevision]:
        limit = max(1, min(limit, 200))  # bounded pagination (DESIGN.md section 10.1)
        stmt = (
            select(AppRevision)
            .where(AppRevision.application_id == application_id)
            .order_by(AppRevision.revision_no.desc())
            .limit(limit)
            .offset(max(0, offset))
        )
        return list(self.session.scalars(stmt))

    def diff(self, revision_id: uuid.UUID, base_revision_id: uuid.UUID) -> ModelDiff:
        return diff_models(to_model(self.get(base_revision_id)), to_model(self.get(revision_id)))

    # -- planning
    def _current(self, app: Application) -> AppRevision | None:
        return self.session.get(AppRevision, app.current_revision_id) if app.current_revision_id else None

    def plan(self, application_id: uuid.UUID, model: AppModel) -> Plan:
        """Dry run of ``create``: assign ids, validate and diff. Writes nothing."""
        app = self.session.get(Application, application_id)
        if app is None:
            raise NotFound(f"application {application_id}")
        parent = self._current(app)
        previous = to_model(parent) if parent else None
        identified = assign_element_ids(model, previous=previous)
        diff = diff_models(previous, identified) if previous else None
        issues = validate_model(identified)
        findings = lint(identified) if not has_errors(issues) else []
        issues += [
            Issue("error", f"lint:{f.rule_id}", f.path, f.message, f.element_id) for f in blocking(findings)
        ]
        return Plan(identified, issues, diff, findings)

    # -- writes
    def _application(self, application_id: uuid.UUID) -> Application:
        app = self.session.get(Application, application_id, with_for_update=True)
        if app is None:
            raise NotFound(f"application {application_id}")
        return app

    def _main_branch(self, app: Application) -> Branch:
        branch = self.session.scalar(
            select(Branch).where(Branch.application_id == app.id, Branch.name == MAIN)
        )
        if branch is None:
            branch = Branch(application_id=app.id, name=MAIN)
            self.session.add(branch)
            self.session.flush()
        return branch

    def create(
        self,
        application_id: uuid.UUID,
        model: AppModel,
        *,
        created_by: uuid.UUID | None,
        message: str | None = None,
        organization_id: uuid.UUID | None = None,
        channel: str = "api",
    ) -> AppRevision:
        """Validate ``model`` and store it as the next revision on ``main``.

        Element ids are carried over from the current revision. Raises ``ModelInvalid``
        if any error-severity issue is found; nothing is written in that case.
        """
        app = self._application(application_id)
        parent = self._current(app)
        plan = self.plan(application_id, model)
        if not plan.valid:
            raise ModelInvalid(plan.issues)
        identified = plan.model

        branch = self._main_branch(app)
        next_no = (
            self.session.scalar(
                select(func.max(AppRevision.revision_no)).where(
                    AppRevision.application_id == application_id
                )
            )
            or 0
        ) + 1
        rev = AppRevision(
            application_id=application_id,
            branch_id=branch.id,
            revision_no=next_no,
            parent_revision_ids=[parent.id] if parent else [],
            schema_version=identified.schema_version,
            model_json=identified.model_dump(mode="json", exclude_none=True),
            model_hash=model_hash(identified),
            message=message,
            created_by=created_by,
        )
        self.session.add(rev)
        self.session.flush()
        self._store_analysis(rev, identified, plan.lint)
        app.current_revision_id = rev.id
        branch.head_revision_id = rev.id
        self._audit(
            "revision.created",
            rev,
            created_by,
            organization_id,
            channel,
            {"revision_no": next_no, "model_hash": rev.model_hash},
        )
        return rev

    def rollback(
        self,
        application_id: uuid.UUID,
        to_revision_id: uuid.UUID,
        *,
        created_by: uuid.UUID | None,
        organization_id: uuid.UUID | None = None,
        channel: str = "api",
    ) -> AppRevision:
        """Restore an earlier model as a *new* revision (history is never rewritten)."""
        target = self.get(to_revision_id)
        if target.application_id != application_id:
            raise NotFound(f"revision {to_revision_id} in application {application_id}")
        rev = self.create(
            application_id,
            to_model(target),
            created_by=created_by,
            message=f"Rollback to revision {target.revision_no}",
            organization_id=organization_id,
            channel=channel,
        )
        self._audit(
            "revision.rolled_back",
            rev,
            created_by,
            organization_id,
            channel,
            {"restored_revision_no": target.revision_no},
        )
        return rev

    def _store_analysis(self, rev: AppRevision, model: AppModel, findings: list[Finding]) -> None:
        """Persist the dependency graph and lint results for this revision."""
        for r in build_refs(model):
            self.session.add(ElementRef(revision_id=rev.id, from_element_id=uuid.UUID(r.from_id),
                                        to_element_id=uuid.UUID(r.to_id), ref_kind=r.kind))
        for f in findings:
            self.session.add(LintFinding(
                revision_id=rev.id, rule_id=f.rule_id, severity=f.severity,
                element_id=uuid.UUID(f.element_id) if f.element_id else None,
                message=f.message, suppressed=f.suppressed, justification=f.justification))
        self.session.flush()

    def lint_findings(self, revision_id: uuid.UUID) -> list[LintFinding]:
        return list(self.session.scalars(select(LintFinding).where(LintFinding.revision_id == revision_id)
                                         .order_by(LintFinding.severity, LintFinding.rule_id)))

    def usages(self, revision_id: uuid.UUID, element_id: uuid.UUID) -> list[ElementRef]:
        return list(self.session.scalars(select(ElementRef).where(
            ElementRef.revision_id == revision_id, ElementRef.to_element_id == element_id)))

    def _audit(
        self,
        action: str,
        rev: AppRevision,
        actor_id: uuid.UUID | None,
        organization_id: uuid.UUID | None,
        channel: str,
        meta: dict[str, Any],
    ) -> None:
        self.session.add(
            AuditEvent(
                organization_id=organization_id,
                actor_id=actor_id,
                plane="platform",
                channel=channel,
                action=action,
                target_type="app_revision",
                target_id=str(rev.id),
                meta=meta,
            )
        )
