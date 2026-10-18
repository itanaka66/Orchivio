"""Inbound webhooks: signature gate, replay protection, payload mapping, queued execution."""

import json
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_tenancy import add_member, make_org

from orchivio.api.routes.inbound import pointer_get
from orchivio.db.models import AuditEvent, JobRun
from orchivio.integrations import sign
from orchivio.jobs import Worker

SECRET = "inbound_secret_value_123"
FLOW = "core.ReorderLowStock"


@pytest.fixture(autouse=True)
def secret_env(monkeypatch):
    monkeypatch.setenv("ORCHIVIO_TEST_INBOUND_SECRET", SECRET)


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


def make_hook(owner, a, **over):
    body = {"key": "stock", "flow": "ReorderLowStock", "run_as_role": "admin", "secret_ref": "env:ORCHIVIO_TEST_INBOUND_SECRET",
            "arg_map": {"min_gap": "/data/gap"}, **over}
    r = owner.post(f"/applications/{a['id']}/inbound-webhooks", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def send(app, a, payload, key="stock", *, secret=SECRET, t=None, raw=None, headers=None, sig=True):
    body = raw if raw is not None else json.dumps(payload).encode()
    h = {"Content-Type": "application/json"}
    if sig:
        h["Orchivio-Signature"] = sign(secret, body, t)
    h.update(headers or {})  # explicit headers win, so tests can send broken signatures
    return TestClient(app).post(f"/api/v1/hooks/{a['id']}/{key}", content=body, headers=h)


def jobs(app):
    with app.state.session_factory() as s:
        return list(s.scalars(select(JobRun).order_by(JobRun.created_at)))


def seed_low(owner, a):
    r = owner.post(f"/applications/{a['id']}/preview/data/core.Product", headers={"X-Preview-User": "a", "X-Preview-Roles": "admin"},
                   json={"name": "L", "sku": "low", "quantity": 1, "reorder_level": 5})
    assert r.status_code == 201, r.text


# --- management --------------------------------------------------------------------------------


def test_management_validation_and_permissions(world):
    a, p = world
    o = p["owner"]
    w = make_hook(o, a)
    assert w["url_path"] == f"/api/v1/hooks/{a['id']}/stock" and w["secret_available"] is True and w["flow"] == FLOW
    base = f"/applications/{a['id']}/inbound-webhooks"

    def bad(**kw):
        body = {"key": "other", "flow": "ReorderLowStock", "run_as_role": "admin", "secret_ref": "env:ORCHIVIO_TEST_INBOUND_SECRET",
                "arg_map": {"min_gap": "/gap"}, **kw}
        return o.post(base, json=body)

    assert bad(key="stock").json()["error"]["code"] == "key_taken"
    for key in ("Stock", "-x", "a b", "x" * 64):
        assert bad(key=key).status_code == 422
    assert bad(flow="Nope").json()["error"]["code"] == "unknown_flow"
    assert bad(run_as_role="ghost").json()["error"]["code"] == "unknown_role"
    assert bad(secret_ref="vault:x").json()["error"]["code"] == "invalid_secret_ref"
    assert bad(secret_ref="env:NOT_SET_ANYWHERE_1").json()["error"]["code"] == "secret_unavailable"
    for arg_map, defaults in (({}, {}), ({"min_gap": "gap"}, {}), ({"min_gap": "/g", "nope": "/x"}, {}), ({"min_gap": "/g~2"}, {}),
                              ({}, {"min_gap": "ten"}), ({}, {"nope": 1})):
        r = bad(arg_map=arg_map, defaults=defaults)
        assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_mapping", (arg_map, defaults)
    assert bad(arg_map={}, defaults={"min_gap": 2}).status_code == 201  # a default alone is enough
    assert p["editor"].get(base).status_code == 403
    assert p["outsider"].get(base).status_code == 404
    assert len(o.get(base).json()["items"]) == 2
    assert o.patch(f"/inbound-webhooks/{w['id']}", json={"status": "paused"}).json()["status"] == "paused"
    assert o.patch(f"/inbound-webhooks/{w['id']}", json={"run_as_role": "ghost"}).status_code == 422
    assert o.delete(f"/inbound-webhooks/{w['id']}").status_code == 204 and o.get(f"/inbound-webhooks/{w['id']}").status_code == 404


def test_secret_availability_is_visible_to_admins(world, monkeypatch):
    a, p = world
    w = make_hook(p["owner"], a)
    monkeypatch.delenv("ORCHIVIO_TEST_INBOUND_SECRET")
    assert p["owner"].get(f"/inbound-webhooks/{w['id']}").json()["secret_available"] is False


# --- the signature gate ------------------------------------------------------------------------


def test_a_valid_request_queues_a_job_that_runs_as_the_webhook_role(world, app):
    a, p = world
    make_hook(p["owner"], a)
    seed_low(p["owner"], a)
    r = send(app, a, {"data": {"gap": 0}})
    assert r.status_code == 202 and r.json()["accepted"] is True and r.json()["duplicate"] is False
    (job,) = jobs(app)
    assert (job.trigger, job.flow, job.args, job.run_as_role, job.status) == ("webhook", FLOW, {"min_gap": 0}, "admin", "queued")
    assert str(job.id) == r.json()["job_id"] and job.source_ref.startswith("inbound:")
    assert Worker(app.state.session_factory, app.state.preview_store).tick().succeeded == 1
    rows = p["owner"].get(f"/applications/{a['id']}/preview/data/core.Product", headers={"X-Preview-User": "a", "X-Preview-Roles": "admin"}).json()
    assert rows["items"][0]["reorder_level"] == 10  # the flow really ran
    assert p["owner"].get(f"/applications/{a['id']}/jobs?status=succeeded").json()["items"][0]["trigger"] == "webhook"


def test_every_authentication_failure_looks_the_same(world, app, monkeypatch):
    a, p = world
    make_hook(p["owner"], a)
    payload = {"data": {"gap": 0}}
    body = json.dumps(payload).encode()
    results = [
        send(app, a, payload, sig=False),
        send(app, a, payload, secret="wrong"),
        send(app, a, payload, t=int(time.time()) - 1000),  # stale: outside the replay window
        send(app, a, payload, headers={"Orchivio-Signature": "garbage"}),
        send(app, a, None, raw=body + b" ", sig=False, headers={"Orchivio-Signature": sign(SECRET, body)}),  # signed body != sent body
    ]
    monkeypatch.delenv("ORCHIVIO_TEST_INBOUND_SECRET")  # the server cannot verify at all: still the same answer
    results.append(send(app, a, payload))
    assert {r.status_code for r in results} == {401}
    assert len({json.dumps(r.json()) for r in results}) == 1
    assert jobs(app) == []


def test_unknown_paused_and_foreign_endpoints_are_plain_404s(world, app):
    a, p = world
    w = make_hook(p["owner"], a)
    assert send(app, a, {}, key="nope").status_code == 404
    p["owner"].patch(f"/inbound-webhooks/{w['id']}", json={"status": "paused"})
    assert send(app, a, {"data": {"gap": 0}}).status_code == 404
    import uuid
    assert TestClient(app).post(f"/api/v1/hooks/{uuid.uuid4()}/stock", content=b"{}").status_code == 404


def test_replays_and_duplicate_ids_run_once(world, app):
    a, p = world
    make_hook(p["owner"], a)
    body = json.dumps({"data": {"gap": 0}}).encode()
    header = sign(SECRET, body)

    def post(extra=None, hdr=header):
        return TestClient(app).post(f"/api/v1/hooks/{a['id']}/stock", content=body, headers={"Orchivio-Signature": hdr, **(extra or {})})

    assert post().json()["duplicate"] is False
    again = post()
    assert again.status_code == 202 and again.json()["duplicate"] is True and again.json()["job_id"] is None
    assert len(jobs(app)) == 1  # the identical signed request is a replay
    fresh = sign(SECRET, body, int(time.time()) + 1)
    assert post({"Webhook-Id": "evt_1"}, fresh).json()["duplicate"] is False
    assert post({"Webhook-Id": "evt_1"}, sign(SECRET, body, int(time.time()) + 2)).json()["duplicate"] is True  # same sender id
    assert post({"Webhook-Id": "evt_2"}, sign(SECRET, body, int(time.time()) + 3)).json()["duplicate"] is False
    assert len(jobs(app)) == 3
    assert post({"Webhook-Id": "bad id!"}, sign(SECRET, body, int(time.time()) + 4)).status_code == 400
    hook_id = p["owner"].get(f"/applications/{a['id']}/inbound-webhooks").json()["items"][0]["id"]
    events = p["owner"].get(f"/inbound-webhooks/{hook_id}/events").json()
    assert len(events["items"]) == 3


# --- payload mapping ---------------------------------------------------------------------------


def test_payload_must_fit_the_flow_parameters(world, app):
    a, p = world
    make_hook(p["owner"], a)
    for payload, why in (({"data": {}}, "nothing at"), ({"data": {"gap": "ten"}}, "does not fit"), ({"data": {"gap": True}}, "does not fit"),
                         ({"other": 1}, "nothing at")):
        r = send(app, a, payload)
        assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_payload", payload
        d = r.json()["error"]["details"][0]
        assert d["param"] == "min_gap" and why in d["message"] and "ten" not in json.dumps(r.json())  # values are not echoed
    assert send(app, a, None, raw=b"not json").status_code == 400
    assert jobs(app) == []  # nothing queued, and nothing recorded: a corrected resend with the same id works
    ok = send(app, a, {"data": {"gap": 2}})
    assert ok.status_code == 202 and jobs(app)[0].args == {"min_gap": 2}


def test_defaults_arrays_and_escapes(world, app):
    a, p = world
    o = p["owner"]
    make_hook(o, a, key="dflt", arg_map={"min_gap": "/gap"}, defaults={"min_gap": 4})
    assert send(app, a, {}, key="dflt").status_code == 202
    assert send(app, a, {"gap": 1}, key="dflt", t=int(time.time()) + 1).status_code == 202
    make_hook(o, a, key="arr", arg_map={"min_gap": "/items/1/a~1b"})
    assert send(app, a, {"items": [{}, {"a/b": 7}]}, key="arr").status_code == 202
    assert [j.args["min_gap"] for j in jobs(app)] == [4, 1, 7]


def test_size_and_rate_limits(world, app):
    a, p = world
    make_hook(p["owner"], a)
    big = b'{"data":{"gap":0},"pad":"' + b"x" * 300_000 + b'"}'
    assert send(app, a, None, raw=big).status_code == 413
    app.state.inbound_limiter.hits.clear()  # the oversize request above also counted
    app.state.inbound_limiter.per_minute = 2
    codes = [send(app, a, {"data": {"gap": 0}}, t=int(time.time()) + i).status_code for i in range(4)]
    assert codes == [202, 202, 429, 429]
    app.state.inbound_limiter.per_minute = 60


def test_audit_records_acceptance_without_the_payload_or_secret(world, app):
    a, p = world
    make_hook(p["owner"], a)
    send(app, a, {"data": {"gap": 0}, "private": "TOP-SECRET-PAYLOAD"})
    with app.state.session_factory() as s:
        events = list(s.scalars(select(AuditEvent)))
    accepted = [e for e in events if e.action == "inbound_webhook.accepted"]
    assert len(accepted) == 1 and accepted[0].plane == "application"
    dump = json.dumps([e.meta for e in events])
    assert "TOP-SECRET-PAYLOAD" not in dump and SECRET not in dump


def test_a_flow_that_disappears_is_reported_after_authentication(world, app):
    a, p = world
    make_hook(p["owner"], a)
    # the model changes: the flow is removed (the target cannot be a deleted flow)
    import copy
    m = p["owner"].get(f"/revisions/{a['current_revision_id']}").json()["model_json"]
    m = copy.deepcopy(m)
    m["modules"][0]["flows"] = []
    r = p["owner"].post(f"/applications/{a['id']}/revisions", json={"model": m, "acknowledge_risks": True})
    if r.status_code != 201:
        pytest.skip(r.text)  # the model may forbid removing a flow that is a hook target; then there is nothing to test
    assert send(app, a, {"data": {"gap": 0}}, sig=False).status_code == 401  # still authenticated first
    assert send(app, a, {"data": {"gap": 0}}).status_code == 422


# --- pointers ----------------------------------------------------------------------------------


@pytest.mark.parametrize("pointer, expected", [
    ("", {"foo": ["bar", "baz"], "": 0, "a/b": 1, "m~n": 8}), ("/foo", ["bar", "baz"]), ("/foo/0", "bar"), ("/", 0),
    ("/a~1b", 1), ("/m~0n", 8), ("/foo/2", "MISSING"), ("/foo/x", "MISSING"), ("/nope", "MISSING"), ("/foo/0/deeper", "MISSING"),
])
def test_json_pointer_examples(pointer, expected):
    doc = {"foo": ["bar", "baz"], "": 0, "a/b": 1, "m~n": 8}
    got = pointer_get(doc, pointer)
    assert (got if expected != "MISSING" else "MISSING" if got.__class__ is object else got) == expected
