"""Minimal Prometheus-format metrics (FR-045) without an extra dependency.

Labels are limited to the HTTP method, the *route template* (never the raw path, so ids cannot blow
up the number of series) and the status class.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict

BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)


def _esc(v: str) -> str:
    return v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


class Metrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.started = time.time()
        self.requests: dict[tuple[str, str, str], int] = defaultdict(int)
        self.hist: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0] * (len(BUCKETS) + 1))
        self.sum: dict[tuple[str, str], float] = defaultdict(float)

    def observe(self, method: str, route: str, status: int, seconds: float) -> None:
        with self._lock:
            self.requests[(method, route, f"{status // 100}xx")] += 1
            key = (method, route)
            counts = self.hist[key]
            for i, b in enumerate(BUCKETS):
                if seconds <= b:
                    counts[i] += 1
                    break
            else:
                counts[-1] += 1  # +Inf
            self.sum[key] += seconds

    def render(self, version: str, gauges: dict[str, float]) -> str:
        out = ["# HELP orchivio_build_info Build information.", "# TYPE orchivio_build_info gauge",
               f'orchivio_build_info{{version="{_esc(version)}"}} 1',
               "# HELP orchivio_uptime_seconds Seconds since the process started.", "# TYPE orchivio_uptime_seconds gauge",
               f"orchivio_uptime_seconds {time.time() - self.started:.0f}"]
        for name, value in sorted(gauges.items()):
            out += [f"# TYPE {name} gauge", f"{name} {value:g}"]
        with self._lock:
            out += ["# HELP orchivio_http_requests_total HTTP requests by method, route template and status class.",
                    "# TYPE orchivio_http_requests_total counter"]
            for (m, r, s), n in sorted(self.requests.items()):
                out.append(f'orchivio_http_requests_total{{method="{m}",route="{_esc(r)}",status="{s}"}} {n}')
            out += ["# HELP orchivio_http_request_duration_seconds Request latency.",
                    "# TYPE orchivio_http_request_duration_seconds histogram"]
            for (m, r), counts in sorted(self.hist.items()):
                cumulative = 0
                for b, c in zip(BUCKETS, counts, strict=False):
                    cumulative += c
                    out.append(f'orchivio_http_request_duration_seconds_bucket{{method="{m}",route="{_esc(r)}",le="{b:g}"}} {cumulative}')
                cumulative += counts[-1]
                out.append(f'orchivio_http_request_duration_seconds_bucket{{method="{m}",route="{_esc(r)}",le="+Inf"}} {cumulative}')
                out.append(f'orchivio_http_request_duration_seconds_sum{{method="{m}",route="{_esc(r)}"}} {self.sum[(m, r)]:.6f}')
                out.append(f'orchivio_http_request_duration_seconds_count{{method="{m}",route="{_esc(r)}"}} {cumulative}')
        return "\n".join(out) + "\n"
