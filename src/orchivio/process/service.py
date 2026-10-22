"""Persistence and rules around the process engine: starting, task inbox actions, timers, escalation, cancel and retry.

Everything here runs on a caller-owned session and the caller commits, so an instance's state, its tasks and its event
trail change together. Instance state is a versioned JSON document updated by compare-and-set: two people acting on the
same instance at once cannot both win (the loser gets ``Conflict`` and nothing of theirs is written). System tasks run
server flows in the application's runtime, which is a different database, so those effects are not rolled back if the
state update then loses a race; that is the one place where an effect can repeat, and it is documented.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from orchivio import telemetry
from orchivio.db import to_model
from orchivio.db.models import (
    Application,
    AppRevision,
    AppUser,
    AppUserRole,
    AuditEvent,
    ProcessEvent,
    ProcessInstance,
    Project,
    UserTask,
)
from orchivio.logic import FlowHooks, FlowRunner
from orchivio.model import AppModel
from orchivio.model.validate import literal_fits
from orchivio.ports import AuditSink, IntegrationClient
from orchivio.runtime import AppUser as RuntimeUser
from orchivio.runtime import PreviewStore

from .engine import Hooks, Machine, ProcessError, State, TaskSpec, resolve_definition


class ServiceError(Exception):
    def __init__(self, status: int, code: str, message: str, details: Any = None) -> None:
        super().__init__(message)
        self.status, self.code, self.message, self.details = status, code, message, details


def aware(d: datetime | None) -> datetime | None:
    return d if d is None or d.tzinfo else d.replace(tzinfo=UTC)


def _utcnow() -> datetime:
    return datetime.now(UTC)


class _Hooks(Hooks):
    def __init__(self, svc: ProcessService, inst: ProcessInstance, actor: str | None) -> None:
        self.svc, self.inst, self.actor = svc, inst, actor

    def create_task(self, spec: TaskSpec) -> str:
        task = UserTask(instance_id=self.inst.id, application_id=self.inst.application_id, step_id=spec.step_id, token_id=spec.token_id,
                        title=spec.title, assignee_role=spec.assignee_role, assignee_user=spec.assignee_user, outcomes=list(spec.outcomes),
                        form=list(spec.form), due_at=spec.due_at, escalate_at=spec.escalate_at, escalate_to_role=spec.escalate_to_role,
                        created_at=self.svc.clock())
        self.svc.session.add(task)
        self.svc.session.flush()
        self.event("task_created", spec.step_id, {"task": str(task.id), "role": spec.assignee_role, "user": spec.assignee_user})
        return str(task.id)

    def send_message(self, message_type: str, payload: dict[str, Any], target: str) -> None:
        """Broadcast a message to all running instances of this app waiting for message_type (FR-110)."""
        waiters = self.svc.session.scalars(
            select(ProcessInstance).where(
                ProcessInstance.application_id == self.inst.application_id,
                ProcessInstance.status == "running",
                ProcessInstance.id != self.inst.id,
            )
        ).all()
        for w in waiters:
            st = State.from_json(w.state)
            if any(tok.status == "waiting_message" for tok in st.tokens):
                try:
                    self.svc._machine(w, self.actor).signal(st, message_type, payload)
                    self.svc._save(w, st)
                except (ProcessError, ServiceError):
                    pass

    def run_flow(self, flow: str, args: dict[str, Any]) -> str | None:
        return self.svc.run_system_flow(self.inst, flow, args)

    def run_process(self, process: str, args: dict[str, Any]) -> str | None:
        app = self.svc.session.get(Application, self.inst.application_id)
        if app is None:
            return "application not found"
        try:
            self.svc._start(app, process, args, initiator=f"subprocess:{self.inst.process}:{self.inst.id}",
                            roles=None, actor=self.actor)
        except ServiceError as e:
            return e.message
        return None

    def event(self, type_: str, step_id: str | None, data: dict[str, Any]) -> None:
        self.svc.session.add(ProcessEvent(instance_id=self.inst.id, at=self.svc.clock(), type=type_, step_id=step_id,
                                          actor=self.actor, data=data))
        telemetry.count("orchivio.process.events", type=type_)


class ProcessService:
    def __init__(self, session: Session, preview: PreviewStore, *, audit: AuditSink | None = None,
                 integrations: Callable[[uuid.UUID], IntegrationClient | None] | None = None,
                 clock: Callable[[], datetime] = _utcnow) -> None:
        self.session, self.preview, self.audit, self.integrations, self.clock = session, preview, audit, integrations, clock

    # -- loading ---------------------------------------------------------------------------------

    def _model(self, revision_id: uuid.UUID) -> AppModel:
        rev = self.session.get(AppRevision, revision_id)
        assert rev is not None
        return to_model(rev)

    def _machine(self, inst: ProcessInstance, actor: str | None) -> Machine:
        model = self._model(inst.revision_id)
        found = resolve_definition(model, inst.process)
        if found is None:  # cannot happen: the revision is immutable
            raise ServiceError(500, "process_missing", "the process definition is missing")
        return Machine(model, found[0], found[1], _Hooks(self, inst, actor), self.clock)

    def _save(self, inst: ProcessInstance, st: State) -> None:
        wake = st.next_wake_at
        finished = st.status in ("completed", "cancelled")
        terminal = finished or st.status == "failed"
        won = self.session.execute(update(ProcessInstance).where(ProcessInstance.id == inst.id, ProcessInstance.version == inst.version).values(
            state=st.to_json(), status=st.status, error=st.error, version=ProcessInstance.version + 1,
            next_wake_at=datetime.fromisoformat(wake) if wake and st.status == "running" else None,
            finished_at=self.clock() if finished else None)).rowcount
        if not won:
            raise ServiceError(409, "conflict", "the process was changed by someone else; reload and try again")
        self.session.expire(inst)
        if terminal:
            level = logging.INFO if finished else logging.WARNING
            telemetry.applog(inst.application_id, level, "process", f"process {st.status}",
                             event_type=f"process.{st.status}", process=inst.process, outcome=st.status)

    # -- starting --------------------------------------------------------------------------------

    def start(self, app: Application, process: str, args: dict[str, Any], *, initiator: str, roles: frozenset[str] | None,
              actor: str | None = None) -> ProcessInstance:
        """``roles=None`` means a builder in the Studio, who may start any process (preview); end users need a start role."""
        with telemetry.span("process.start", process=process[:100]):
            return self._start(app, process, args, initiator=initiator, roles=roles, actor=actor)

    def _start(self, app: Application, process: str, args: dict[str, Any], *, initiator: str, roles: frozenset[str] | None,
               actor: str | None) -> ProcessInstance:
        if app.current_revision_id is None:
            raise ServiceError(409, "no_revision", "the application has no model yet")
        model = self._model(app.current_revision_id)
        found = resolve_definition(model, process)
        if found is None:
            raise ServiceError(404, "unknown_process", "unknown process")
        mod, proc = found
        if roles is not None and not (set(proc.start_roles) & set(roles)):
            raise ServiceError(403, "forbidden", "your roles may not start this process")
        params = {p.name: p for p in proc.params}
        problems = [{"param": n, "message": "unknown parameter"} for n in args if n not in params]
        for n, p in params.items():
            if n not in args:
                problems.append({"param": n, "message": "is required"})
            elif not literal_fits(p.type, args[n]):
                problems.append({"param": n, "message": f"does not fit type '{p.type}'"})
        if problems:
            raise ServiceError(422, "invalid_arguments", "the arguments do not fit the process parameters", problems)
        inst = ProcessInstance(application_id=app.id, revision_id=app.current_revision_id, process=f"{mod.key}.{proc.key}", status="running",
                               state={}, version=1, initiator=initiator[:64], started_at=self.clock())
        self.session.add(inst)
        self.session.flush()
        st = Machine(model, mod, proc, _Hooks(self, inst, actor or initiator), self.clock).start(args, initiator)
        self._save(inst, st)
        self._audit(app, "process.started", initiator, {"process": inst.process, "instance": str(inst.id)})
        telemetry.applog(app.id, logging.INFO, "process", "process started",
                         event_type="process.started", process=inst.process, outcome=st.status)
        return inst

    # -- the inbox -------------------------------------------------------------------------------

    @staticmethod
    def visible(task: UserTask, user_id: str, roles: frozenset[str]) -> bool:
        return task.assignee_user == user_id or (task.assignee_user is None and task.assignee_role in roles)

    def _open_task(self, task: UserTask) -> None:
        if task.status != "open":
            raise ServiceError(409, "task_closed", "this task is already " + task.status)

    def claim(self, task: UserTask, user_id: str, roles: frozenset[str]) -> None:
        self.session.refresh(task)
        self._open_task(task)
        if task.assignee_user is not None:
            if task.assignee_user == user_id:
                return
            raise ServiceError(409, "already_claimed", "someone else has taken this task")
        if task.assignee_role not in roles:
            raise ServiceError(403, "forbidden", "this task is not offered to your roles")
        # compare-and-set: of two people claiming at once, exactly one wins
        won = self.session.execute(update(UserTask).where(UserTask.id == task.id, UserTask.assignee_user.is_(None), UserTask.status == "open")
                                   .values(assignee_user=user_id)).rowcount
        self.session.expire(task)
        if not won:
            raise ServiceError(409, "already_claimed", "someone else has taken this task")
        self._event(task, "task_claimed", user_id, {})

    def release(self, task: UserTask, user_id: str) -> None:
        self.session.refresh(task)
        self._open_task(task)
        if task.assignee_user != user_id:
            raise ServiceError(403, "forbidden", "only the person holding a task can release it")
        if task.assignee_role is None:
            raise ServiceError(409, "not_releasable", "this task was assigned to you personally and has no role to return to")
        task.assignee_user = None
        self._event(task, "task_released", user_id, {})

    def delegate(self, task: UserTask, user_id: str, roles: frozenset[str], target_username: str, app_id: uuid.UUID) -> None:
        self.session.refresh(task)
        self._open_task(task)
        if not self.visible(task, user_id, roles):
            raise ServiceError(403, "forbidden", "this task is not yours")
        target = self.session.scalar(select(AppUser).where(AppUser.application_id == app_id, AppUser.username == target_username.strip().lower()))
        if target is None or target.status != "active":
            raise ServiceError(404, "unknown_user", "no active user with that name")
        if str(target.id) == user_id:
            raise ServiceError(422, "invalid_delegate", "choose someone else")
        if task.assignee_role is not None:
            held = set(self.session.scalars(select(AppUserRole.role_key).where(AppUserRole.app_user_id == target.id)))
            if task.assignee_role not in held:
                raise ServiceError(422, "invalid_delegate", "that person does not hold the role this task is offered to")
        task.assignee_user, task.delegated_by = str(target.id), user_id
        self._event(task, "task_delegated", user_id, {"to": str(target.id)})

    def complete(self, task: UserTask, user_id: str, roles: frozenset[str], outcome: str, result: dict[str, Any]) -> ProcessInstance:
        with telemetry.span("process.task.complete", outcome=outcome[:31]):
            return self._complete(task, user_id, roles, outcome, result)

    def _complete(self, task: UserTask, user_id: str, roles: frozenset[str], outcome: str, result: dict[str, Any]) -> ProcessInstance:
        self.session.refresh(task)  # act on what is stored now, not on what this session saw earlier
        self._open_task(task)
        if not self.visible(task, user_id, roles):
            raise ServiceError(403, "forbidden", "this task is not yours")
        if outcome not in task.outcomes:
            raise ServiceError(422, "invalid_outcome", "not an outcome of this task", {"outcomes": task.outcomes})
        values = self._form_values(task, result)
        inst = self.session.get(ProcessInstance, task.instance_id)
        assert inst is not None
        if inst.status != "running":
            raise ServiceError(409, "instance_closed", f"the process is {inst.status}")
        st = State.from_json(inst.state)
        machine = self._machine(inst, user_id)
        task.status, task.completed_at, task.completed_by, task.outcome, task.result = "completed", self.clock(), user_id, outcome, values
        if task.assignee_user is None:
            task.assignee_user = user_id
        self._event(task, "task_completed", user_id, {"outcome": outcome})  # before the steps it triggers, so the trail reads in order
        try:
            machine.complete_task(st, task.token_id, outcome, values, str(task.id))
        except ProcessError as e:
            raise ServiceError(409, "task_not_waiting", str(e)) from e
        self._save(inst, st)
        app = self.session.get(Application, inst.application_id)
        self._audit(app, "task.completed", user_id, {"task": str(task.id), "outcome": outcome, "instance": str(inst.id)})
        return inst

    @staticmethod
    def _form_values(task: UserTask, result: dict[str, Any]) -> dict[str, Any]:
        fields = {f["name"]: f for f in task.form}
        problems = [{"field": k, "message": "unknown field"} for k in result if k not in fields]
        out: dict[str, Any] = {}
        for name, f in fields.items():
            if name not in result or result[name] is None or result[name] == "":
                if f.get("required", True):
                    problems.append({"field": name, "message": "is required"})
                continue
            v, t = result[name], f.get("type", "string")
            if t == "decimal" and isinstance(v, (int, float, str)) and not isinstance(v, bool):
                try:
                    d = Decimal(str(v))
                    if not d.is_finite():
                        raise ValueError
                    v = float(d) if "." in str(v) or "e" in str(v).lower() else int(d)
                except (ValueError, ArithmeticError):
                    problems.append({"field": name, "message": "must be a number"})
                    continue
            elif not literal_fits(t, v) or (t == "string" and (not isinstance(v, str) or len(v) > 2000)):
                problems.append({"field": name, "message": f"must be of type {t}"})
                continue
            out[name] = v
        if problems:
            raise ServiceError(422, "invalid_result", "the form is incomplete or invalid", problems)
        return out

    # -- FR-112: process versioning ---------------------------------------------------------------

    def stale_instances(self, app: Application) -> list[ProcessInstance]:
        """Running instances whose revision differs from the application's current revision (FR-112)."""
        if app.current_revision_id is None:
            return []
        return list(self.session.scalars(
            select(ProcessInstance).where(
                ProcessInstance.application_id == app.id,
                ProcessInstance.status == "running",
                ProcessInstance.revision_id != app.current_revision_id,
            )
        ))

    def set_migration_policy(self, inst: ProcessInstance, policy: str, actor: str) -> None:
        """Change the migration policy of a running instance (FR-112). Allowed values: 'pin', 'migrate'."""
        if policy not in ("pin", "migrate"):
            raise ServiceError(422, "invalid_policy", "migration_policy must be 'pin' or 'migrate'")
        if inst.status not in ("running", "failed"):
            raise ServiceError(409, "instance_closed", f"cannot change policy on a {inst.status} instance")
        self.session.execute(
            update(ProcessInstance).where(ProcessInstance.id == inst.id).values(migration_policy=policy)
        )
        self.session.add(ProcessEvent(instance_id=inst.id, at=self.clock(), type="policy_changed", step_id=None,
                                      actor=actor, data={"migration_policy": policy}))
        self.session.expire(inst)

    def migrate_to_current(self, inst: ProcessInstance, app: Application, actor: str) -> None:
        """Move a stale running instance to the application's current revision (FR-112).

        The instance's token positions are preserved. Steps that no longer exist in the new
        definition will fail when next executed; the instance may then be retried or cancelled.
        Only allowed when migration_policy='migrate'.
        """
        if inst.migration_policy == "pin":
            raise ServiceError(409, "policy_pin", "update migration_policy to 'migrate' before migrating this instance")
        if app.current_revision_id is None:
            raise ServiceError(409, "no_revision", "the application has no current revision")
        if inst.revision_id == app.current_revision_id:
            return  # already current
        if inst.status not in ("running", "failed"):
            raise ServiceError(409, "instance_closed", f"cannot migrate a {inst.status} instance")
        new_model = self._model(app.current_revision_id)
        if resolve_definition(new_model, inst.process) is None:
            raise ServiceError(409, "process_removed", "the process no longer exists in the current revision")
        from_rev = str(inst.revision_id)
        won = self.session.execute(
            update(ProcessInstance).where(ProcessInstance.id == inst.id, ProcessInstance.version == inst.version)
            .values(revision_id=app.current_revision_id, version=ProcessInstance.version + 1)
        ).rowcount
        if not won:
            raise ServiceError(409, "conflict", "the process was changed by someone else; reload and try again")
        self.session.add(ProcessEvent(instance_id=inst.id, at=self.clock(), type="version_migrated", step_id=None,
                                      actor=actor, data={"from_revision": from_rev, "to_revision": str(app.current_revision_id)}))
        self.session.expire(inst)

    # -- administration --------------------------------------------------------------------------

    def cancel(self, inst: ProcessInstance, actor: str) -> None:
        if inst.status not in ("running", "failed"):
            raise ServiceError(409, "instance_closed", f"the process is already {inst.status}")
        st = State.from_json(inst.state)
        st.status, st.tokens, st.error = "cancelled", [], None
        for task in self.session.scalars(select(UserTask).where(UserTask.instance_id == inst.id, UserTask.status == "open")):
            task.status = "cancelled"
        self.session.add(ProcessEvent(instance_id=inst.id, at=self.clock(), type="cancelled", step_id=None, actor=actor, data={}))
        self._save(inst, st)

    def retry(self, inst: ProcessInstance, actor: str) -> None:
        if inst.status != "failed":
            raise ServiceError(409, "not_failed", "only a failed process can be retried")
        st = State.from_json(inst.state)
        failed = next((t for t in st.tokens if t.status == "failed"), None)
        if failed is None:
            raise ServiceError(409, "not_failed", "nothing to retry")
        try:
            self._machine(inst, actor).retry(st, failed.id)
        except ProcessError as e:
            raise ServiceError(409, "not_failed", str(e)) from e
        self._save(inst, st)

    def signal(self, inst: ProcessInstance, message: str, data: dict[str, Any], actor: str) -> None:
        """Deliver an external message to any catch_message steps waiting for it (FR-110)."""
        if inst.status != "running":
            raise ServiceError(409, "instance_closed", f"the process is {inst.status}")
        st = State.from_json(inst.state)
        try:
            self._machine(inst, actor).signal(st, message, data)
        except ProcessError as e:
            raise ServiceError(409, "no_waiter", str(e)) from e
        self._save(inst, st)

    # -- time ------------------------------------------------------------------------------------

    def tick(self) -> dict[str, int]:
        """Fire due timers and escalate overdue tasks. Safe to run from several workers: updates are compare-and-set."""
        with telemetry.span("process.tick") as sp:
            out = self._tick()
            sp.set_attributes({"process.timers_fired": out["timers"], "process.escalations": out["escalations"]})
        return out

    def _tick(self) -> dict[str, int]:
        now = self.clock()
        fired = escalated = 0
        due = self.session.scalars(select(ProcessInstance).where(ProcessInstance.status == "running", ProcessInstance.next_wake_at <= now)).all()
        for inst in due:
            st = State.from_json(inst.state)
            machine = self._machine(inst, "system:timer")
            try:
                for tok in [t for t in st.tokens if t.status == "waiting_timer" and t.wake_at and datetime.fromisoformat(t.wake_at) <= now]:
                    machine.fire_timer(st, tok.id)
                    fired += 1
                self._save(inst, st)
                self.session.commit()
            except ServiceError:
                self.session.rollback()  # somebody else got there first
        overdue = self.session.scalars(select(UserTask).where(UserTask.status == "open", UserTask.escalated_at.is_(None),
                                                              UserTask.escalate_to_role.is_not(None), UserTask.escalate_at <= now)).all()
        for task in overdue:
            won = self.session.execute(update(UserTask).where(UserTask.id == task.id, UserTask.status == "open", UserTask.escalated_at.is_(None))
                                       .values(assignee_role=task.escalate_to_role, assignee_user=None, escalated_at=now)).rowcount
            if won:
                escalated += 1
                self.session.expire(task)
                self._event(task, "task_escalated", "system:escalation", {"to_role": task.escalate_to_role})
        self.session.commit()
        return {"timers": fired, "escalations": escalated}

    # -- system tasks and helpers ------------------------------------------------------------------

    def run_system_flow(self, inst: ProcessInstance, flow: str, args: dict[str, Any]) -> str | None:
        app = self.session.get(Application, inst.application_id)
        assert app is not None and app.current_revision_id is not None
        rev = self.session.get(AppRevision, app.current_revision_id)
        assert rev is not None
        model = to_model(rev)
        found = resolve_definition(self._model(inst.revision_id), inst.process)
        role = found[1].run_as_role if found else None
        project = self.session.get(Project, app.project_id)
        try:
            data = self.preview.open(app.id, model, rev.model_hash, audit=self.audit, organization_id=project.organization_id if project else None,
                                     hooks=FlowHooks(model), integrations=self.integrations(app.id) if self.integrations else None)
        except Exception as e:  # noqa: BLE001 - preview not built / stale: the step fails, an admin can retry
            return f"the application's data is not available ({type(e).__name__})"
        if role is None:
            return "the process has no run_as_role"
        result = FlowRunner(data.model, data).run(flow, args, RuntimeUser(f"system:process:{inst.process}", frozenset({role})))
        return None if result.status == "succeeded" else (result.error or "the flow failed")

    def _event(self, task: UserTask, type_: str, actor: str | None, data: dict[str, Any]) -> None:
        self.session.add(ProcessEvent(instance_id=task.instance_id, at=self.clock(), type=type_, step_id=task.step_id, actor=actor,
                                      data={"task": str(task.id), **data}))

    def _audit(self, app: Application | None, action: str, actor: str, meta: dict[str, Any]) -> None:
        project = self.session.get(Project, app.project_id) if app else None
        self.session.add(AuditEvent(organization_id=project.organization_id if project else None, actor_id=None, plane="application", channel="api",
                                    action=action, target_type="process_instance", target_id=meta.get("instance"),
                                    meta={"application_id": str(app.id) if app else None, "actor": actor, **meta}))
