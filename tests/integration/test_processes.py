"""Processes and the task inbox: starting, visibility, claiming, delegation, timers, escalation, failure and control."""

import copy
from datetime import timedelta

import pytest
from sqlalchemy import select
from test_app_auth import AppClient, add_user, ready_client
from test_tenancy import add_member, make_org
from tests_support import with_process

from orchivio.db.models import AuditEvent, ProcessInstance, UserTask
from orchivio.model import AppModel, assign_element_ids, build_archive, diff_models, flatten, migrate_to_latest, read_archive, unflatten
from orchivio.process import ProcessService, ServiceError
from orchivio.process.service import aware
from orchivio.api.deps import utcnow


@pytest.fixture
def world(new_api, raw_model, app):
    owner = new_api("owner@example.com")
    org = make_org(owner)
    proj = owner.post(f"/organizations/{org['id']}/projects", json={"name": "Inv", "slug": "inventory"}).json()
    viewer_member = new_api("viewer@example.com")
    add_member(owner, org, "viewer@example.com", "viewer")
    outsider = new_api("outsider@example.com")
    r = owner.post(f"/projects/{proj['id']}/applications", json={"key": "inventory", "name": "Inv", "model": with_process(raw_model)})
    assert r.status_code == 201, r.text
    a = r.json()
    owner.post(f"/applications/{a['id']}/preview/reset", json={})
    users = {
        "alice": ready_client(app, owner, a, "alice", ["viewer"]),  # may start processes
        "ed": ready_client(app, owner, a, "ed", ["editor"]),
        "bob": ready_client(app, owner, a, "bob", ["editor"]),
        "ada": ready_client(app, owner, a, "ada", ["admin"]),
    }
    return a, owner, users, viewer_member, outsider


def uid(c):
    return c.get("/auth/me").json()["id"]


def seed_low_stock(owner, a):
    r = owner.post(f"/applications/{a['id']}/preview/data/core.Product", headers={"X-Preview-User": "s", "X-Preview-Roles": "admin"},
                   json={"name": "L", "sku": "low", "quantity": 1, "reorder_level": 5})
    assert r.status_code == 201, r.text


def start(c, amount=10, note="pens"):
    r = c.post("/processes/core.Approval/start", json={"args": {"amount": amount, "note": note}})
    assert r.status_code == 201, r.text
    return r.json()["instance_id"]


def tasks(c, **q):
    qs = "&".join(f"{k}={v}" for k, v in q.items())
    return c.get("/tasks" + (f"?{qs}" if qs else "")).json()["items"]


def tick(app, delta=timedelta(0)):
    with app.state.session_factory() as s:
        svc = ProcessService(s, app.state.preview_store, audit=app.state.audit_sink, integrations=app.state.integration_clients,
                             clock=lambda: utcnow() + delta)
        return svc.tick()


def instance(owner, iid):
    return owner.get(f"/process-instances/{iid}").json()


# --- starting ----------------------------------------------------------------------------------


def test_who_may_start_and_what_they_may_pass(world, app):
    a, owner, u, *_ = world
    assert [p["process"] for p in u["alice"].get("/processes").json()["items"]] == ["core.Approval"]
    assert u["ada"].get("/processes").json()["items"] == []  # admins were not given start rights
    assert u["ada"].post("/processes/core.Approval/start", json={"args": {"amount": 1, "note": ""}}).status_code == 403
    assert u["alice"].post("/processes/core.Nope/start", json={"args": {}}).json()["error"]["code"] == "unknown_process"
    bad = u["alice"].post("/processes/core.Approval/start", json={"args": {"amount": "lots", "extra": 1}})
    assert bad.status_code == 422 and {d["param"] for d in bad.json()["error"]["details"]} == {"amount", "note", "extra"}
    assert AppClient(app, a["id"]).post("/processes/core.Approval/start", json={"args": {}}).status_code == 401
    iid = start(u["alice"])
    mine = u["alice"].get(f"/process-instances/{iid}").json()
    assert mine["status"] == "running" and [e["type"] for e in mine["events"]] == ["started", "task_created"]
    assert "variables" not in mine  # the starter sees progress, not the process's data
    assert u["ed"].get(f"/process-instances/{iid}").status_code == 404


