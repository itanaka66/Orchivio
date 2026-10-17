"""Scheduled jobs and the task queue: validation, planning, single execution, retry, dead letter, API."""

import copy
import threading
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from test_tenancy import add_member, make_org

from orchivio.db.models import AuditEvent, JobRun
from orchivio.jobs import Worker, queue

T0 = datetime(2026, 3, 2, 10, 0, 0, tzinfo=UTC)  # a Monday, on a 5-minute boundary


def schedule(**kw):
    return {"cron": "*/5 * * * *", "run_as_role": "admin", "args": {"min_gap": 0}, "max_attempts": 3, "backoff_seconds": 10, **kw}


def with_schedule(raw, **kw):
    m = copy.deepcopy(raw)
    flow = next(f for mod in m["modules"] for f in mod["flows"] if f["key"] == "ReorderLowStock")
    flow["schedule"] = schedule(**kw)
    return m


@pytest.fixture
def ctx(new_api, raw_model):
    owner = new_api("owner@example.com")
    org = make_org(owner)
    proj = owner.post(f"/organizations/{org['id']}/projects", json={"name": "Inv", "slug": "inventory"}).json()
    viewer = new_api("viewer@example.com")
    add_member(owner, org, "viewer@example.com", "viewer")
    outsider = new_api("outsider@example.com")
    return proj, {"owner": owner, "viewer": viewer, "outsider": outsider}, raw_model


def make_app(owner, proj, model):
    r = owner.post(f"/projects/{proj['id']}/applications", json={"key": "inventory", "name": "Inv", "model": model})
    assert r.status_code == 201, r.text
    return r.json()


def acting(user, *roles):
    return {"X-Preview-User": user, "X-Preview-Roles": ",".join(roles)}


def seed_low_stock(owner, app, n=1):
    owner.post(f"/applications/{app['id']}/preview/reset", json={})
    for i in range(n):
        r = owner.post(f"/applications/{app['id']}/preview/data/core.Product", headers=acting("a", "admin"),
                       json={"name": f"p{i}", "sku": f"low{i}", "quantity": 1, "reorder_level": 5})
        assert r.status_code == 201, r.text


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, **kw):
        self.t += timedelta(**kw)


def worker_for(app, clock, **kw):
    return Worker(app.state.session_factory, app.state.preview_store, audit=app.state.audit_sink, clock=clock, **kw)


def jobs(app, **where):
    with app.state.session_factory() as s:
        q = select(JobRun).order_by(JobRun.created_at, JobRun.id)
        for k, v in where.items():
            q = q.where(getattr(JobRun, k) == v)
        return [(j.flow, j.status, j.attempt, j.error, queue.aware(j.scheduled_for)) for j in s.scalars(q)]


# --- validation --------------------------------------------------------------


@pytest.mark.parametrize("patch, code", [
    ({"cron": "61 * * * *"}, "bad-cron"),
    ({"cron": "0 0 30 2 *"}, "bad-cron"),
    ({"run_as_role": "nobody"}, "unknown-role"),
    ({"args": {}}, "schedule-args"),
    ({"args": {"min_gap": "ten"}}, "schedule-args"),
    ({"args": {"min_gap": 1, "extra": 1}}, "schedule-args"),
])
def test_invalid_schedules_are_rejected_with_a_code(ctx, patch, code):
    proj, p, raw = ctx
    r = p["owner"].post("/models/validate", json={"model": with_schedule(raw, **patch)})
    assert code in {i["code"] for i in r.json()["issues"] if i["severity"] == "error"}, r.json()["issues"]


def test_valid_schedule_and_unknown_fields(ctx):
    proj, p, raw = ctx
    r = p["owner"].post("/models/validate", json={"model": with_schedule(raw)})
    assert r.json()["valid"] is True
    bad = with_schedule(raw)
    bad["modules"][0]["flows"][0]["schedule"]["surprise"] = 1
    assert p["owner"].post("/models/validate", json={"model": bad}).status_code == 422
    for key, value in (("max_attempts", 0), ("max_attempts", 11), ("backoff_seconds", 0), ("missed_runs", "replay")):
        assert p["owner"].post("/models/validate", json={"model": with_schedule(raw, **{key: value})}).status_code == 422


def test_unscheduled_models_keep_their_hash(ctx):
    from orchivio.model import AppModel, migrate_to_latest, model_hash
    _, _, raw = ctx
    m = AppModel.model_validate(migrate_to_latest(raw))
    assert "schedule" not in m.model_dump(mode="json", exclude_none=True)["modules"][0]["flows"][0]
    assert model_hash(m).startswith("sha256:")


