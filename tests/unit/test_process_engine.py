import copy
from datetime import UTC, datetime, timedelta

import pytest

from orchivio.model import AppModel, assign_element_ids, migrate_to_latest, validate_model
from orchivio.process import Machine, ProcessError, State, resolve_definition
from tests_support import with_process

T0 = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)


class FakeHooks:
    def __init__(self):
        self.tasks, self.flows, self.events = [], [], []
        self.flow_error = None

    def create_task(self, spec):
        self.tasks.append(spec)
        return f"task-{len(self.tasks)}"

    def run_flow(self, flow, args):
        self.flows.append((flow, args))
        return self.flow_error

    def event(self, type_, step_id, data):
        self.events.append((type_, step_id))


def build(raw, hooks=None, now=T0, mutate=None):
    m = with_process(raw)
    if mutate:
        mutate(m["modules"][0]["processes"][0])
    model = assign_element_ids(AppModel.model_validate(migrate_to_latest(m)))
    mod, proc = resolve_definition(model, "Approval")
    hooks = hooks or FakeHooks()
    clock = type("C", (), {"t": now, "__call__": lambda self: self.t})()
    return Machine(model, mod, proc, hooks, clock), hooks, clock


def at(st, step):
    return [t for t in st.tokens if t.step == step]


def test_start_waits_at_the_first_task(raw_model):
    mach, hooks, _ = build(raw_model)
    st = mach.start({"amount": 10, "note": "pens"}, "user-1")
    assert st.status == "running" and [t.step for t in st.tokens] == ["review"] and st.tokens[0].status == "waiting_task"
    spec = hooks.tasks[0]
    assert (spec.title, spec.assignee_role, spec.assignee_user, spec.outcomes) == ("Review request", "editor", None, ("approve", "reject", "more_info"))
    assert spec.due_at == T0 + timedelta(hours=24) and spec.escalate_at == T0 + timedelta(hours=8) and spec.escalate_to_role == "admin"
    assert st.variables["initiator"] == "user-1" and st.variables["reviewer_comment"] is None


def test_a_small_request_goes_straight_to_the_parallel_part_and_finishes_when_both_branches_do(raw_model):
    mach, hooks, clock = build(raw_model)
    st = mach.start({"amount": 10, "note": ""}, "u")
    mach.complete_task(st, st.tokens[0].id, "approve", {"reviewer_comment": "ok"})
    assert hooks.flows == [("core.ReorderLowStock", {"min_gap": 0})]  # the system task ran
    assert st.variables["reviewer_comment"] == "ok" and st.status == "running"
    (timer,) = st.tokens
    assert timer.status == "waiting_timer" and timer.step == "cool" and datetime.fromisoformat(timer.wake_at) == T0 + timedelta(hours=1)
    assert st.arrivals["join"] == 1 and st.next_wake_at == timer.wake_at  # one branch is already at the join
    mach.fire_timer(st, timer.id)
    assert st.status == "completed" and st.tokens == [] and st.arrivals["join"] == 0


def test_a_large_request_needs_the_manager_first(raw_model):
    mach, hooks, _ = build(raw_model)
    st = mach.start({"amount": 5000, "note": ""}, "u")
    mach.complete_task(st, st.tokens[0].id, "approve", {})
    assert [t.step for t in st.tokens] == ["manager"] and hooks.tasks[-1].assignee_role == "admin" and hooks.flows == []
    mach.complete_task(st, st.tokens[0].id, "reject", {})
    assert st.status == "completed" and ("completed", None) in hooks.events


def test_rework_loops_back_to_the_same_task(raw_model):
    mach, hooks, _ = build(raw_model)
    st = mach.start({"amount": 1, "note": ""}, "alice")
    mach.complete_task(st, st.tokens[0].id, "more_info", {})
    assert hooks.tasks[-1].title == "Add information" and hooks.tasks[-1].assignee_user == "alice"  # assigned by expression
    mach.complete_task(st, st.tokens[0].id, "done", {})
    assert hooks.tasks[-1].title == "Review request" and len(hooks.tasks) == 3 and st.status == "running"