# --- visibility, claiming, delegation ----------------------------------------------------------


def test_tasks_are_offered_to_roles_and_claimed_exclusively(world):
    a, owner, u, *_ = world
    iid = start(u["alice"])
    assert tasks(u["alice"]) == [] and tasks(u["ada"]) == []  # neither the starter nor admins are offered an editor task
    (t,) = tasks(u["ed"])
    assert t["title"] == "Review request" and t["claimable"] is True and t["mine"] is False and t["outcomes"] == ["approve", "reject", "more_info"]
    assert tasks(u["ed"], scope="mine") == [] and len(tasks(u["bob"], scope="available")) == 1
    assert u["alice"].get(f"/tasks/{t['id']}").status_code == 404 and u["ada"].post(f"/tasks/{t['id']}/claim").status_code == 404

    assert u["ed"].post(f"/tasks/{t['id']}/claim").json()["mine"] is True
    assert u["ed"].post(f"/tasks/{t['id']}/claim").status_code == 200  # idempotent for the holder
    assert u["bob"].get(f"/tasks/{t['id']}").status_code == 404 and tasks(u["bob"]) == []  # now it is ed's alone
    assert u["bob"].post(f"/tasks/{t['id']}/complete", json={"outcome": "approve"}).status_code == 404

    assert u["ed"].post(f"/tasks/{t['id']}/release").json()["claimable"] is True
    assert u["bob"].post(f"/tasks/{t['id']}/claim").json()["mine"] is True
    assert u["ed"].post(f"/tasks/{t['id']}/release").status_code == 404  # no longer visible to ed
    assert iid


def test_delegation_goes_only_to_people_who_could_do_the_work(world, app):
    a, owner, u, *_ = world
    start(u["alice"])
    (t,) = tasks(u["ed"])
    tid = t["id"]
    assert u["ed"].post(f"/tasks/{tid}/delegate", json={"username": "alice"}).json()["error"]["code"] == "invalid_delegate"  # lacks the role
    assert u["ed"].post(f"/tasks/{tid}/delegate", json={"username": "nobody"}).status_code == 404
    assert u["ed"].post(f"/tasks/{tid}/delegate", json={"username": "ed"}).json()["error"]["code"] == "invalid_delegate"
    owner.patch(f"/app-users/{next(x['id'] for x in owner.get(f'/applications/{a['id']}/app-users').json()['items'] if x['username'] == 'bob')}", json={"status": "disabled"})
    assert u["ed"].post(f"/tasks/{tid}/delegate", json={"username": "bob"}).status_code == 404  # disabled users are not delegates
    owner.patch(f"/app-users/{next(x['id'] for x in owner.get(f'/applications/{a['id']}/app-users').json()['items'] if x['username'] == 'bob')}", json={"status": "active"})
    bob = AppClient(app, a["id"])
    bob.login("bob", "a much better pass phrase 2")
    r = u["ed"].post(f"/tasks/{tid}/delegate", json={"username": "bob"})
    assert r.status_code == 200 and r.json()["assignee_user"] == uid(bob) and r.json()["delegated_by"] == uid(u["ed"])
    assert tasks(bob, scope="mine")[0]["id"] == tid
    assert u["ed"].get(f"/tasks/{tid}").status_code == 200  # the delegator can still see what they handed over
    assert u["ed"].post(f"/tasks/{tid}/complete", json={"outcome": "approve"}).status_code == 403


# --- running a process -------------------------------------------------------------------------


