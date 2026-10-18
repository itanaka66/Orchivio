"""OpenTelemetry: what is traced and measured, what must never appear in it, and that it can be switched off."""

import json
import logging

import pytest
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode
from test_jobs import Clock, seed_low_stock, with_schedule
from test_jobs import ctx as jobs_ctx  # noqa: F401 - fixtures reused below
from test_jobs import make_app as make_job_app
from test_tenancy import make_org

from orchivio import telemetry
from orchivio.api.observability import JsonFormatter
from orchivio.jobs import Worker, queue


@pytest.fixture
def otel(app):
    exporter, reader = InMemorySpanExporter(), InMemoryMetricReader()
    app.state.settings.otel_enabled = True
    telemetry.setup(app.state.settings, version="test", span_exporter=exporter, metric_reader=reader)
    yield exporter, reader
    telemetry.shutdown()
    app.state.settings.otel_enabled = False


def spans(exporter, name=None):
    out = exporter.get_finished_spans()
    return [s for s in out if name is None or s.name == name or s.name.startswith(name)]


def metrics(reader):
    data = reader.get_metrics_data()
    out = {}
    for rm in (data.resource_metrics if data else []):
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                out[m.name] = list(m.data.data_points)
    return out


def everything(exporter) -> str:
    """All attribute keys and values, event and status text of all spans, as one string to search for secrets."""
    parts = []
    for s in exporter.get_finished_spans():
        parts += [s.name, json.dumps(dict(s.attributes or {}), default=str), s.status.description or ""]
        parts += [json.dumps(dict(e.attributes or {}), default=str) + e.name for e in s.events]
    return "\n".join(parts)


@pytest.fixture
def world(new_api, raw_model, app):
    owner = new_api("owner@example.com")
    org = make_org(owner)
    proj = owner.post(f"/organizations/{org['id']}/projects", json={"name": "Inv", "slug": "inventory"}).json()
    a = owner.post(f"/projects/{proj['id']}/applications", json={"key": "inventory", "name": "Inv", "model": raw_model}).json()
    owner.post(f"/applications/{a['id']}/preview/reset", json={})
    return a, owner


# --- requests ----------------------------------------------------------------------------------


def test_requests_are_server_spans_named_by_route_template(world, otel):
    a, owner = world
    exporter, reader = otel
    assert owner.get(f"/applications/{a['id']}").status_code == 200
    (s,) = [x for x in spans(exporter) if x.attributes.get("http.route", "").endswith("/applications/{app_id}")]
    assert s.kind == SpanKind.SERVER and s.name == "GET /api/v1/applications/{app_id}"
    assert s.attributes["http.request.method"] == "GET" and s.attributes["http.response.status_code"] == 200
    assert a["id"] not in s.name and a["id"] not in everything(exporter)  # ids stay out of names (cardinality) and attributes
    assert s.status.status_code != StatusCode.ERROR
    points = metrics(reader)["orchivio.http.server.duration"]
    assert any(p.attributes["http.route"].endswith("/applications/{app_id}") and p.attributes["http.response.status_class"] == "2xx" for p in points)


def test_nothing_sensitive_is_ever_recorded(world, otel, app):
    a, owner = world
    exporter, _ = otel
    owner.post("/models/validate", json={"model": {"note": "PINEAPPLE-BODY-SECRET"}}, headers={"Authorization": "Bearer orv_TOPSECRETTOKEN"})
    owner.get(f"/applications/{a['id']}?token=QUERYSECRET")
    owner.c.cookies.set("extra", "COOKIESECRET")
    owner.get("/me")
    blob = everything(exporter)
    for secret in ("PINEAPPLE-BODY-SECRET", "TOPSECRETTOKEN", "QUERYSECRET", "COOKIESECRET", "owner@example.com", "orchivio_session"):
        assert secret not in blob, secret
    for s in exporter.get_finished_spans():
        assert set(s.attributes or {}) <= {"http.request.method", "request.id", "http.route", "http.response.status_code", "flow", "flow.status",
                                           "flow.steps", "error.type", "job.flow", "job.trigger", "job.attempt", "job.outcome"}, s.attributes


def test_server_errors_mark_the_span_without_the_message(world, otel, app):
    a, owner = world
    exporter, _ = otel

    def boom():
        raise RuntimeError("CONNECTION STRING password=hunter2")

    app.add_api_route("/api/v1/_boom", boom)
    owner.c.get("/api/v1/_boom")
    (s,) = [x for x in spans(exporter) if "_boom" in x.name]
    assert s.status.status_code == StatusCode.ERROR and "hunter2" not in everything(exporter)


