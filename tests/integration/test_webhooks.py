"""Business events through the transactional outbox to signed, SSRF-guarded webhooks."""

import json
import threading
import uuid
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from sqlalchemy import select
from test_tenancy import add_member, make_org

from orchivio.db.models import AuditEvent, WebhookDelivery
from orchivio.integrations import (
    DestinationError,
    Dispatcher,
    PinnedTransport,
    Policy,
    Response,
    verify_signature,
)
from orchivio.runtime import AppUser, outbox

SECRET = "whsec_test_secret_value"
T0 = datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)
URL = "https://hooks.example.com/orchivio"


class FakeTransport:
    def __init__(self):
        self.sent = []
        self.script = []  # responses or exceptions, consumed in order; default 200

    def post(self, url, headers, body):
        self.sent.append((url, headers, body))
        step = self.script.pop(0) if self.script else Response(200)
        if isinstance(step, Exception):
            raise step
        return step


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t

    def advance(self, **kw):
        self.t += timedelta(**kw)


@pytest.fixture(autouse=True)
def secret_env(monkeypatch):
    monkeypatch.setenv("ORCHIVIO_TEST_HOOK_SECRET", SECRET)


@pytest.fixture
def world(new_api, raw_model, app):
    owner = new_api("owner@example.com")
    org = make_org(owner)
    proj = owner.post(f"/organizations/{org['id']}/projects", json={"name": "Inv", "slug": "inventory"}).json()
    editor = new_api("editor@example.com")
    add_member(owner, org, "editor@example.com", "editor")
    outsider = new_api("outsider@example.com")
    a = owner.post(f"/projects/{proj['id']}/applications", json={"key": "inventory", "name": "Inv", "model": raw_model}).json()
    owner.post(f"/applications/{a['id']}/preview/reset", json={})
    return a, {"owner": owner, "editor": editor, "outsider": outsider}


def make_dispatcher(app, transport, clock=None, **kw):
    return Dispatcher(app.state.session_factory, app.state.preview_store.engine, transport, clock=clock or Clock(), **kw)


