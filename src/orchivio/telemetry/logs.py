"""OTLP/HTTP (JSON) export of the platform's own log lines (FR-123).

Only the ``orchivio`` logger tree is exported. A record carries level, logger name, formatted message, the request id, trace
and span ids, and the scalar ``fields`` call sites attach. Exception *types* are sent, never tracebacks (they can quote data).
Delivery is best effort on a background thread with a bounded queue: a slow or dead collector drops log lines, it never
blocks or breaks a request. Uses only the standard library; the endpoint is operator configuration, not user input.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any

from orchivio import telemetry

_SEVERITY = {"DEBUG": 5, "INFO": 9, "WARNING": 13, "ERROR": 17, "CRITICAL": 21}
MAX_QUEUE = 2000
MAX_BATCH = 100
MAX_VALUE = 1000


def _attr(key: str, value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        v: dict[str, Any] = {"boolValue": value}
    elif isinstance(value, int):
        v = {"intValue": str(value)}
    elif isinstance(value, float):
        v = {"doubleValue": value}
    else:
        v = {"stringValue": str(value)[:MAX_VALUE]}
    return {"key": key, "value": v}


class OtlpLogHandler(logging.Handler):
    def __init__(self, url: str, resource_attrs: Mapping[str, str], *, post: Callable[[str, bytes], None] | None = None,
                 start: bool = True) -> None:
        super().__init__(level=logging.INFO)
        self.url = url
        self.resource = [_attr(k, v) for k, v in resource_attrs.items()]
        self._post = post or self._http_post
        self._q: queue.Queue[dict[str, Any]] = queue.Queue(MAX_QUEUE)
        self.dropped = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        if start:
            self._thread = threading.Thread(target=self._run, name="orchivio-otlp-logs", daemon=True)
            self._thread.start()

    # -- producing -------------------------------------------------------------------------------------------------
    def emit(self, record: logging.LogRecord) -> None:
        try:
            item = self._convert(record)
            self._q.put_nowait(item)
        except queue.Full:
            self.dropped += 1
        except Exception:  # noqa: BLE001 - logging must never raise into the caller
            self.dropped += 1

    def _convert(self, record: logging.LogRecord) -> dict[str, Any]:
        attrs = [_attr("logger", record.name), _attr("request_id", telemetry.request_id_var.get())]
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            attrs += [_attr(str(k), v) for k, v in fields.items() if isinstance(v, (str, int, float, bool))]
        if record.exc_info and record.exc_info[0] is not None:
            attrs.append(_attr("exception.type", record.exc_info[0].__name__))
        item: dict[str, Any] = {
            "timeUnixNano": str(int(record.created * 1_000_000_000)),
            "severityNumber": _SEVERITY.get(record.levelname, 9),
            "severityText": record.levelname,
            "body": {"stringValue": record.getMessage()[:MAX_VALUE]},
            "attributes": attrs,
        }
        ids = telemetry.current_ids()
        if ids is not None:
            item["traceId"], item["spanId"] = ids
        return item

    # -- consuming -------------------------------------------------------------------------------------------------
    def payload(self, records: list[dict[str, Any]]) -> bytes:
        return json.dumps({"resourceLogs": [{"resource": {"attributes": self.resource},
                                             "scopeLogs": [{"scope": {"name": "orchivio"}, "logRecords": records}]}]}).encode()

    def flush(self) -> None:
        batch: list[dict[str, Any]] = []
        while len(batch) < MAX_BATCH:
            try:
                item = self._q.get_nowait()
            except queue.Empty:
                break
            batch.append(item)
        if batch:
            try:
                self._post(self.url, self.payload(batch))
            except Exception:  # noqa: BLE001 - best effort; the lines are dropped
                self.dropped += len(batch)

    def _run(self) -> None:
        while not self._stop.wait(2):
            self.flush()

    def _http_post(self, url: str, body: bytes) -> None:
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=5):  # noqa: S310 - operator-configured http(s) endpoint
            pass

    def close(self) -> None:
        if self._thread is not None:
            self._stop.set()
            self._thread.join(timeout=3)
            self._thread = None
        self.flush()
        logging.getLogger("orchivio").removeHandler(self)
        super().close()


def attach_log_export(url: str, *, resource_attrs: Mapping[str, str], post: Callable[[str, bytes], None] | None = None,
                      start: bool = True) -> OtlpLogHandler:
    handler = OtlpLogHandler(url, resource_attrs, post=post, start=start)
    logging.getLogger("orchivio").addHandler(handler)
    return handler
