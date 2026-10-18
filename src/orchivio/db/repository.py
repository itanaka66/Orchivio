"""Revision persistence: immutable revisions, hash-checked, branch-ready (FR-025/026/090).

The repository enforces *model* rules (valid, identified, hashed, audited). Who may call
it (platform RBAC, tenant boundaries) is the caller's responsibility; see
``orchivio.policy``.
"""

from __future__ import annotations

import re
import uuid
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from orchivio.model import (
    AppModel,
    Conflict,
    Finding,
    Issue,
    ModelDiff,
    assign_element_ids,
    blocking,
    build_refs,
    diff_models,
    has_errors,
    lint,
    merge,
    model_hash,
    validate_model,
)
from orchivio.model.policies import evaluate as evaluate_policies

from .models import (
    Application,
    AppRevision,
    AuditEvent,
    Branch,
    ElementRef,
    LintFinding,
    PolicyPack,
    Project,
)

MAIN = "main"
BRANCH_NAME = re.compile(r"^[a-z0-9][a-z0-9._/-]{0,60}$")


class BranchError(Exception):
    """A branch operation is not allowed (bad name, duplicate, main, wrong application...)."""

    def __init__(self, message: str, code: str = "branch_error") -> None:
        super().__init__(message)
        self.code = code


@dataclass
class MergeOutcome:
    status: str  # up_to_date | conflicts | invalid | clean (dry run) | merged
    conflicts: list[Conflict] = field(default_factory=list)  # unresolved
    all_conflicts: list[Conflict] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    diff: ModelDiff | None = None
    revision: AppRevision | None = None
    merged_from_both: int = 0


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

    def _head(self, app: Application, branch: str) -> AppRevision | None:
        if branch == MAIN:
            return self._current(app)
        b = self._branch(app, branch)
        return self.session.get(AppRevision, b.head_revision_id) if b.head_revision_id else None

    def _branch(self, app: Application, name: str) -> Branch:
        b = self.session.scalar(select(Branch).where(Branch.application_id == app.id, Branch.name == name))
        if b is None:
            raise NotFound(f"branch {name!r}")
        return b

    def plan(self, application_id: uuid.UUID, model: AppModel, branch: str = MAIN) -> Plan:
        """Dry run of ``create``: assign ids, validate and diff. Writes nothing."""
        app = self.session.get(Application, application_id)
        if app is None:
            raise NotFound(f"application {application_id}")
        parent = self._head(app, branch)
        previous = to_model(parent) if parent else None
        identified = assign_element_ids(model, previous=previous)
        diff = diff_models(previous, identified) if previous else None
        issues = validate_model(identified)
        findings = lint(identified) if not has_errors(issues) else []
        issues += [
            Issue("error", f"lint:{f.rule_id}", f.path, f.message, f.element_id) for f in blocking(findings)
        ]
        if not has_errors(issues):
            issues += self.policy_issues(app, identified)
        return Plan(identified, issues, diff, findings)

    def policy_issues(self, app: Application, model: AppModel) -> list[Issue]:
        """Violations of the organization's active policy packs as issues (``policy:<rule>``): errors block, warnings inform."""
        project = self.session.get(Project, app.project_id)
        if project is None:
            return []
        rules: list[dict[str, Any]] = []
        for pack in self.session.scalars(select(PolicyPack).where(PolicyPack.organization_id == project.organization_id,
                                                                 PolicyPack.status == "active").order_by(PolicyPack.name)):
            rules += pack.rules
        return [Issue(v.severity, f"policy:{v.rule}", v.path, v.message, v.element_id) for v in evaluate_policies(model, rules)]

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
        branch: str = MAIN,
        parents: list[uuid.UUID] | None = None,
    ) -> AppRevision:
        """Validate ``model`` and store it as the next revision on ``branch`` (default ``main``).

        Element ids are carried over from the current revision. Raises ``ModelInvalid``
        if any error-severity issue is found; nothing is written in that case.
        """
        app = self._application(application_id)
        parent = self._head(app, branch)
        plan = self.plan(application_id, model, branch)
        if not plan.valid:
            raise ModelInvalid(plan.issues)
        identified = plan.model

        branch_row = self._main_branch(app) if branch == MAIN else self._branch(app, branch)
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
            branch_id=branch_row.id,
            revision_no=next_no,
            parent_revision_ids=parents if parents is not None else ([parent.id] if parent else []),
            schema_version=identified.schema_version,
            model_json=identified.model_dump(mode="json", exclude_none=True),
            model_hash=model_hash(identified),
            message=message,
            created_by=created_by,
        )
        self.session.add(rev)
        self.session.flush()
        self._store_analysis(rev, identified, plan.lint)
        if branch == MAIN:
            app.current_revision_id = rev.id
        branch_row.head_revision_id = rev.id
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

    # -- branches and merging ---------------------------------------------------------------------

    def branches(self, application_id: uuid.UUID) -> list[Branch]:
        app = self.session.get(Application, application_id)
        if app is None:
            raise NotFound(f"application {application_id}")
        self._main_branch(app)  # make sure the main branch row exists
        return list(self.session.scalars(select(Branch).where(Branch.application_id == application_id).order_by(Branch.created_at, Branch.name)))

    def branches_by_name(self, application_id: uuid.UUID, name: str) -> Branch:
        return self._branch(self._application(application_id), name)

    def create_branch(self, application_id: uuid.UUID, name: str, *, from_revision_id: uuid.UUID | None = None,
                      created_by: uuid.UUID | None = None, organization_id: uuid.UUID | None = None) -> Branch:
        """A new branch starting at ``from_revision_id`` (default: the head of ``main``)."""
        if not BRANCH_NAME.match(name) or name.endswith(("/", ".")) or ".." in name:
            raise BranchError("branch names use lowercase letters, digits and . _ / - (at most 61 characters)", "invalid_branch_name")
        app = self._application(application_id)
        self._main_branch(app)
        if self.session.scalar(select(Branch.id).where(Branch.application_id == app.id, Branch.name == name)):
            raise BranchError(f"a branch named {name!r} already exists", "branch_exists")
        start = self.get(from_revision_id) if from_revision_id else self._current(app)
        if start is None:
            raise BranchError("the application has no revision to branch from yet", "no_revision")
        if start.application_id != app.id:
            raise NotFound(f"revision {from_revision_id}")
        branch = Branch(application_id=app.id, name=name, head_revision_id=start.id, status="open")
        self.session.add(branch)
        self.session.flush()
        self.session.add(AuditEvent(organization_id=organization_id, actor_id=created_by, plane="platform", channel="api",
                                    action="branch.created", target_type="branch", target_id=str(branch.id),
                                    meta={"name": name, "from_revision_no": start.revision_no}))
        return branch

    def abandon_branch(self, branch: Branch, *, by: uuid.UUID | None = None, organization_id: uuid.UUID | None = None) -> None:
        if branch.name == MAIN:
            raise BranchError("the main branch cannot be abandoned", "main_protected")
        branch.status = "abandoned"
        self.session.add(AuditEvent(organization_id=organization_id, actor_id=by, plane="platform", channel="api",
                                    action="branch.abandoned", target_type="branch", target_id=str(branch.id), meta={"name": branch.name}))

    def _ancestors(self, application_id: uuid.UUID, start: uuid.UUID) -> set[uuid.UUID]:
        parents = dict(self.session.execute(
            select(AppRevision.id, AppRevision.parent_revision_ids).where(AppRevision.application_id == application_id)).all())
        seen, q = {start}, deque([start])
        while q:
            for p in parents.get(q.popleft(), []):
                if p not in seen:
                    seen.add(p)
                    q.append(p)
        return seen

    def merge_base(self, application_id: uuid.UUID, a: uuid.UUID, b: uuid.UUID) -> AppRevision | None:
        """Best common ancestor of two revisions (the one with the highest revision number among the common
        ancestors that are not ancestors of another common ancestor). ``None`` if they share no history."""
        common = self._ancestors(application_id, a) & self._ancestors(application_id, b)
        if not common:
            return None
        revs = {r.id: r for r in self.session.scalars(select(AppRevision).where(AppRevision.id.in_(common)))}
        shadowed = set()
        for rid in common:
            shadowed |= self._ancestors(application_id, rid) - {rid}
        best = [revs[r] for r in common if r not in shadowed]
        return max(best, key=lambda r: r.revision_no)

    def merge_branches(self, application_id: uuid.UUID, source: str, target: str = MAIN, *,
                       resolutions: Mapping[str, str] | None = None, dry_run: bool = False, message: str | None = None,
                       created_by: uuid.UUID | None = None, organization_id: uuid.UUID | None = None,
                       require_acknowledged: Callable[[ModelDiff], None] | None = None) -> MergeOutcome:
        """Merge ``source`` into ``target`` (three-way, by element). Nothing is written unless the result is
        conflict-free and valid; a dry run never writes."""
        if source == target:
            raise BranchError("a branch cannot be merged into itself", "same_branch")
        app = self._application(application_id)
        s_branch = self._branch(app, source) if source != MAIN else self._main_branch(app)
        if target != MAIN:
            self._branch(app, target)  # the target must exist
        s_head, t_head = self._head(app, source), self._head(app, target)
        if s_head is None or t_head is None:
            raise BranchError("both branches need at least one revision", "no_revision")
        if s_head.id == t_head.id or s_head.id in self._ancestors(application_id, t_head.id):
            return MergeOutcome("up_to_date")
        base_rev = self.merge_base(application_id, t_head.id, s_head.id)
        if base_rev is None:
            raise BranchError("the branches share no history", "no_common_ancestor")
        base, ours, theirs = (assign_element_ids(to_model(x)) for x in (base_rev, t_head, s_head))
        # Revisions are stored identified, so ids already agree; assigning is a no-op safety net.
        result = merge(base, ours, theirs, resolutions)
        if not result.clean:
            only_invalid = [c for c in result.conflicts if c.kind == "invalid"]
            return MergeOutcome("invalid" if only_invalid and len(only_invalid) == len(result.conflicts) else "conflicts",
                                result.conflicts, result.all_conflicts, [], None, None, result.merged_from_both)
        merged = assign_element_ids(result.model, previous=ours)
        issues = validate_model(merged)
        findings = lint(merged) if not has_errors(issues) else []
        issues += [Issue("error", f"lint:{f.rule_id}", f.path, f.message, f.element_id) for f in blocking(findings)]
        if not has_errors(issues):
            issues += self.policy_issues(app, merged)  # a merge may not smuggle in what a policy forbids
        diff = diff_models(ours, merged)
        if has_errors(issues):
            return MergeOutcome("invalid", [], result.all_conflicts, issues, diff, None, result.merged_from_both)
        if diff.is_empty:
            return MergeOutcome("up_to_date", all_conflicts=result.all_conflicts, diff=diff)
        if dry_run:
            return MergeOutcome("clean", [], result.all_conflicts, issues, diff, None, result.merged_from_both)
        if require_acknowledged is not None:
            require_acknowledged(diff)  # may raise to demand an explicit acknowledgement of A3+ changes
        rev = self.create(application_id, merged, created_by=created_by, organization_id=organization_id, branch=target,
                          parents=[t_head.id, s_head.id], message=message or f"Merge branch '{source}' into '{target}'")
        if source != MAIN:
            s_branch.status = "merged"
        self.session.add(AuditEvent(organization_id=organization_id, actor_id=created_by, plane="platform", channel="api",
                                    action="branch.merged", target_type="branch", target_id=str(s_branch.id),
                                    meta={"source": source, "target": target, "revision_no": rev.revision_no,
                                          "conflicts_resolved": len(result.all_conflicts)}))
        return MergeOutcome("merged", [], result.all_conflicts, issues, diff, rev, result.merged_from_both)

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
