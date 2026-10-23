"""Login attempt throttle.

``LoginThrottle`` is in-memory and per process: adequate for a single-node deployment and
tests. ``DbLoginThrottle`` uses the application database so state is shared across replicas.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from sqlalchemy import text

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker


class LoginThrottle:
    def __init__(self, max_failures: int, window_seconds: int, clock: Callable[[], float] = time.monotonic):
        self.max_failures = max_failures
        self.window = window_seconds
        self.clock = clock
        self._failures: dict[str, deque[float]] = defaultdict(deque)

    def _prune(self, key: str) -> deque[float]:
        q = self._failures[key]
        cutoff = self.clock() - self.window
        while q and q[0] <= cutoff:
            q.popleft()
        if not q:
            self._failures.pop(key, None)
            return deque()
        return q

    def blocked(self, *keys: str) -> bool:
        return any(len(self._prune(k)) >= self.max_failures for k in keys)

    def fail(self, *keys: str) -> None:
        now = self.clock()
        for k in keys:
            self._failures[k].append(now)

    def succeed(self, key: str) -> None:
        self._failures.pop(key, None)


class DbLoginThrottle:
    """Login throttle backed by the application database; safe for multi-replica deployments."""

    def __init__(self, session_factory: "sessionmaker[Session]", max_failures: int,
                 window_seconds: int) -> None:
        self._sf = session_factory
        self.max_failures = max_failures
        self.window = window_seconds

    def _cutoff(self) -> datetime:
        return datetime.now(timezone.utc) - timedelta(seconds=self.window)

    def blocked(self, *keys: str) -> bool:
        if not keys:
            return False
        cutoff = self._cutoff()
        with self._sf() as s:
            for key in keys:
                count = s.execute(
                    text("SELECT COUNT(*) FROM login_failures WHERE key = :key AND failed_at > :cutoff"),
                    {"key": key, "cutoff": cutoff},
                ).scalar()
                if count >= self.max_failures:
                    return True
        return False

    def fail(self, *keys: str) -> None:
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(seconds=self.window)
        with self._sf() as s:
            for key in keys:
                s.execute(
                    text("INSERT INTO login_failures (key, failed_at) VALUES (:key, :at)"),
                    {"key": key, "at": now},
                )
                s.execute(
                    text("DELETE FROM login_failures WHERE key = :key AND failed_at <= :cutoff"),
                    {"key": key, "cutoff": cutoff},
                )
            s.commit()

    def succeed(self, key: str) -> None:
        with self._sf() as s:
            s.execute(text("DELETE FROM login_failures WHERE key = :key"), {"key": key})
            s.commit()