def test_a_small_request_runs_the_system_task_waits_on_the_timer_then_completes(world, app):
    a, owner, u, *_ = world
    seed_low_stock(owner, a)
    iid = start(u["alice"], amount=10)
    (t,) = tasks(u["ed"])
    assert u["ed"].post(f"/tasks/{t['id']}/complete", json={"outcome": "explode"}).json()["error"]["code"] == "invalid_outcome"
    assert u["ed"].post(f"/tasks/{t['id']}/complete", json={"outcome": "approve", "result": {"nope": 1}}).json()["error"]["code"] == "invalid_result"
    done = u["ed"].post(f"/tasks/{t['id']}/complete", json={"outcome": "approve", "result": {"reviewer_comment": "fine"}})
    assert done.status_code == 200, done.text
    assert done.json()["status"] == "completed" and done.json()["outcome"] == "approve" and done.json()["instance_status"] == "running"
    assert u["ed"].post(f"/tasks/{t['id']}/complete", json={"outcome": "approve"}).json()["error"]["code"] == "task_closed"
    rows = owner.get(f"/applications/{a['id']}/preview/data/core.Product", headers={"X-Preview-User": "a", "X-Preview-Roles": "admin"}).json()["items"]
    assert rows[0]["reorder_level"] == 10  # the system task really ran the flow, as the process's run_as_role

    inst = instance(owner, iid)
    assert inst["status"] == "running" and inst["variables"]["reviewer_comment"] == "fine" and inst["tokens"][0]["status"] == "waiting_timer"
    assert tick(app, timedelta(minutes=30)) == {"timers": 0, "escalations": 0}  # not due yet
    assert tick(app, timedelta(hours=2))["timers"] == 1
    inst = instance(owner, iid)
    assert inst["status"] == "completed" and inst["finished_at"] and inst["tokens"] == []
    kinds = [e["type"] for e in inst["events"]]
    assert kinds[:3] == ["started", "task_created", "task_completed"] and "system_task_ok" in kinds and "timer_fired" in kinds and kinds[-1] == "completed"
    assert [e["type"] for e in u["alice"].get(f"/process-instances/{iid}").json()["events"]][-1] == "completed"
    assert tasks(u["ed"], status="completed")[0]["outcome"] == "approve"
    with app.state.session_factory() as s:
        assert {"process.started", "task.completed"} <= {e.action for e in s.scalars(select(AuditEvent))}


def test_rework_loops_back_and_big_requests_need_a_manager(world):
    a, owner, u, *_ = world
    iid = start(u["alice"], amount=5000)
    (t,) = tasks(u["ed"])
    u["ed"].post(f"/tasks/{t['id']}/complete", json={"outcome": "more_info"})
    (fix,) = tasks(u["alice"], scope="mine")  # assigned to the initiator by expression
    assert fix["title"] == "Add information" and fix["assignee_role"] is None
    assert u["alice"].post(f"/tasks/{fix['id']}/release").json()["error"]["code"] == "not_releasable"
    u["alice"].post(f"/tasks/{fix['id']}/complete", json={"outcome": "done"})
    (again,) = tasks(u["ed"])
    assert again["title"] == "Review request" and again["id"] != t["id"]
    u["ed"].post(f"/tasks/{again['id']}/complete", json={"outcome": "approve"})
    assert tasks(u["ed"]) == []
    (mgr,) = tasks(u["ada"])
    assert mgr["title"] == "Manager approval"
    out = u["ada"].post(f"/tasks/{mgr['id']}/complete", json={"outcome": "reject"}).json()
    assert out["instance_status"] == "completed"
    assert [e["type"] for e in instance(owner, iid)["events"]].count("task_created") == 4


def test_form_fields_are_validated_and_typed(world):
    a, owner, u, *_ = world
    start(u["alice"])
    (t,) = tasks(u["ed"])
    bad = u["ed"].post(f"/tasks/{t['id']}/complete", json={"outcome": "reject", "result": {"reviewer_comment": 5}})
    assert bad.status_code == 422 and bad.json()["error"]["details"][0] == {"field": "reviewer_comment", "message": "must be of type string"}
    assert u["ed"].post(f"/tasks/{t['id']}/complete", json={"outcome": "reject", "result": {"reviewer_comment": ""}}).status_code == 200  # optional


