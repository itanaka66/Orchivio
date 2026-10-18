"""OpenTelemetry traces and metrics (FR-123, docs/OPERATIONS.md §7).

Design rules, because telemetry leaves the process:

* **Optional.** Without ``opentelemetry-sdk`` installed, or with telemetry disabled, every function here is a no-op and
  costs one attribute lookup. Nothing else in the code base imports OpenTelemetry.
* **Allow-listed attributes.** Spans carry only what call sites pass explicitly, as short scalars; request bodies,
  headers, cookies, query strings, SQL, prompts, record data, URLs and secrets are never attached. Exceptions are
  recorded by type only (the message may contain data). Metric attributes are restricted per instrument to low-cardinality
  keys.
* **No global state of the OpenTelemetry API.** A private tracer and meter provider are used, so embedding applications and
  tests keep their own.
* **Incoming trace context is ignored by default** (anyone could otherwise force sampling on); enable
  ``otel_trust_incoming_context`` behind a trusted gateway.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("orchivio.telemetry")

try:  # the SDK is an optional extra
    from opentelemetry import trace
    from opentelemetry.context import Context
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import (
        BatchSpanProcessor,
        ConsoleSpanExporter,
        SimpleSpanProcessor,
    )
    from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
    from opentelemetry.trace import SpanKind, Status, StatusCode
    from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

    AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only without the extra
    AVAILABLE = False

MAX_ATTR = 200

# name -> (kind, unit, allowed attribute keys). Only these instruments exist, with only these attributes.
INSTRUMENTS: dict[str, tuple[str, str, frozenset[str]]] = {
    "orchivio.http.server.duration": ("histogram", "s", frozenset({"http.request.method", "http.route", "http.response.status_class"})),
    "orchivio.flow.runs": ("counter", "{run}", frozenset({"status"})),
    "orchivio.job.runs": ("counter", "{run}", frozenset({"outcome", "trigger"})),
    "orchivio.webhook.deliveries": ("counter", "{delivery}", frozenset({"outcome"})),
    "orchivio.process.events": ("counter", "{event}", frozenset({"type"})),
    "orchivio.integration.calls": ("counter", "{call}", frozenset({"status_class"})),
    "orchivio.integration.call.duration": ("histogram", "s", frozenset({"status_class"})),
    "orchivio.mcp.tool_calls": ("counter", "{call}", frozenset({"outcome"})),
}


@dataclass
class Telemetry:
    tracer: Any
    tracer_provider: Any
    meter_provider: Any
    instruments: dict[str, Any] = field(default_factory=dict)
    trust_incoming: bool = False

    def shutdown(self) -> None:
        for p in (self.tracer_provider, self.meter_provider):
            try:
                p.shutdown()
            except Exception:  # noqa: BLE001 - never let telemetry break shutdown
                log.exception("telemetry shutdown failed")


_active: Telemetry | None = None


def enabled() -> bool:
    return _active is not None


def setup(settings: Any, *, version: str = "0", span_exporter: Any = None, metric_reader: Any = None) -> Telemetry | None:
    """Start telemetry from ``Settings``. ``span_exporter`` / ``metric_reader`` let tests capture what would be exported."""
    global _active
    if not getattr(settings, "otel_enabled", False):
        return None
    if not AVAILABLE:
        log.warning("ORCHIVIO_OTEL_ENABLED is set but the OpenTelemetry SDK is not installed (pip install orchivio[otel])")
        return None
    resource = Resource.create({"service.name": settings.otel_service_name, "service.version": version,
                                "deployment.environment.name": settings.env})
    sampler = ParentBased(TraceIdRatioBased(max(0.0, min(1.0, settings.otel_sample_ratio))))
    tp = TracerProvider(resource=resource, sampler=sampler)
    endpoint = settings.otel_endpoint or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    readers: list[Any] = []
    if span_exporter is not None:
        tp.add_span_processor(SimpleSpanProcessor(span_exporter))
    elif settings.otel_exporter == "console":
        tp.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
    elif settings.otel_exporter == "otlp":
        if not endpoint:
            log.warning("telemetry is enabled but no OTLP endpoint is set (ORCHIVIO_OTEL_ENDPOINT or OTEL_EXPORTER_OTLP_ENDPOINT); "
                        "nothing will be exported")
        else:
            try:
                from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
                    OTLPMetricExporter,
                )
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
                from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

                base = endpoint.rstrip("/")
                tp.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{base}/v1/traces")))
                readers.append(PeriodicExportingMetricReader(OTLPMetricExporter(endpoint=f"{base}/v1/metrics"),
                                                             export_interval_millis=settings.otel_metric_interval_ms))
            except ImportError:
                log.warning("the OTLP exporter is not installed (pip install orchivio[otel])")
    if metric_reader is not None:
        readers.append(metric_reader)
    mp = MeterProvider(resource=resource, metric_readers=readers)
    meter = mp.get_meter("orchivio", version)
    instruments: dict[str, Any] = {}
    for name, (kind, unit, _keys) in INSTRUMENTS.items():
        instruments[name] = meter.create_histogram(name, unit=unit) if kind == "histogram" else meter.create_counter(name, unit=unit)
    _active = Telemetry(tp.get_tracer("orchivio", version), tp, mp, instruments, bool(settings.otel_trust_incoming_context))
    return _active


def shutdown() -> None:
    global _active
    if _active is not None:
        active, _active = _active, None
        active.shutdown()


# --- spans ------------------------------------------------------------------------------------------


def _clean(attrs: Mapping[str, Any]) -> dict[str, str | int | float | bool]:
    out: dict[str, str | int | float | bool] = {}
    for k, v in attrs.items():
        if isinstance(v, bool | int | float):
            out[k] = v
        elif isinstance(v, str):
            out[k] = v[:MAX_ATTR]
    return out


class _NoSpan:
    def set_attribute(self, key: str, value: Any) -> None: ...
    def set_attributes(self, attrs: Mapping[str, Any]) -> None: ...
    def update_name(self, name: str) -> None: ...
    def set_error(self, error: BaseException | str) -> None: ...


class _Span:
    def __init__(self, span: Any) -> None:
        self._span = span

    def set_attribute(self, key: str, value: Any) -> None:
        clean = _clean({key: value})
        if clean:
            self._span.set_attribute(key, clean[key])

    def set_attributes(self, attrs: Mapping[str, Any]) -> None:
        for k, v in _clean(attrs).items():
            self._span.set_attribute(k, v)

    def update_name(self, name: str) -> None:
        self._span.update_name(name[:MAX_ATTR])

    def set_error(self, error: BaseException | str) -> None:
        """Mark the span failed. Only the exception *type* is kept: messages can contain data."""
        what = type(error).__name__ if isinstance(error, BaseException) else str(error)[:80]
        self._span.set_status(Status(StatusCode.ERROR, what))
        self._span.set_attribute("error.type", what)


@contextmanager
def span(name: str, *, kind: str = "internal", parent: Any = None, **attrs: Any) -> Iterator[Any]:
    """A span around a block. No-op when telemetry is off. An exception escaping the block marks the span failed."""
    t = _active
    if t is None:
        yield _NoSpan()
        return
    with t.tracer.start_as_current_span(name, context=parent, kind=getattr(SpanKind, kind.upper(), SpanKind.INTERNAL),
                                        attributes=_clean(attrs), record_exception=False, set_status_on_exception=False) as s:
        wrapped = _Span(s)
        try:
            yield wrapped
        except BaseException as e:
            wrapped.set_error(e)
            raise


def extract(headers: Mapping[str, str]) -> Any:
    """The remote parent from a ``traceparent`` header, only when trusting incoming context is enabled."""
    t = _active
    if t is None or not t.trust_incoming:
        return None
    carrier = {k.lower(): v for k, v in headers.items() if k.lower() in ("traceparent", "tracestate")}
    if not carrier:
        return None
    ctx = TraceContextTextMapPropagator().extract(carrier, context=Context())
    return ctx if trace.get_current_span(ctx).get_span_context().is_valid else None


def current_ids() -> tuple[str, str] | None:
    """(trace_id, span_id) in hex of the current span, for correlating log lines."""
    if _active is None:
        return None
    ctx = trace.get_current_span().get_span_context()
    return (f"{ctx.trace_id:032x}", f"{ctx.span_id:016x}") if ctx.is_valid else None


# --- metrics ----------------------------------------------------------------------------------------


def _metric(name: str, value: float, attrs: Mapping[str, Any], method: str) -> None:
    t = _active
    spec = INSTRUMENTS.get(name)
    if t is None or spec is None:
        return
    allowed = {k: v for k, v in _clean(attrs).items() if k in spec[2]}
    getattr(t.instruments[name], method)(value, allowed)


def count(name: str, value: int = 1, **attrs: Any) -> None:
    _metric(name, value, attrs, "add")


def record(name: str, value: float, **attrs: Any) -> None:
    _metric(name, value, attrs, "record")


__all__ = ["AVAILABLE", "INSTRUMENTS", "Telemetry", "count", "current_ids", "enabled", "extract", "record", "setup", "shutdown", "span"]