def test_incoming_trace_context_is_ignored_unless_trusted(world, otel, app):
    a, owner = world
    exporter, _ = otel
    tp = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
    owner.get("/me", headers={"traceparent": tp})
    server = [x for x in spans(exporter) if x.kind == SpanKind.SERVER][-1]
    assert server.parent is None and f"{server.context.trace_id:032x}" != "0af7651916cd43dd8448eb211c80319c"  # an outsider cannot choose our traces

    telemetry.shutdown()  # (this also shuts the old exporter down, so the second phase gets its own)
    app.state.settings.otel_trust_incoming_context = True
    exporter = InMemorySpanExporter()
    telemetry.setup(app.state.settings, version="test", span_exporter=exporter)
    owner.get("/me", headers={"traceparent": tp})
    server = [x for x in spans(exporter) if x.kind == SpanKind.SERVER][-1]
    assert f"{server.context.trace_id:032x}" == "0af7651916cd43dd8448eb211c80319c" and f"{server.parent.span_id:016x}" == "b7ad6b7169203331"
    exporter.clear()
    owner.get("/me", headers={"traceparent": "garbage"})
    assert spans(exporter)[-1].parent is None  # malformed context is just ignored
    app.state.settings.otel_trust_incoming_context = False


# --- operations --------------------------------------------------------------------------------


def run_flow(owner, a, **args):
    r = owner.post(f"/applications/{a['id']}/preview/flows/core.ReorderLowStock/run", json={"args": args},
                   headers={"X-Preview-User": "u", "X-Preview-Roles": "admin"})
    assert r.status_code == 200, r.text
    return r.json()


def test_flow_runs_are_child_spans_of_the_request_and_count_by_status(world, otel):
    a, owner = world
    exporter, reader = otel
    failed = run_flow(owner, a, min_gap=0)  # nothing is low on stock: the flow raises
    assert failed["status"] == "failed"
    owner.post(f"/applications/{a['id']}/preview/data/core.Product", headers={"X-Preview-User": "u", "X-Preview-Roles": "admin"},
               json={"name": "L", "sku": "low", "quantity": 1, "reorder_level": 5})
    assert run_flow(owner, a, min_gap=0)["status"] == "succeeded"
    flows = spans(exporter, "flow.run")
    assert [s.attributes["flow.status"] for s in flows] == ["failed", "succeeded"] and flows[0].attributes["flow"] == "core.ReorderLowStock"
    assert flows[0].status.status_code == StatusCode.ERROR and flows[1].status.status_code != StatusCode.ERROR
    assert "reorder level" not in everything(exporter)  # the flow's error text can hold record data
    by_id = {s.context.span_id: s for s in exporter.get_finished_spans()}
    ancestor = by_id[flows[0].parent.span_id]
    assert ancestor.kind == SpanKind.SERVER and ancestor.context.trace_id == flows[0].context.trace_id
    runs = {p.attributes["status"]: p.value for p in metrics(reader)["orchivio.flow.runs"]}
    assert runs == {"failed": 1, "succeeded": 1}


def test_background_jobs_are_traced_with_their_outcome(jobs_ctx, app, otel):  # noqa: F811
    proj, p, raw = jobs_ctx
    a = make_job_app(p["owner"], proj, with_schedule(raw))
    seed_low_stock(p["owner"], a)
    exporter, reader = otel
    clock = Clock()
    w = Worker(app.state.session_factory, app.state.preview_store, audit=app.state.audit_sink, clock=clock)
    w.tick()
    clock.advance(minutes=5)
    assert w.tick().succeeded == 1
    (job,) = spans(exporter, "job.run")
    assert job.attributes["job.outcome"] == "succeeded" and job.attributes["job.trigger"] == "schedule" and job.attributes["job.flow"] == "core.ReorderLowStock"
    assert {s.name for s in spans(exporter)} >= {"job.run", "flow.run"}
    assert any(p_.attributes == {"outcome": "succeeded", "trigger": "schedule"} for p_ in metrics(reader)["orchivio.job.runs"])
    assert queue  # the queue module is what produced the job


def test_webhook_deliveries_are_client_spans(world, otel, app, monkeypatch):
    from datetime import UTC, datetime, timedelta

    from test_webhooks import FakeTransport, create_product, hook, make_dispatcher
    a, owner = world
    exporter, reader = otel
    monkeypatch.setenv("ORCHIVIO_TEST_HOOK_SECRET", "s3cret-value")
    hook(owner, a)
    create_product(owner, a)
    t = FakeTransport()
    clock = Clock()
    clock.t = datetime.now(UTC) + timedelta(seconds=5)
    assert make_dispatcher(app, t, clock).tick().delivered == 1
    (s,) = spans(exporter, "webhook.deliver")
    assert s.kind == SpanKind.CLIENT and s.attributes["webhook.event_type"] == "core.Product.created" and s.attributes["http.response.status_code"] == 200
    assert "hooks.example.com" not in everything(exporter) and "s3cret-value" not in everything(exporter)  # no URL, no secret
    assert [p.value for p in metrics(reader)["orchivio.webhook.deliveries"]] == [1]


