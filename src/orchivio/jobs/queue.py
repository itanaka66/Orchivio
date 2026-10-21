"""Task queue over the ``job_runs`` table (FR-056).

Every state change is a compare-and-set ``UPDATE ... WHERE status = ...``, so it behaves the same on
PostgreSQL and SQLite and any number of workers can poll the same table: exactly one wins each claim.
A claim is a lease; a worker that dies leaves an expired lease that ``reclaim_expired`` turns into a failed
attempt, so work is retried (at-least-once) and never stuck.
"""

from __future__ import annotations

import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import insert, select, update
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.orm import Session

from orchivio.db.models import JobRun

MAX_ERROR = 2000


def utcnow() -> datetime:
    return datetime.now(UTC)


def aware(dt: datetime | None) -> datetime | None:
    return dt if dt is None or dt.tzinfo else dt.replace(tzinfo=UTC)


def _insert_ignoring_conflicts(session: Session, values: dict[str, Any]) -> bool:
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        stmt = postgresql.insert(JobRun).values(**values).on_conflict_do_nothing()
    elif dialect == "sqlite":
        stmt = sqlite.insert(JobRun).values(**values).on_conflict_do_nothing()
    else:  # pragma: no cover - other databases: rely on the unique constraint
        stmt = insert(JobRun).values(**values)
    return session.execute(stmt).rowcount == 1


def enqueue(session: Session, *, application_id: uuid.UUID, flow: str, args: dict[str, Any], run_as_role: str,
            trigger: str, scheduled_for: datetime | None = None, max_attempts: int = 3, backoff_seconds: int = 60,
            created_by: uuid.UUID | None = None, at: datetime | None = None, source_ref: str | None = None,
            environment_id: uuid.UUID | None = None) -> uuid.UUID | None:
    """Add a queued run. Returns its id, or ``None`` when this cron occurrence was already planned."""
    now = at or utcnow()
    job_id = uuid.uuid4()
    inserted = _insert_ignoring_conflicts(session, {
        "id": job_id, "application_id": application_id, "flow": flow, "args": args, "run_as_role": run_as_role,
        "trigger": trigger, "scheduled_for": scheduled_for, "status": "queued", "attempt": 0,
        "max_attempts": max_attempts, "backoff_seconds": backoff_seconds, "available_at": now,
        "environment_id": environment_id, "created_by": created_by, "source_ref": source_ref, "created_at": now})
    return job_id if inserted else None


def _fail_or_retry(session: Session, job: JobRun, worker: str | None, error: str, now: datetime) -> str:
    """Record a failed attempt: back off and requeue, or dead-letter after the last attempt."""
    error = error[:MAX_ERROR]
    guard = [JobRun.id == job.id, JobRun.status == "running"]
    if worker is not None:
        guard.append(JobRun.locked_by == worker)
    if job.attempt >= job.max_attempts:
        vals: dict[str, Any] = {"status": "dead", "finished_at": now}
        outcome = "dead"
    else:
        delay = min(job.backoff_seconds * 2 ** (job.attempt - 1), 24 * 3600)
        vals = {"status": "queued", "available_at": now + timedelta(seconds=delay)}
        outcome = "retry"
    won = session.execute(update(JobRun).where(*guard).values(error=error, locked_by=None, locked_until=None, **vals)).rowcount
    session.expire(job)
    return outcome if won else "lost"


def fail(session: Session, job: JobRun, worker: str, error: str, now: datetime | None = None) -> str:
    """Returns ``retry``, ``dead`` or ``lost`` (the lease had been taken over)."""
    return _fail_or_retry(session, job, worker, error, now or utcnow())


def succeed(session: Session, job: JobRun, worker: str, result: dict[str, Any], now: datetime | None = None) -> bool:
    won = session.execute(update(JobRun).where(JobRun.id == job.id, JobRun.status == "running", JobRun.locked_by == worker)
                          .values(status="succeeded", finished_at=now or utcnow(), result=result, error=None,
                                  locked_by=None, locked_until=None)).rowcount
    session.expire(job)
    return bool(won)


def reclaim_expired(session: Session, now: datetime) -> int:
    """Running jobs whose lease ran out (worker crashed or hung) count as a failed attempt."""
    stale = session.scalars(select(JobRun).where(JobRun.status == "running", JobRun.locked_until < now)).all()
    n = 0
    for job in stale:
        if _fail_or_retry(session, job, None, "worker lease expired before the run finished", now) != "lost":
            n += 1
    return n


def claim(session: Session, worker: str, now: datetime | None = None, *, limit: int = 10, lease_seconds: int = 120,
          per_app_limit: int = 2) -> list[JobRun]:
    """Atomically take up to ``limit`` due jobs, never more than ``per_app_limit`` running per application."""
    now = now or utcnow()
    reclaim_expired(session, now)
    running = Counter(session.scalars(select(JobRun.application_id).where(JobRun.status == "running")))
    due = session.scalars(select(JobRun).where(JobRun.status == "queued", JobRun.available_at <= now)
                          .order_by(JobRun.available_at, JobRun.id).limit(limit * 4)).all()
    taken: list[JobRun] = []
    for job in due:
        if len(taken) >= limit:
            break
        if running[job.application_id] >= per_app_limit:
            continue
        won = session.execute(update(JobRun).where(JobRun.id == job.id, JobRun.status == "queued")
                              .values(status="running", locked_by=worker, locked_until=now + timedelta(seconds=lease_seconds),
                                      attempt=JobRun.attempt + 1, started_at=now)).rowcount
        if won:
            running[job.application_id] += 1
            session.expire(job)
            taken.append(job)
    return taken


def requeue_dead(session: Session, job: JobRun, now: datetime | None = None) -> bool:
    """Give a dead-lettered run a fresh set of attempts."""
    won = session.execute(update(JobRun).where(JobRun.id == job.id, JobRun.status == "dead")
                          .values(status="queued", attempt=0, available_at=now or utcnow(), finished_at=None)).rowcount
    session.expire(job)
    return bool(won)


def cancel(session: Session, job: JobRun, now: datetime | None = None) -> bool:
    won = session.execute(update(JobRun).where(JobRun.id == job.id, JobRun.status == "queued")
                          .values(status="cancelled", finished_at=now or utcnow())).rowcount
    session.expire(job)
    return bool(won)