def test_a_failing_system_task_fails_the_instance_and_can_be_retried(raw_model):
    hooks = FakeHooks()
    hooks.flow_error = "boom"
    mach, hooks, _ = build(raw_model, hooks)
    st = mach.start({"amount": 1, "note": ""}, "u")
    mach.complete_task(st, st.tokens[0].id, "approve", {})
    assert st.status == "failed" and "boom" in st.error and "notify" in st.error
    failed = next(t for t in st.tokens if t.status == "failed")
    hooks.flow_error = None
    mach.retry(st, failed.id)
    assert st.status == "running" and [t.step for t in st.tokens] == ["cool"]
    with pytest.raises(ProcessError):
        mach.retry(st, st.tokens[0].id)


def test_invalid_completions_are_refused_and_change_nothing(raw_model):
    mach, _, _ = build(raw_model)
    st = mach.start({"amount": 1, "note": ""}, "u")
    before = st.to_json()
    with pytest.raises(ProcessError, match="not an outcome"):
        mach.complete_task(st, st.tokens[0].id, "explode", {})
    with pytest.raises(ProcessError, match="not waiting"):
        mach.complete_task(st, 999, "approve", {})
    with pytest.raises(ProcessError, match="not waiting"):
        mach.fire_timer(st, st.tokens[0].id)
    assert st.to_json() == before


def test_state_round_trips_through_json(raw_model):
    mach, _, _ = build(raw_model)
    st = mach.start({"amount": 1, "note": "x"}, "u")
    mach.complete_task(st, st.tokens[0].id, "approve", {})
    again = State.from_json(st.to_json())
    assert again.to_json() == st.to_json() and again.tokens[0].status == "waiting_timer"


def test_a_bad_assignee_expression_fails_the_step_not_the_engine(raw_model):
    def mutate(p):
        next(s for s in p["steps"] if s["id"] == "fix")["config"]["assignee_expr"] = "note"  # empty string: no user id

    mach, _, _ = build(raw_model, mutate=mutate)
    st = mach.start({"amount": 1, "note": ""}, "u")
    mach.complete_task(st, st.tokens[0].id, "more_info", {})
    assert st.status == "failed" and "user id" in st.error


def test_step_limit_stops_runaway_instances(raw_model):
    def mutate(p):  # a decision loop that always goes back to itself through an end-less timer-free path is invalid,
        pass  # so the limit is exercised by lowering it instead

    import orchivio.process.engine as eng
    mach, _, _ = build(raw_model, mutate=mutate)
    old = eng.MAX_STEPS_PER_ADVANCE
    eng.MAX_STEPS_PER_ADVANCE = 2
    try:
        st = mach.start({"amount": 1, "note": ""}, "u")
        mach.complete_task(st, st.tokens[0].id, "approve", {})
    finally:
        eng.MAX_STEPS_PER_ADVANCE = old
    assert st.status == "failed" and "too many steps" in st.error


# --- validation -------------------------------------------------------------------------------


def errors(raw, mutate):
    m = with_process(raw)
    mutate(m["modules"][0]["processes"][0])
    model = assign_element_ids(AppModel.model_validate(migrate_to_latest(m)))
    return {(i.code, i.path.rsplit("/", 1)[-1]) for i in validate_model(model) if i.severity == "error"}, [i.message for i in validate_model(model) if i.severity == "error"]


def step(p, sid):
    return next(s for s in p["steps"] if s["id"] == sid)