# --- the module itself -------------------------------------------------------------------------


def test_exceptions_are_recorded_by_type_only(otel):
    exporter, _ = otel
    with pytest.raises(ValueError):
        with telemetry.span("work"):
            raise ValueError("customer record 4711 is invalid")
    (s,) = spans(exporter, "work")
    assert s.status.status_code == StatusCode.ERROR and s.status.description == "ValueError" and s.attributes["error.type"] == "ValueError"
    assert s.events == () and "4711" not in everything(exporter)


def test_attributes_are_scalars_and_bounded(otel):
    exporter, _ = otel
    with telemetry.span("x", ok=1, flag=True, ratio=0.5, text="y" * 500, bad={"a": 1}, also_bad=[1, 2], none=None) as sp:
        sp.set_attribute("later", {"nested": 1})
        sp.set_attribute("fine", "v")
    (s,) = spans(exporter, "x")
    assert set(s.attributes) == {"ok", "flag", "ratio", "text", "fine"} and len(s.attributes["text"]) == 200


def test_metric_attributes_are_restricted_to_low_cardinality_keys(otel):
    _, reader = otel
    telemetry.count("orchivio.flow.runs", status="succeeded", user_id="42", record="secret")
    telemetry.count("orchivio.not.an.instrument", status="x")  # unknown instruments are ignored, not created on the fly
    telemetry.record("orchivio.integration.call.duration", 0.2, status_class="2xx", url="https://x.example/secret")
    got = metrics(reader)
    assert [p.attributes for p in got["orchivio.flow.runs"]] == [{"status": "succeeded"}]
    assert [p.attributes for p in got["orchivio.integration.call.duration"]] == [{"status_class": "2xx"}]
    assert "orchivio.not.an.instrument" not in got


def test_logs_carry_trace_ids_only_inside_a_span(otel, caplog):
    fmt = JsonFormatter()
    rec = logging.LogRecord("orchivio", logging.INFO, __file__, 1, "hello", None, None)
    assert "trace_id" not in json.loads(fmt.format(rec))
    with telemetry.span("logged") as _:
        data = json.loads(fmt.format(rec))
        ids = telemetry.current_ids()
    assert data["trace_id"] == ids[0] and data["span_id"] == ids[1] and len(ids[0]) == 32 and len(ids[1]) == 16


def test_sampling_ratio_zero_exports_nothing(app):
    exporter = InMemorySpanExporter()
    app.state.settings.otel_enabled, app.state.settings.otel_sample_ratio = True, 0.0
    telemetry.setup(app.state.settings, span_exporter=exporter)
    try:
        with telemetry.span("dropped"):
            pass
        assert exporter.get_finished_spans() == ()
    finally:
        telemetry.shutdown()
        app.state.settings.otel_enabled, app.state.settings.otel_sample_ratio = False, 1.0


def test_everything_is_a_noop_when_disabled(app):
    assert app.state.settings.otel_enabled is False and telemetry.enabled() is False
    assert telemetry.setup(app.state.settings) is None
    with telemetry.span("nothing", a=1) as sp:
        sp.set_attribute("k", "v")
        sp.set_error("e")
    telemetry.count("orchivio.flow.runs", status="x")
    telemetry.record("orchivio.http.server.duration", 1.0)
    assert telemetry.current_ids() is None and telemetry.extract({"traceparent": "00-" + "0" * 32 + "-" + "0" * 16 + "-00"}) is None


def test_a_missing_sdk_degrades_with_a_warning(app, monkeypatch, caplog):
    monkeypatch.setattr(telemetry, "AVAILABLE", False)
    app.state.settings.otel_enabled = True
    try:
        with caplog.at_level(logging.WARNING, logger="orchivio.telemetry"):
            assert telemetry.setup(app.state.settings) is None
        assert "pip install orchivio[otel]" in caplog.text
    finally:
        app.state.settings.otel_enabled = False


def test_enabled_without_an_endpoint_warns_and_exports_nothing(app, monkeypatch, caplog):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    app.state.settings.otel_enabled = True
    try:
        with caplog.at_level(logging.WARNING, logger="orchivio.telemetry"):
            handle = telemetry.setup(app.state.settings)
        assert handle is not None and "no OTLP endpoint" in caplog.text
    finally:
        telemetry.shutdown()
        app.state.settings.otel_enabled = False