def hook(owner, a, **over):
    body = {"name": "main", "url": URL, "secret_ref": "env:ORCHIVIO_TEST_HOOK_SECRET", "events": ["core.Product.*"], **over}
    r = owner.post(f"/applications/{a['id']}/webhooks", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def acting(user="a", roles="admin"):
    return {"X-Preview-User": user, "X-Preview-Roles": roles}


def create_product(owner, a, sku="w-1", **over):
    body = {"name": "Widget", "sku": sku, "quantity": 3, "reorder_level": 5, **over}
    r = owner.post(f"/applications/{a['id']}/preview/data/core.Product", headers=acting(), json=body)
    assert r.status_code == 201, r.text
    return r.json()


def deliveries(app, **where):
    with app.state.session_factory() as s:
        q = select(WebhookDelivery).order_by(WebhookDelivery.created_at, WebhookDelivery.id)
        for k, v in where.items():
            q = q.where(getattr(WebhookDelivery, k) == v)
        return list(s.scalars(q))


# --- subscriptions -----------------------------------------------------------------------------


def test_subscription_management_and_validation(world):
    a, p = world
    o = p["owner"]
    w = hook(o, a)
    assert w["status"] == "active" and "secret" not in json.dumps(w).replace("secret_ref", "")
    base = f"/applications/{a['id']}/webhooks"
    bad = lambda **kw: o.post(base, json={"name": "x", "url": URL, "secret_ref": "env:ORCHIVIO_TEST_HOOK_SECRET", "events": ["*"], **kw})  # noqa: E731
    assert bad(name="main").json()["error"]["code"] == "name_taken"
    for url in ("http://hooks.example.com/", "https://localhost/", "https://169.254.169.254/latest", "https://u:p@hooks.example.com/",
                "https://10.1.2.3/", "file:///etc/passwd"):
        r = bad(url=url)
        assert r.status_code == 422 and r.json()["error"]["code"] == "destination_refused", url
    assert bad(secret_ref="vault:kv/x").json()["error"]["code"] == "invalid_secret_ref"
    assert bad(secret_ref="env:NOT_SET_ANYWHERE_123").json()["error"]["code"] == "secret_unavailable"
    assert bad(events=["core Product"]).json()["error"]["code"] == "invalid_event_pattern"
    assert bad(events=[]).status_code == 422
    assert bad(fields=["Bad Field"]).json()["error"]["code"] == "invalid_field"
    assert bad(surprise=1).status_code == 422
    assert p["editor"].get(base).status_code == 403  # URLs can carry tokens: admin only
    assert p["editor"].post(base, json={}).status_code in (403, 422)
    assert p["outsider"].get(base).status_code == 404
    assert [x["name"] for x in o.get(base).json()["items"]] == ["main"]
    patched = o.patch(f"/webhooks/{w['id']}", json={"events": ["core.Product.created"], "fields": ["name"]}).json()
    assert patched["events"] == ["core.Product.created"] and patched["fields"] == ["name"]
    assert o.patch(f"/webhooks/{w['id']}", json={"clear_fields": True}).json()["fields"] is None
    assert o.patch(f"/webhooks/{w['id']}", json={"url": "https://127.0.0.1/"}).status_code == 422
    assert o.delete(f"/webhooks/{w['id']}").status_code == 204 and o.get(f"/webhooks/{w['id']}").status_code == 404


def test_the_url_is_not_written_to_the_audit_log(world, app):
    a, p = world
    hook(p["owner"], a, url="https://hooks.example.com/orchivio?token=SUPERSECRETTOKEN")
    with app.state.session_factory() as s:
        events = list(s.scalars(select(AuditEvent)))
    assert "webhook.created" in {e.action for e in events}
    assert "SUPERSECRETTOKEN" not in " ".join(json.dumps(e.meta) for e in events)


# --- the outbox is transactional ---------------------------------------------------------------


def test_events_exist_only_for_committed_changes(world, app):
    a, p = world
    engine = app.state.preview_store.engine
    bad = p["owner"].post(f"/applications/{a['id']}/preview/data/core.Product", headers=acting(), json={"name": "x"})
    assert bad.status_code == 422 and outbox.pending(engine) == []  # rejected writes leave nothing behind
    create_product(p["owner"], a)
    pending = outbox.pending(engine)
    assert [e["type"] for e in pending] == ["core.Product.created"]

    from orchivio.db import to_model
    from orchivio.db.models import Application, AppRevision
    with app.state.session_factory() as s:
        application = s.get(Application, uuid.UUID(a["id"]))
        rev = s.get(AppRevision, application.current_revision_id)
        model, mhash = to_model(rev), rev.model_hash
    data = app.state.preview_store.open(uuid.UUID(a["id"]), model, mhash)
    user = AppUser("t", frozenset({"admin"}))
    with pytest.raises(RuntimeError):
        with data.transaction():
            data.create("core.Product", user, {"name": "R", "sku": "rolled-back", "quantity": 1, "reorder_level": 1})
            raise RuntimeError("the flow failed")
    assert [e["type"] for e in outbox.pending(engine)] == ["core.Product.created"]  # still just the first one


def test_created_updated_deleted_events_and_their_content(world, app):
    a, p = world
    o = p["owner"]
    row = create_product(o, a)
    o.patch(f"/applications/{a['id']}/preview/data/core.Product/{row['id']}", headers=acting(), json={"quantity": 9})
    o.delete(f"/applications/{a['id']}/preview/data/core.Product/{row['id']}", headers=acting())
    events = outbox.pending(app.state.preview_store.engine)
    assert [e["type"] for e in events] == ["core.Product.created", "core.Product.updated", "core.Product.deleted"]
    assert events[1]["payload"]["changed"] == ["quantity"] and events[1]["payload"]["record"]["quantity"] == 9
    assert events[2]["payload"]["record"]["sku"] == "w-1" and events[2]["subject"] == f"core.Product/{row['id']}"
    assert len({e["event_id"] for e in events}) == 3


def test_resetting_the_preview_discards_its_events(world, app):
    a, p = world
    create_product(p["owner"], a)
    p["owner"].post(f"/applications/{a['id']}/preview/reset", json={})
    assert outbox.pending(app.state.preview_store.engine) == []


# --- delivery ----------------------------------------------------------------------------------


def test_delivery_is_signed_cloudevents_json_with_only_the_chosen_fields(world, app):
    a, p = world
    hook(p["owner"], a, name="ids", events=["core.Product.created"])
    hook(p["owner"], a, name="some", events=["core.Product.*"], fields=["name", "sku"])
    hook(p["owner"], a, name="all", events=["*"], fields=["*"])
    row = create_product(p["owner"], a)
    p["owner"].patch(f"/applications/{a['id']}/preview/data/core.Product/{row['id']}", headers=acting(), json={"quantity": 9})
    t = FakeTransport()
    res = make_dispatcher(app, t).tick()
    assert (res.relayed, res.created, res.delivered) == (2, 5, 5)

    by_url = {}
    for _url, headers, body in t.sent:
        assert verify_signature(SECRET, headers["Orchivio-Signature"], body)
        ev = json.loads(body)
        assert headers["Orchivio-Event-Id"] == ev["id"] and headers["Orchivio-Event-Type"] == ev["type"]
        assert headers["Content-Type"].startswith("application/cloudevents+json")
        assert ev["specversion"] == "1.0" and ev["source"] == f"orchivio:app:{a['id']}"
        by_url.setdefault(ev["type"], []).append(ev)
    created = by_url["core.Product.created"]
    shapes = sorted((tuple(sorted(e["data"])), tuple(sorted(e["data"].get("record", {})))) for e in created)
    assert shapes == [(("entity", "id"), ()),  # no fields requested: only that something happened
                      (("entity", "id", "record"), ("created_at", "id", "name", "owner", "quantity", "reorder_level", "sku", "status")),
                      (("entity", "id", "record"), ("id", "name", "sku"))]
    updated = by_url["core.Product.updated"]
    assert len(updated) == 2 and all(e["data"]["entity"] == "core.Product" for e in updated)
    some = next(e for e in updated if set(e["data"].get("record", {})) == {"id", "name", "sku"})
    assert some["data"]["changed"] == []  # "quantity" changed but this subscriber did not ask for it


def test_pattern_matching_and_paused_subscriptions(world, app):
    a, p = world
    o = p["owner"]
    created_only = hook(o, a, name="c", events=["core.Product.created"])
    hook(o, a, name="other", events=["core.Order.*"])
    paused = hook(o, a, name="p", events=["*"])
    o.patch(f"/webhooks/{paused['id']}", json={"status": "paused"})
    row = create_product(o, a)
    o.patch(f"/applications/{a['id']}/preview/data/core.Product/{row['id']}", headers=acting(), json={"quantity": 1})
    make_dispatcher(app, FakeTransport()).tick()
    got = deliveries(app)
    assert [(d.event_type, str(d.subscription_id)) for d in got] == [("core.Product.created", created_only["id"])]


def test_relay_is_idempotent(world, app):
    a, p = world
    hook(p["owner"], a)
    create_product(p["owner"], a)
    d = make_dispatcher(app, FakeTransport())
    d.relay()
    engine = app.state.preview_store.engine
    from sqlalchemy import update
    with engine.begin() as conn:  # simulate a crash between "deliveries created" and "outbox marked relayed"
        conn.execute(update(outbox.OUTBOX).values(relayed_at=None))
    assert d.relay() == (1, 0)
    assert len(deliveries(app)) == 1


def test_retry_backoff_dead_letter_and_manual_retry(world, app):
    a, p = world
    w = hook(p["owner"], a, events=["core.Product.created"])
    create_product(p["owner"], a)
    clock, t = Clock(), FakeTransport()
    t.script = [Response(503), OSError("connection reset"), Response(502), Response(500), Response(500), Response(500)]
    d = make_dispatcher(app, t, clock, backoff_seconds=10, max_attempts=3)
    assert d.tick().retried == 1  # HTTP 503: retry in 10 s
    assert d.tick().delivered == 0 and len(t.sent) == 1
    clock.advance(seconds=10)
    assert d.tick().retried == 1  # network error: retry in 20 s
    clock.advance(seconds=19)
    assert d.tick().retried == 0 and len(t.sent) == 2
    clock.advance(seconds=1)
    assert d.tick().dead == 1  # third and last attempt
    (dead,) = deliveries(app)
    assert dead.status == "dead" and dead.attempt == 3 and dead.response_status == 502
    with app.state.session_factory() as s:
        assert s.scalar(select(AuditEvent).where(AuditEvent.action == "webhook.delivery_dead")) is not None
    assert p["owner"].get(f"/webhooks/{w['id']}/deliveries?status=dead").json()["total"] == 1
    assert p["owner"].get(f"/webhook-deliveries/{dead.id}").json()["payload"]["type"] == "core.Product.created"

    t.script = [Response(200)]
    clock.t = datetime.now(UTC)  # the API requeues at the real "now"
    assert p["owner"].post(f"/webhook-deliveries/{dead.id}/retry").json()["status"] == "queued"
    clock.advance(seconds=5)
    assert d.tick().delivered == 1
    assert p["owner"].post(f"/webhook-deliveries/{dead.id}/retry").json()["error"]["code"] == "not_dead"
    assert p["editor"].post(f"/webhook-deliveries/{dead.id}/retry").status_code == 403


def test_client_errors_are_permanent_but_timeouts_and_429_are_not(world, app):
    a, p = world
    hook(p["owner"], a, events=["core.Product.created"])
    create_product(p["owner"], a)
    create_product(p["owner"], a, sku="w-2")
    create_product(p["owner"], a, sku="w-3")
    t = FakeTransport()
    t.script = [Response(400), Response(429), Response(408)]
    res = make_dispatcher(app, t, max_attempts=5).tick()
    assert (res.dead, res.retried) == (1, 2)


def test_circuit_breaker_pauses_a_failing_subscription_until_resumed(world, app):
    a, p = world
    o = p["owner"]
    w = hook(o, a, events=["core.Product.created"])
    for i in range(3):
        create_product(o, a, sku=f"s-{i}")
    t = FakeTransport()
    t.script = [Response(500)] * 3
    d = make_dispatcher(app, t, pause_after_failures=3)
    d.tick()
    got = o.get(f"/webhooks/{w['id']}").json()
    assert got["status"] == "paused" and "3 consecutive" in got["paused_reason"]
    create_product(o, a, sku="after")
    d.tick()
    assert len(deliveries(app)) == 3  # nothing new is queued for a paused subscription
    with app.state.session_factory() as s:
        assert s.scalar(select(AuditEvent).where(AuditEvent.action == "webhook.paused")) is not None
    resumed = o.patch(f"/webhooks/{w['id']}", json={"status": "active"}).json()
    assert resumed["status"] == "active" and resumed["consecutive_failures"] == 0


def test_a_missing_secret_fails_the_attempt_without_sending(world, app, monkeypatch):
    a, p = world
    hook(p["owner"], a)
    create_product(p["owner"], a)
    monkeypatch.delenv("ORCHIVIO_TEST_HOOK_SECRET")
    t = FakeTransport()
    res = make_dispatcher(app, t).tick()
    assert res.retried == 1 and t.sent == []
    assert "not available" in deliveries(app)[0].error


def test_refused_destinations_fail_permanently_at_send_time(world, app):
    a, p = world
    hook(p["owner"], a)  # a public-looking name: fine when saved
    create_product(p["owner"], a)
    t = PinnedTransport(Policy(), resolver=lambda h, port: ["10.0.0.5"])  # ...but it resolves to a private address now
    res = make_dispatcher(app, t).tick()
    assert res.dead == 1
    assert "destination refused" in deliveries(app)[0].error and deliveries(app)[0].attempt == 1


def test_test_endpoint_sends_one_ping(world, app):
    a, p = world
    w = hook(p["owner"], a, events=["core.Order.created"])
    r = p["owner"].post(f"/webhooks/{w['id']}/test")
    assert r.status_code == 202 and r.json()["event_type"] == "orchivio.ping" and r.json()["max_attempts"] == 1
    t = FakeTransport()
    t.script = [Response(500)]
    clock = Clock()
    clock.t = datetime.now(UTC) + timedelta(seconds=5)  # the API queued it at the real "now"
    res = make_dispatcher(app, t, clock, max_attempts=6).tick()
    assert res.dead == 1  # a ping is tried once
    assert json.loads(t.sent[0][2])["type"] == "orchivio.ping"


def test_a_worker_runs_the_dispatcher_as_a_periodic_step(world, app):
    from orchivio.jobs import Worker
    a, p = world
    hook(p["owner"], a)
    create_product(p["owner"], a)
    t = FakeTransport()
    w = Worker(app.state.session_factory, app.state.preview_store, extra_steps=(make_dispatcher(app, t).tick,))
    w.tick()
    assert len(t.sent) == 1


# --- the real transport against a local receiver -----------------------------------------------


class Receiver(BaseHTTPRequestHandler):
    seen = []

    def do_POST(self):  # noqa: N802
        body = self.rfile.read(int(self.headers["Content-Length"]))
        Receiver.seen.append((self.path, dict(self.headers), body))
        self.send_response(204 if self.path == "/ok" else 302)
        self.send_header("Location", "http://127.0.0.1:1/elsewhere")
        self.end_headers()

    def log_message(self, *a):
        pass


@pytest.fixture
def receiver():
    Receiver.seen = []
    server = HTTPServer(("127.0.0.1", 0), Receiver)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1]
    server.shutdown()