@pytest.mark.parametrize("change, code", [
    (lambda p: p["steps"].remove(step(p, "start")), "process-start"),
    (lambda p: [p["steps"].remove(step(p, "done")), p["steps"].remove(step(p, "rejected"))], "process-end"),
    (lambda p: p["steps"].append({"id": "orphan", "type": "end"}), "unreachable-step"),
    (lambda p: p["edges"].append({"id": "e1", "source": "start", "target": "review"}), "duplicate-edge"),
    (lambda p: p["edges"].append({"id": "ex", "source": "review", "target": "nowhere", "outcome": "approve"}), "edge-unknown-step"),
    (lambda p: p["edges"].remove(next(e for e in p["edges"] if e["id"] == "e3")), "process-edges"),  # outcome 'reject' lost its edge
    (lambda p: p["edges"].remove(next(e for e in p["edges"] if e["id"] == "e7")), "process-edges"),  # decision without 'false'
    (lambda p: p["edges"].remove(next(e for e in p["edges"] if e["id"] == "e13")), "process-join"),
    (lambda p: step(p, "review")["config"].update(assignee_role="ghost"), "unknown-role"),
    (lambda p: step(p, "review")["config"].update(assignee_expr="initiator"), "process-assignee"),
    (lambda p: step(p, "review")["config"].pop("assignee_role"), "process-assignee"),
    (lambda p: step(p, "review")["config"].update(outcomes=["Approve!"]), "process-config"),
    (lambda p: step(p, "review")["config"].update(escalate_to_role=None) or step(p, "review")["config"].pop("escalate_to_role"), "process-escalation"),
    (lambda p: step(p, "review")["config"].update(due_in_hours=0), "process-config"),
    (lambda p: step(p, "review")["config"]["form"].append({"name": "amount", "type": "integer"}), "process-variable"),
    (lambda p: step(p, "big")["config"].update(expr="amount + 1"), "expression"),
    (lambda p: step(p, "big")["config"].update(expr="nope > 1"), "expression"),
    (lambda p: step(p, "notify")["config"].update(flow="Ghost"), "process-config"),
    (lambda p: step(p, "notify")["config"].update(args={}), "process-config"),
    (lambda p: p.update(run_as_role=None), "process-run-as"),
    (lambda p: p.update(start_roles=["ghost"]), "unknown-role"),
    (lambda p: step(p, "cool")["config"].update(wait_seconds=0), "process-config"),
    (lambda p: step(p, "cool")["config"].update(surprise=1), "process-config"),
    (lambda p: p["params"].append({"name": "initiator", "type": "string"}), "process-variable"),
])
def test_process_rules(raw_model, change, code):
    codes, messages = errors(raw_model, change)
    assert code in {c for c, _ in codes}, (codes, messages)


def test_a_loop_must_wait_somewhere(raw_model):
    def make_sync_loop(p):
        p["steps"] += [{"id": "a", "type": "decision", "config": {"expr": "amount > 0"}}, {"id": "b", "type": "decision", "config": {"expr": "amount > 1"}}]
        p["edges"] += [{"id": "x1", "source": "a", "target": "b", "outcome": "true"}, {"id": "x2", "source": "a", "target": "b", "outcome": "false"},
                       {"id": "x3", "source": "b", "target": "a", "outcome": "true"}, {"id": "x4", "source": "b", "target": "done", "outcome": "false"}]
        next(e for e in p["edges"] if e["id"] == "e7")["target"] = "a"

    codes, _ = errors(raw_model, make_sync_loop)
    assert "process-sync-cycle" in {c for c, _ in codes}
    assert "process-sync-cycle" not in {c for c, _ in errors(raw_model, lambda p: None)[0]}  # the rework loop waits for a person


def test_a_step_that_cannot_finish_is_reported(raw_model):
    def trap(p):
        p["steps"].append({"id": "trap", "type": "timer", "config": {"wait_seconds": 5}})
        p["edges"].append({"id": "t1", "source": "trap", "target": "trap"})
        next(e for e in p["edges"] if e["id"] == "e3")["target"] = "trap"

    codes, _ = errors(raw_model, trap)
    assert "process-dead-end" in {c for c, _ in codes}


def test_models_without_processes_are_untouched(raw_model):
    m = assign_element_ids(AppModel.model_validate(migrate_to_latest(copy.deepcopy(raw_model))))
    assert m.modules[0].processes is None