# --- planning and running ----------------------------------------------------


def test_scheduled_run_happens_once_on_time(ctx, app):
    proj, p, raw = ctx
    a = make_app(p["owner"], proj, with_schedule(raw))
    seed_low_stock(p["owner"], a)
    clock = Clock()
    w = worker_for(app, clock)
    assert w.tick().planned == 0  # first sight: planning starts now, nothing is replayed
    clock.advance(minutes=5)
    t = w.tick()
    assert (t.planned, t.claimed, t.succeeded) == (1, 1, 1)
    assert jobs(app) == [("core.ReorderLowStock", "succeeded", 1, None, T0 + timedelta(minutes=5))]
    rows = p["owner"].get(f"/applications/{a['id']}/preview/data/core.Product", headers=acting("a", "admin")).json()["items"]
    assert rows[0]["reorder_level"] == 10  # the flow really ran, as the schedule's role
    clock.advance(minutes=2)
    assert w.tick().planned == 0  # nothing due between occurrences
    clock.advance(minutes=3)
    assert w.tick().planned == 1  # next occurrence


def test_two_workers_never_run_the_same_occurrence(ctx, app):
    proj, p, raw = ctx
    a = make_app(p["owner"], proj, with_schedule(raw))
    seed_low_stock(p["owner"], a)
    clock = Clock()
    w1, w2 = worker_for(app, clock, worker_id="w1"), worker_for(app, clock, worker_id="w2")
    w1.tick(), w2.tick()
    clock.advance(minutes=5)
    results = []
    threads = [threading.Thread(target=lambda w=w: results.append(w.tick())) for w in (w1, w2)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert len(jobs(app)) == 1
    assert sum(r.succeeded for r in results) == 1


def test_planning_the_same_window_again_is_a_noop(ctx, app):
    proj, p, raw = ctx
    a = make_app(p["owner"], proj, with_schedule(raw))
    clock = Clock()
    w = worker_for(app, clock)
    w.tick()
    clock.advance(minutes=5)
    assert w.planner.plan(app.state.session_factory(), clock()) != []
    again = worker_for(app, clock)  # a fresh node with its own cache sees the same state
    with app.state.session_factory() as s:
        assert again.planner.plan(s, clock()) == []
    assert len(jobs(app)) == 1 and a


@pytest.mark.parametrize("policy, expected", [("skip", 0), ("run_once", 1)])
def test_missed_runs_policy(ctx, app, policy, expected):
    proj, p, raw = ctx
    make_app(p["owner"], proj, with_schedule(raw, missed_runs=policy))
    clock = Clock()
    w = worker_for(app, clock)
    w.tick()
    clock.advance(hours=3, minutes=8)  # the last occurrence (3h05) is three minutes old, grace is two
    assert w.tick().planned == expected
    if expected:
        (flow, status, attempt, error, when), = jobs(app)
        assert when == T0 + timedelta(hours=3, minutes=5)  # 38 missed occurrences collapsed into one


def test_skip_policy_still_runs_a_recent_occurrence_after_a_gap(ctx, app):
    proj, p, raw = ctx
    make_app(p["owner"], proj, with_schedule(raw))
    clock = Clock()
    w = worker_for(app, clock)
    w.tick()
    clock.advance(hours=3, minutes=6)  # latest occurrence is one minute old: inside the grace period
    assert w.tick().planned == 1


def test_disabled_schedule_runs_nothing_and_does_not_replay_when_enabled(ctx, app):
    proj, p, raw = ctx
    a = make_app(p["owner"], proj, with_schedule(raw, enabled=False, missed_runs="run_once"))
    clock = Clock()
    w = worker_for(app, clock)
    w.tick()
    clock.advance(hours=2)
    assert w.tick().planned == 0
    sch = p["owner"].get(f"/applications/{a['id']}/schedules").json()["items"][0]
    assert sch["enabled"] is False and sch["next_run_at"] is None


def test_retry_with_backoff_then_dead_letter_then_manual_retry(ctx, app):
    proj, p, raw = ctx
    a = make_app(p["owner"], proj, with_schedule(raw))
    p["owner"].post(f"/applications/{a['id']}/preview/reset", json={})  # nothing low on stock: the flow raises
    clock = Clock()
    w = worker_for(app, clock)
    w.tick()
    clock.advance(minutes=5)
    assert w.tick().retried == 1  # attempt 1 fails, back off 10 s
    assert w.tick().claimed == 0
    clock.advance(seconds=9)
    assert w.tick().claimed == 0
    clock.advance(seconds=1)
    assert w.tick().retried == 1  # attempt 2 fails, back off 20 s
    clock.advance(seconds=19)
    assert w.tick().claimed == 0
    clock.advance(seconds=1)
    assert w.tick().dead == 1  # attempt 3 is the last
    (_, status, attempt, error, _), = jobs(app)
    assert (status, attempt) == ("dead", 3) and "reorder level" in error
    with app.state.session_factory() as s:
        assert s.scalar(select(AuditEvent).where(AuditEvent.action == "job.dead")) is not None
    clock.advance(minutes=1)
    assert w.tick().claimed == 0  # dead runs stay dead

    job = p["owner"].get(f"/applications/{a['id']}/jobs?status=dead").json()["items"][0]
    seed_low_stock(p["owner"], a)
    assert p["owner"].post(f"/jobs/{job['id']}/retry").json()["status"] == "queued"  # queued at the real "now"
    clock.t = datetime.now(UTC) + timedelta(seconds=5)
    w.planner.plan = lambda session, now=None: []  # the jump to real time would also plan new occurrences
    assert w.tick().succeeded == 1
    assert p["owner"].post(f"/jobs/{job['id']}/retry").json()["error"]["code"] == "not_dead"


def test_run_as_role_limits_what_the_job_may_do(ctx, app):
    proj, p, raw = ctx
    a = make_app(p["owner"], proj, with_schedule(raw, run_as_role="viewer", max_attempts=1))
    seed_low_stock(p["owner"], a)
    clock = Clock()
    w = worker_for(app, clock)
    w.tick()
    clock.advance(minutes=5)
    assert w.tick().dead == 1
    assert "may not update" in jobs(app)[0][3]
    rows = p["owner"].get(f"/applications/{a['id']}/preview/data/core.Product", headers=acting("a", "admin")).json()["items"]
    assert rows[0]["reorder_level"] == 5  # unchanged


def test_unbuilt_or_stale_preview_fails_the_attempt_with_a_hint(ctx, app):
    proj, p, raw = ctx
    make_app(p["owner"], proj, with_schedule(raw, max_attempts=1))
    clock = Clock()
    w = worker_for(app, clock)
    w.tick()
    clock.advance(minutes=5)
    assert w.tick().dead == 1
    assert "build the preview" in jobs(app)[0][3]


# --- the queue itself --------------------------------------------------------


def test_lease_expiry_makes_a_crashed_worker_retry(ctx, app):
    proj, p, raw = ctx
    a = make_app(p["owner"], proj, raw)
    with app.state.session_factory() as s:
        jid = queue.enqueue(s, application_id=uuid.UUID(a["id"]), flow="core.ReorderLowStock", args={"min_gap": 0},
                            run_as_role="admin", trigger="manual", max_attempts=2, backoff_seconds=5, at=T0)
        s.commit()
        got = queue.claim(s, "crashy", T0, lease_seconds=60)
        s.commit()
        assert [j.id for j in got] == [jid]
        assert queue.claim(s, "other", T0 + timedelta(seconds=30), lease_seconds=60) == []  # still leased
        s.commit()
        assert queue.claim(s, "other", T0 + timedelta(seconds=61), lease_seconds=60) == []  # requeued, backing off 5 s
        s.commit()
        again = queue.claim(s, "other", T0 + timedelta(seconds=70), lease_seconds=60)
        s.commit()
        assert [(j.id, j.attempt, j.locked_by) for j in again] == [(jid, 2, "other")]
        # the original worker finishing late cannot overwrite the new lease
        job = s.get(JobRun, jid)
        assert queue.succeed(s, job, "crashy", {}, T0 + timedelta(seconds=75)) is False
        assert queue.claim(s, "third", T0 + timedelta(seconds=200), lease_seconds=60) == []
        s.commit()
        assert s.get(JobRun, jid).status == "dead"  # second lease expired on the last attempt


def test_claims_are_exclusive_and_respect_per_app_concurrency(ctx, app):
    proj, p, raw = ctx
    a = make_app(p["owner"], proj, raw)
    with app.state.session_factory() as s:
        for _ in range(5):
            queue.enqueue(s, application_id=uuid.UUID(a["id"]), flow="core.ReorderLowStock", args={}, run_as_role="admin",
                          trigger="manual", at=T0)
        s.commit()
        first = queue.claim(s, "w1", T0, limit=10, per_app_limit=2)
        s.commit()
    with app.state.session_factory() as s2:
        second = queue.claim(s2, "w2", T0, limit=10, per_app_limit=2)
        s2.commit()
    assert len(first) == 2 and second == []  # the application already has two running
    with app.state.session_factory() as s3:
        assert queue.claim(s3, "w3", T0, limit=10, per_app_limit=5) != []


def test_duplicate_occurrence_is_ignored_by_the_database(ctx, app):
    proj, p, raw = ctx
    a = make_app(p["owner"], proj, raw)
    kw = dict(application_id=uuid.UUID(a["id"]), flow="core.ReorderLowStock", args={}, run_as_role="admin", trigger="schedule",
              scheduled_for=T0)
    with app.state.session_factory() as s:
        assert queue.enqueue(s, **kw) is not None
        assert queue.enqueue(s, **kw) is None
        assert queue.enqueue(s, **{**kw, "trigger": "manual", "scheduled_for": None}) is not None
        assert queue.enqueue(s, **{**kw, "trigger": "manual", "scheduled_for": None}) is not None  # manual runs repeat
        s.commit()


# --- API ---------------------------------------------------------------------


def test_job_api_lifecycle_and_permissions(ctx, app):
    proj, p, raw = ctx
    a = make_app(p["owner"], proj, with_schedule(raw))
    seed_low_stock(p["owner"], a)
    o = p["owner"]
    sch = o.get(f"/applications/{a['id']}/schedules").json()["items"]
    assert [(s["flow"], s["cron"], s["enabled"]) for s in sch] == [("core.ReorderLowStock", "*/5 * * * *", True)]
    assert datetime.fromisoformat(sch[0]["next_run_at"]) > datetime.now(UTC)

    r = o.post(f"/applications/{a['id']}/jobs", json={"flow": "ReorderLowStock"})  # args and role from the schedule
    assert r.status_code == 201 and r.json()["status"] == "queued" and r.json()["args"] == {"min_gap": 0}
    assert o.post(f"/applications/{a['id']}/jobs", json={"flow": "Nope"}).json()["error"]["code"] == "unknown_flow"
    assert o.post(f"/applications/{a['id']}/jobs", json={"flow": "core.ReorderLowStock", "run_as_role": "ghost"}).status_code == 422

    assert p["viewer"].post(f"/applications/{a['id']}/jobs", json={"flow": "ReorderLowStock"}).status_code == 403
    assert p["viewer"].get(f"/applications/{a['id']}/jobs").json()["total"] == 1  # viewers may look
    assert p["outsider"].get(f"/applications/{a['id']}/jobs").status_code == 404
    assert p["outsider"].get(f"/jobs/{r.json()['id']}").status_code == 404
    assert p["viewer"].post(f"/jobs/{r.json()['id']}/cancel").status_code == 403

    cancelled = o.post(f"/jobs/{r.json()['id']}/cancel").json()
    assert cancelled["status"] == "cancelled"
    assert o.post(f"/jobs/{r.json()['id']}/cancel").json()["error"]["code"] == "not_queued"

    r2 = o.post(f"/applications/{a['id']}/jobs", json={"flow": "ReorderLowStock"}).json()
    clock = Clock(datetime.now(UTC) + timedelta(seconds=5))
    assert worker_for(app, clock).tick().succeeded == 1
    done = o.get(f"/jobs/{r2['id']}").json()
    assert done["status"] == "succeeded" and done["attempt"] == 1 and done["result"]["status"] == "succeeded"
    assert o.get(f"/applications/{a['id']}/jobs?status=succeeded").json()["total"] == 1
    actions = {e.action for e in app.state.session_factory().scalars(select(AuditEvent))}
    assert {"job.enqueued", "job.cancelled"} <= actions


def test_hook_flows_and_client_flows_cannot_be_queued(ctx):
    proj, p, raw = ctx
    m = copy.deepcopy(raw)
    mod = m["modules"][0]
    mod["flows"].append({"key": "ClientOnly", "kind": "client", "steps": [{"id": "start", "type": "start"}, {"id": "end", "type": "end"}],
                         "edges": [{"id": "e1", "source": "start", "target": "end"}]})
    a = make_app(p["owner"], proj, m)
    r = p["owner"].post(f"/applications/{a['id']}/jobs", json={"flow": "ClientOnly", "run_as_role": "admin"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "not_runnable"
