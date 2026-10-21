"""Plans cron occurrences into the queue (FR-055).

Single execution across nodes comes from two independent guards: the planner advances
``schedule_state.last_planned_at`` by compare-and-set (only the node that wins plans that window), and the
queue's unique ``(application, flow, scheduled_for)`` constraint rejects a duplicate occurrence anyway.

Missed-run policy (``schedule.missed_runs``), applied to the occurrences in the window since the last plan:

* ``skip``: only the latest occurrence is run, and only if it is at most ``grace_seconds`` old; anything older
  is dropped (a node that was down does not replay the past).
* ``run_once``: the latest occurrence is run once however late it is, and earlier ones are collapsed into it.

The look-back is capped at 24 hours, so re-enabling an old schedule never replays history. The first time a
flow is seen, planning starts from now.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from orchivio.db import to_model
from orchivio.db.models import Application, AppRevision, Environment, ScheduleState
from orchivio.model import AppModel
from orchivio.model.cron import CronError, parse_cron
from orchivio.model.schema import ScheduleDef

from . import queue

MAX_LOOKBACK = timedelta(hours=24)


def scheduled_flows(model: AppModel) -> list[tuple[str, ScheduleDef]]:
    return [(f"{m.key}.{f.key}", f.schedule) for m in model.modules for f in m.flows if f.schedule is not None]


class Planner:
    def __init__(self, grace_seconds: int = 120) -> None:
        self.grace = timedelta(seconds=grace_seconds)
        self._cache: dict[uuid.UUID, list[tuple[str, ScheduleDef]]] = {}

    def _schedules(self, session: Session, revision_id: uuid.UUID) -> list[tuple[str, ScheduleDef]]:
        if revision_id not in self._cache:
            rev = session.get(AppRevision, revision_id)
            if len(self._cache) > 256:
                self._cache.clear()
            self._cache[revision_id] = scheduled_flows(to_model(rev)) if rev is not None else []
        return self._cache[revision_id]

    def plan(self, session: Session, now: datetime | None = None) -> list[uuid.UUID]:
        """One planning pass over every application; returns the ids of the runs it queued."""
        now = now or queue.utcnow()
        queued: list[uuid.UUID] = []
        apps = session.execute(select(Application.id, Application.current_revision_id)
                               .where(Application.current_revision_id.is_not(None))).all()
        for app_id, rev_id in apps:
            for flow, sched in self._schedules(session, rev_id):
                queued += self._plan_flow(session, app_id, flow, sched, now)
        session.commit()
        return queued

    def _plan_flow(self, session: Session, app_id: uuid.UUID, flow: str, sched: ScheduleDef, now: datetime) -> list[uuid.UUID]:
        state = session.scalar(select(ScheduleState).where(ScheduleState.application_id == app_id, ScheduleState.flow == flow))
        if state is None:
            try:
                with session.begin_nested():
                    session.add(ScheduleState(application_id=app_id, flow=flow, last_planned_at=now))
            except IntegrityError:  # another node saw the flow first
                pass
            return []
        last = queue.aware(state.last_planned_at)
        if last >= now:
            return []
        won = session.execute(update(ScheduleState)
                              .where(ScheduleState.id == state.id, ScheduleState.last_planned_at == state.last_planned_at)
                              .values(last_planned_at=now)).rowcount
        if not won or not sched.enabled:
            return []
        try:
            cron = parse_cron(sched.cron)
        except CronError:
            return []  # the validator rejects such models; a stored one is ignored rather than crashing the loop
        due = cron.between(max(last, now - MAX_LOOKBACK), now)
        if not due:
            return []
        latest = due[-1]
        if sched.missed_runs == "skip" and now - latest > self.grace:
            return []
        env_id = session.scalar(select(Environment.id).where(Environment.application_id == app_id)
                               .order_by(Environment.stage_order.desc()).limit(1))
        job = queue.enqueue(session, application_id=app_id, flow=flow, args=dict(sched.args), run_as_role=sched.run_as_role,
                            trigger="schedule", scheduled_for=latest, max_attempts=sched.max_attempts,
                            backoff_seconds=sched.backoff_seconds, environment_id=env_id, at=now)
        return [job] if job else []
