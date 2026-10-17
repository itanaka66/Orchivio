"""Job queue port (FR-042, DESIGN.md section 6.2).

Long-running work (AI calls, builds, tests) runs as jobs with progress, cancellation,
retry and timeout. The first production adapter is DB-backed (``ai_jobs``/``builds``);
``InMemoryJobQueue`` is the reference implementation used by tests and local runs.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in (JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED)


@dataclass
class Job:
    id: uuid.UUID
    type: str
    payload: dict[str, Any]
    status: JobStatus = JobStatus.QUEUED
    attempts: int = 0
    max_attempts: int = 1
    result: dict[str, Any] | None = None
    error: str | None = None
    progress: float = 0.0
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    cancel_requested: bool = False


class JobContext(Protocol):
    """Handed to handlers so they can report progress and honour cancellation."""

    def report(self, progress: float) -> None: ...

    @property
    def cancelled(self) -> bool: ...


Handler = Callable[[Job, JobContext], dict[str, Any]]


class JobQueue(Protocol):
    def submit(
        self, type: str, payload: dict[str, Any], *, max_attempts: int = 1, idempotency_key: str | None = None
    ) -> Job:
        """Enqueue a job. A repeated ``idempotency_key`` returns the original job."""
        ...

    def get(self, job_id: uuid.UUID) -> Job: ...

    def cancel(self, job_id: uuid.UUID) -> Job: ...

    def run_next(self) -> Job | None:
        """Run one queued job to completion (worker loop body). ``None`` if idle."""
        ...


class InMemoryJobQueue:
    def __init__(self, handlers: dict[str, Handler]) -> None:
        self._handlers = handlers
        self._jobs: dict[uuid.UUID, Job] = {}
        self._order: list[uuid.UUID] = []
        self._idem: dict[str, uuid.UUID] = {}

    def submit(
        self, type: str, payload: dict[str, Any], *, max_attempts: int = 1, idempotency_key: str | None = None
    ) -> Job:
        if type not in self._handlers:
            raise ValueError(f"no handler for job type {type!r}")  # only allowlisted job types
        if idempotency_key and idempotency_key in self._idem:
            return self._jobs[self._idem[idempotency_key]]
        job = Job(uuid.uuid4(), type, dict(payload), max_attempts=max(1, max_attempts))
        self._jobs[job.id] = job
        self._order.append(job.id)
        if idempotency_key:
            self._idem[idempotency_key] = job.id
        return job

    def get(self, job_id: uuid.UUID) -> Job:
        return self._jobs[job_id]

    def cancel(self, job_id: uuid.UUID) -> Job:
        job = self._jobs[job_id]
        if job.status.terminal:
            return job
        job.cancel_requested = True
        if job.status is JobStatus.QUEUED:
            job.status = JobStatus.CANCELLED
        return job

    def run_next(self) -> Job | None:
        job = next((self._jobs[i] for i in self._order if self._jobs[i].status is JobStatus.QUEUED), None)
        if job is None:
            return None
        queue = self

        class _Ctx:
            def report(self, progress: float) -> None:
                job.progress = max(0.0, min(1.0, progress))

            @property
            def cancelled(self) -> bool:
                return job.cancel_requested

        job.status = JobStatus.RUNNING
        job.attempts += 1
        try:
            job.result = queue._handlers[job.type](job, _Ctx())
            job.status = JobStatus.CANCELLED if job.cancel_requested else JobStatus.SUCCEEDED
            job.error = None
        except Exception as e:  # noqa: BLE001 - job failures are data, not crashes
            job.error = f"{type(e).__name__}: {e}"
            retry = job.attempts < job.max_attempts and not job.cancel_requested
            job.status = JobStatus.QUEUED if retry else JobStatus.FAILED
        return job
