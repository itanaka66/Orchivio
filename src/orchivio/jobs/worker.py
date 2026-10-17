"""Runs queued jobs: plan due schedules, claim runs, execute flows, record the outcome.

A scheduled run executes a server flow in the application's preview runtime under a system identity
(``system:schedule:<flow>``) that holds only the schedule's ``run_as_role``, so application security applies
exactly as for a person with that role. There are no production environments yet (docs/OPERATIONS.md).
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from orchivio.db import to_model
from orchivio.db.models import Application, AppRevision, AuditEvent, JobRun, Project
from orchivio.logic import FlowHooks, FlowRunner
from orchivio.ports import AuditSink
from orchivio.runtime import AppUser, PreviewNotBuilt, PreviewStale, PreviewStore

from . import queue
from .scheduler import Planner

log = logging.getLogger("orchivio.jobs")


@dataclass
class TickResult:
    planned: int = 0
    claimed: int = 0
    succeeded: int = 0
    retried: int = 0
    dead: int = 0


class Worker:
    def __init__(self, session_factory: sessionmaker[Session], preview: PreviewStore, *, audit: AuditSink | None = None,
                 worker_id: str | None = None, lease_seconds: int = 120, per_app_limit: int = 2, batch: int = 10,
                 grace_seconds: int = 120, clock: Callable[[], datetime] = queue.utcnow,
                 extra_steps: tuple[Callable[[], Any], ...] = (),
                 integrations: Callable[[uuid.UUID], Any] | None = None) -> None:
        self.sessions, self.preview, self.audit = session_factory, preview, audit
        self.id = worker_id or f"worker-{uuid.uuid4().hex[:8]}"
        self.lease, self.per_app, self.batch, self.clock = lease_seconds, per_app_limit, batch, clock
        self.planner = Planner(grace_seconds)
        self.extra_steps = extra_steps
        self.integrations = integrations

    def tick(self) -> TickResult:
        out = TickResult()
        now = self.clock()
        with self.sessions() as s:
            out.planned = len(self.planner.plan(s, now))
        with self.sessions() as s:
            ids = [j.id for j in queue.claim(s, self.id, now, limit=self.batch, lease_seconds=self.lease,
                                             per_app_limit=self.per_app)]
            s.commit()
        out.claimed = len(ids)
        for step in self.extra_steps:
            try:
                step()
            except Exception:  # noqa: BLE001 - one failing subsystem must not stop the others
                log.exception("periodic step failed")
        for job_id in ids:
            outcome = self._execute(job_id)
            if outcome == "succeeded":
                out.succeeded += 1
            elif outcome == "retry":
                out.retried += 1
            elif outcome == "dead":
                out.dead += 1
        return out

    def run_forever(self, interval: float, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - the loop must survive database blips
                log.exception("job worker tick failed")
            stop.wait(interval)

    # -- one run ----------------------------------------------------------------------------

    def _execute(self, job_id: uuid.UUID) -> str:
        with self.sessions() as s:
            job = s.get(JobRun, job_id)
            assert job is not None
            error: str | None = None
            result: dict[str, Any] = {}
            try:
                result = self._run(s, job)
                if result["status"] != "succeeded":
                    error = result.get("error") or "flow failed"
            except (PreviewNotBuilt, PreviewStale) as e:
                error = f"{type(e).__name__}: build the preview for the current model first"
            except Exception as e:  # noqa: BLE001 - any failure is an attempt that can be retried
                log.exception("job %s crashed", job.id)
                error = f"{type(e).__name__}: {e}"
            now = self.clock()
            if error is None:
                outcome = "succeeded" if queue.succeed(s, job, self.id, result, now) else "lost"
            else:
                outcome = queue.fail(s, job, self.id, error, now)
                if outcome == "dead":
                    app = s.get(Application, job.application_id)
                    project = s.get(Project, app.project_id) if app else None
                    s.add(AuditEvent(organization_id=project.organization_id if project else None, actor_id=None,
                                     plane="platform", channel="api", action="job.dead", target_type="job_run",
                                     target_id=str(job.id),
                                     meta={"flow": job.flow, "attempts": job.attempt, "error": error[:300]}))
            s.commit()
            return outcome

    def _run(self, s: Session, job: JobRun) -> dict[str, Any]:
        app = s.get(Application, job.application_id)
        if app is None or app.current_revision_id is None:
            raise LookupError("application has no current revision")
        rev = s.get(AppRevision, app.current_revision_id)
        assert rev is not None
        model = to_model(rev)
        project = s.get(Project, app.project_id)
        job.model_hash = rev.model_hash
        data = self.preview.open(app.id, model, rev.model_hash, audit=self.audit,
                                 organization_id=project.organization_id if project else None, hooks=FlowHooks(model),
                                 integrations=self.integrations(app.id) if self.integrations else None)
        user = AppUser(f"system:{job.trigger}:{job.flow}", frozenset({job.run_as_role}))
        res = FlowRunner(data.model, data).run(job.flow, job.args, user)
        return {"status": res.status, "steps": res.steps_executed, "error": res.error, "error_step": res.error_step}
