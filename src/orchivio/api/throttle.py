"""Login attempt throttle.

In-memory and per process: adequate for a single-node deployment and tests. A shared store
(Redis, or the database) is needed before running several API replicas.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from collections.abc import Callable


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