# --- failure, retry, cancel --------------------------------------------------------------------


def test_a_failing_system_task_stops_the_instance_until_someone_retries(world, app):
    a, owner, u, viewer_member, outsider = world
    iid = start(u["alice"], amount=10)  # no low-stock product exists, so the flow raises
    (t,) = tasks(u["ed"])
    u["ed"].post(f"/tasks/{t['id']}/complete", json={"outcome": "approve"})
    inst = instance(owner, iid)
    assert inst["status"] == "failed" and "ReorderLowStock" in inst["error"] and "notify" in inst["error"]
    assert any(t["status"] == "failed" for t in inst["tokens"])
    assert owner.get(f"/applications/{a['id']}/process-instances?status=failed").json()["total"] == 1
    assert viewer_member.post(f"/process-instances/{iid}/retry").status_code == 403
    assert outsider.post(f"/process-instances/{iid}/retry").status_code == 404
    seed_low_stock(owner, a)
    ok = owner.post(f"/process-instances/{iid}/retry")
    assert ok.status_code == 200 and ok.json()["status"] == "running"
    assert owner.post(f"/process-instances/{iid}/retry").json()["error"]["code"] == "not_failed"


def test_cancel_closes_the_instance_and_its_tasks(world):
    a, owner, u, viewer_member, _ = world
    iid = start(u["alice"])
    (t,) = tasks(u["ed"])
    assert viewer_member.post(f"/process-instances/{iid}/cancel").status_code == 403
    assert owner.post(f"/process-instances/{iid}/cancel").json()["status"] == "cancelled"
    assert tasks(u["ed"]) == []
    late = u["ed"].post(f"/tasks/{t['id']}/complete", json={"outcome": "approve"})
    assert late.status_code == 409 and late.json()["error"]["code"] == "task_closed"
    assert owner.post(f"/process-instances/{iid}/cancel").json()["error"]["code"] == "instance_closed"
    assert owner.get(f"/applications/{a['id']}/user-tasks?status=cancelled").json()["total"] == 1


# --- escalation and due dates ------------------------------------------------------------------


def test_overdue_tasks_escalate_once_to_the_configured_role(world, app):
    a, owner, u, *_ = world
    start(u["alice"])
    (t,) = tasks(u["ed"])
    u["ed"].post(f"/tasks/{t['id']}/claim")
    assert tick(app, timedelta(hours=7))["escalations"] == 0
    assert tick(app, timedelta(hours=9))["escalations"] == 1
    assert tick(app, timedelta(hours=10))["escalations"] == 0  # only once
    assert tasks(u["ed"]) == []  # the claim was dropped along with the role
    (esc,) = tasks(u["ada"])
    assert esc["id"] == t["id"] and esc["assignee_role"] == "admin" and esc["escalated_at"] and esc["claimable"] is True
    assert u["ada"].post(f"/tasks/{t['id']}/claim").json()["mine"] is True
    listing = owner.get(f"/applications/{a['id']}/user-tasks").json()["items"][0]
    assert listing["overdue"] is False and listing["due_at"]  # due after 24 h: not yet at the time of the API call
    events = [e["type"] for e in owner.get(f"/process-instances/{esc['instance_id']}").json()["events"]]
    assert "task_escalated" in events


# --- concurrency -------------------------------------------------------------------------------