def test_pinned_transport_connects_to_the_resolved_address_with_the_original_host(receiver):
    calls = []

    def resolver(host, port):
        calls.append(host)
        return ["127.0.0.1"]

    t = PinnedTransport(Policy(allow_http=True, allow_private=True), resolver=resolver)
    resp = t.post(f"http://hooks.example.test:{receiver}/ok", {"X-Test": "1"}, b'{"a":1}')
    assert resp.status == 204 and calls == ["hooks.example.test"]  # resolved exactly once, then pinned
    path, headers, body = Receiver.seen[0]
    assert path == "/ok" and body == b'{"a":1}' and headers["Host"] == f"hooks.example.test:{receiver}" and headers["X-Test"] == "1"
    redirected = t.post(f"http://hooks.example.test:{receiver}/redir", {}, b"{}")
    assert redirected.status == 302 and len(Receiver.seen) == 2  # redirects are not followed


def test_pinned_transport_refuses_private_targets_by_default(receiver):
    with pytest.raises(DestinationError):
        PinnedTransport(Policy(allow_http=True)).post(f"http://127.0.0.1:{receiver}/ok", {}, b"{}")
    with pytest.raises(DestinationError):
        PinnedTransport(Policy(), resolver=lambda h, p: ["127.0.0.1"]).post("https://hooks.example.test/ok", {}, b"{}")
    assert Receiver.seen == []


def test_end_to_end_over_a_real_socket(world, app, receiver):
    a, p = world
    app.state.settings.webhook_allow_http = True
    app.state.settings.webhook_allow_private_networks = True
    hook(p["owner"], a, url=f"http://127.0.0.1:{receiver}/ok", events=["core.Product.created"], fields=["name"])
    create_product(p["owner"], a)
    from orchivio.integrations import dispatcher_from_settings
    res = dispatcher_from_settings(app.state.settings, app.state.session_factory, app.state.preview_store.engine).tick()
    assert res.delivered == 1
    _, headers, body = Receiver.seen[0]
    assert verify_signature(SECRET, headers["Orchivio-Signature"], body)
    assert json.loads(body)["data"]["record"] == {"id": json.loads(body)["data"]["id"], "name": "Widget"}