def test_two_people_acting_at_once_cannot_both_win(world, app):
    a, owner, u, *_ = world
    iid = start(u["alice"], amount=5000)
    with app.state.session_factory() as s1, app.state.session_factory() as s2:
        t1 = s1.scalars(select(UserTask)).first()
        t2 = s2.scalars(select(UserTask)).first()
        stale = s2.get(ProcessInstance, t2.instance_id)  # s2 looked at the instance before s1 changed it
        assert stale.version == 2
        svc1, svc2 = ProcessService(s1, app.state.preview_store), ProcessService(s2, app.state.preview_store)
        svc1.complete(t1, "u-ed", frozenset({"editor"}), "approve", {})
        s1.commit()
        with pytest.raises(ServiceError) as late:  # the same task, completed a second time
            svc2.complete(t2, "u-bob", frozenset({"editor"}), "reject", {})
        assert late.value.code == "task_closed"  # (no rollback here: `stale` must keep describing the old version)
        with pytest.raises(ServiceError) as cas:  # a change based on the old version of the instance
            svc2.cancel(stale, "someone")
        assert cas.value.code == "conflict"
        s2.rollback()
    inst = instance(owner, iid)
    assert [e["type"] for e in inst["events"]].count("task_completed") == 1 and inst["status"] == "running" and inst["tokens"][0]["step"] == "manager"


# --- isolation and the model -------------------------------------------------------------------


def test_tasks_never_cross_applications(world, app, raw_model):
    a, owner, u, *_ = world
    start(u["alice"])
    (t,) = tasks(u["ed"])
    proj = owner.get(f"/projects/{a['project_id']}").json()
    b = owner.post(f"/projects/{proj['id']}/applications", json={"key": "other", "name": "Other", "model": with_process(raw_model)}).json()
    owner.post(f"/applications/{b['id']}/preview/reset", json={})
    add_user(owner, b, "ed", ["editor"])
    other = AppClient(app, b["id"])
    other.login("ed")
    other.post("/auth/change-password", json={"current_password": "temporary pass phrase 1", "new_password": "a much better pass phrase 2"})
    assert tasks(other) == [] and other.get(f"/tasks/{t['id']}").status_code == 404


def test_widening_who_can_start_is_a_permission_change(raw_model):
    base = assign_element_ids(AppModel.model_validate(migrate_to_latest(with_process(raw_model))))

    def changed(fn):
        m = with_process(raw_model)
        fn(m["modules"][0]["processes"][0])
        return diff_models(base, assign_element_ids(AppModel.model_validate(migrate_to_latest(m)), previous=base))

    assert changed(lambda p: p["start_roles"].append("admin")).level.name == "A3"
    assert changed(lambda p: p.update(run_as_role="editor")).level.name == "A3"
    assert changed(lambda p: p["start_roles"].remove("viewer")).level.name == "A2"
    assert changed(lambda p: p["steps"][1]["config"].update(title="Renamed")).level.name == "A2"
    assert changed(lambda p: p["steps"][0].update(position={"x": 1, "y": 2})).level.name == "A0"  # layout is not a change


def test_processes_survive_files_archives_and_merging(raw_model):
    model = assign_element_ids(AppModel.model_validate(migrate_to_latest(with_process(raw_model))))
    assert read_archive(build_archive(model)) == model
    assert unflatten(flatten(model), schema_version=model.schema_version).modules[0].processes == model.modules[0].processes


def test_studio_can_start_a_process_for_testing(world, app):
    a, owner, u, viewer_member, outsider = world
    r = owner.post(f"/applications/{a['id']}/processes/core.Approval/start", json={"args": {"amount": 1, "note": ""}}, headers={"X-Preview-User": "tester"})
    assert r.status_code == 201 and r.json()["initiator"] == "tester"
    assert viewer_member.post(f"/applications/{a['id']}/processes/core.Approval/start", json={"args": {}}).status_code == 403
    assert outsider.get(f"/applications/{a['id']}/process-instances").status_code == 404
    assert viewer_member.get(f"/applications/{a['id']}/process-instances").json()["total"] == 1
    assert owner.get(f"/applications/{a['id']}/process-instances?process=core.Other").json()["total"] == 0
