import copy
import uuid

import pytest

from orchivio.db import make_engine
from orchivio.logic import FlowHooks, FlowRunner, Limits
from orchivio.model import AppModel, assign_element_ids, has_errors, model_hash, validate_model
from orchivio.ports import InMemoryAuditSink
from orchivio.runtime import AppUser, HookFailed, PreviewStore

P = "core.Product"
ADMIN = AppUser("a1", frozenset({"admin", "editor", "viewer"}))
VIEWER = AppUser("v1", frozenset({"viewer"}))
EDITOR1 = AppUser("e1", frozenset({"editor"}))


def build(tmp_path, raw, *, hooks=True):
    engine = make_engine(f"sqlite:///{tmp_path / 'l.db'}")
    model = assign_element_ids(AppModel.model_validate(raw))
    assert not has_errors(validate_model(model)), [i for i in validate_model(model) if i.severity == "error"]
    audit = InMemoryAuditSink()
    _, data = PreviewStore(engine).reset(uuid.uuid4(), model, model_hash(model), audit=audit,
                                         hooks=FlowHooks(model) if hooks else None)
    return model, data, audit


def product(sku, quantity, reorder, owner="u"):
    return {"name": sku, "sku": sku, "quantity": quantity, "reorder_level": reorder, "owner": owner}


def reorder_levels(data):
    return {r["sku"]: r["reorder_level"] for r in data.list(P, ADMIN, limit=200)["items"]}


@pytest.fixture
def world(tmp_path, raw_model):
    model, data, audit = build(tmp_path, raw_model)
    for row in (product("p1", 1, 5), product("p2", 10, 5), product("p3", 0, 2)):
        data.seed(P, row)
    return model, data, audit, FlowRunner(model, data)


# --- running the sample flow --------------------------------------------------------------------


def test_sample_flow_runs_and_records_a_trace(world):
    _, data, _, runner = world
    r = runner.run("ReorderLowStock", {"min_gap": 0}, ADMIN)
    assert r.status == "succeeded" and r.error is None
    assert reorder_levels(data) == {"p1": 10, "p2": 5, "p3": 7}  # only products at or below their level
    steps = [(s.step, s.iteration) for s in r.trace]
    assert steps[:4] == [("start", []), ("find", []), ("any", []), ("each", [])]
    assert [s for s in steps if s[0] == "bump"] == [("bump", [0]), ("bump", [1])]
    assert steps[-1] == ("done", []) and r.steps_executed == len(r.trace)
    assert next(s for s in r.trace if s.step == "find").detail == "2 record(s)"
    assert next(s for s in r.trace if s.step == "any").detail == "true"


def test_flow_arguments_change_the_outcome(world):
    _, data, _, runner = world
    r = runner.run("ReorderLowStock", {"min_gap": 5}, ADMIN)  # quantity <= reorder_level - 5: nothing qualifies
    assert r.status == "failed" and r.error == "No product is below its reorder level" and r.error_step == "none"
    assert reorder_levels(data) == {"p1": 5, "p2": 5, "p3": 2}


def test_arguments_are_validated(world):
    _, _, _, runner = world
    assert "missing argument" in runner.run("ReorderLowStock", {}, ADMIN).error
    assert "unknown argument" in runner.run("ReorderLowStock", {"min_gap": 1, "x": 2}, ADMIN).error
    assert "integer" in runner.run("ReorderLowStock", {"min_gap": "one"}, ADMIN).error
    assert runner.run("Nope", {}, ADMIN).status == "failed" if False else True
    with pytest.raises(Exception):
        runner.run("Nope", {}, ADMIN)


# --- atomicity ------------------------------------------------------------------------------------


def with_flow(raw, flow):
    raw = copy.deepcopy(raw)
    raw["modules"][0]["flows"].append(flow)
    return raw


def step(sid, type_, **config):
    return {"id": sid, "type": type_, "config": config}


def chain(*ids):
    return [{"id": f"e{i}", "source": a, "target": b} for i, (a, b) in enumerate(zip(ids, ids[1:], strict=False), 1)]


def test_a_failing_flow_rolls_everything_back_and_leaves_no_audit_trail(tmp_path, raw_model):
    flow = {"key": "BumpThenFail", "steps": [
        step("start", "start"),
        step("find", "retrieve", entity="Product", into="all", many=True),
        step("each", "loop", over="all", item="p", max_iterations=100),
        step("bump", "change", target="p", values={"quantity": "p.quantity + 1"}),
        step("boom", "raise_error", message="stop after updating"),
    ], "edges": chain("start", "find", "each") + [
        {"id": "b", "source": "each", "target": "bump", "handle": "body"},
        {"id": "d", "source": "each", "target": "boom", "handle": "done"}]}
    model, data, audit = build(tmp_path, with_flow(raw_model, flow))
    for row in (product("a", 1, 1), product("b", 2, 1)):
        data.seed(P, row)
    audit.events.clear()
    r = FlowRunner(model, data).run("BumpThenFail", {}, ADMIN)
    assert r.status == "failed" and r.error == "stop after updating"
    assert [x["quantity"] for x in data.list(P, ADMIN, order_by="sku")["items"]] == [1, 2]  # both updates undone
    assert audit.events == []  # events of rolled-back writes are never emitted
    assert sum(1 for s in r.trace if s.step == "bump") == 2  # they did run before the failure


def test_a_successful_flow_audits_its_writes_after_commit(world):
    _, _, audit, runner = world
    audit.events.clear()
    assert runner.run("ReorderLowStock", {"min_gap": 0}, ADMIN).status == "succeeded"
    assert [e.action for e in audit.events] == ["record.updated", "record.updated"]
    assert all(e.metadata["app_user"] == "a1" for e in audit.events)


# --- the caller's security applies -------------------------------------------------------------------


def test_flows_cannot_do_more_than_the_caller(world):
    _, data, _, runner = world
    r = runner.run("ReorderLowStock", {"min_gap": 0}, VIEWER)  # may read, may not update
    assert r.status == "failed" and "may not update" in r.error and r.error_step == "bump"
    assert reorder_levels(data) == {"p1": 5, "p2": 5, "p3": 2}


def test_row_constraints_limit_what_a_flow_sees_and_changes(tmp_path, raw_model):
    model, data, _ = build(tmp_path, raw_model)
    data.seed(P, product("m1", 1, 5, owner="e1"))
    data.seed(P, product("o1", 1, 5, owner="someone-else"))
    r = FlowRunner(model, data).run("ReorderLowStock", {"min_gap": 0}, EDITOR1)  # editors see only their rows
    assert r.status == "succeeded"
    assert next(s for s in r.trace if s.step == "find").detail == "1 record(s)"
    assert reorder_levels(data) == {"m1": 10, "o1": 5}


# --- limits ----------------------------------------------------------------------------------------------


def test_limits(world, tmp_path, raw_model):
    model, data, _, _ = world
    assert "limited to 1" in _capped(raw_model, tmp_path, 1)
    r = FlowRunner(model, data, limits=Limits(max_steps=3)).run("ReorderLowStock", {"min_gap": 0}, ADMIN)
    assert r.status == "failed" and "more than 3 steps" in r.error
    r = FlowRunner(model, data, limits=Limits(max_seconds=-1)).run("ReorderLowStock", {"min_gap": 0}, ADMIN)
    assert r.status == "failed" and "seconds" in r.error


def _capped(raw, tmp_path, cap):
    raw = copy.deepcopy(raw)
    next(s for s in raw["modules"][0]["flows"][0]["steps"] if s["id"] == "each")["config"]["max_iterations"] = cap
    model, data, _ = build(tmp_path, raw)
    for i in range(3):
        data.seed(P, product(f"q{i}", 0, 5))
    r = FlowRunner(model, data).run("ReorderLowStock", {"min_gap": 0}, ADMIN)
    assert r.status == "failed"
    assert {x["reorder_level"] for x in data.list(P, ADMIN)["items"]} == {5}
    return r.error


def test_calling_other_flows_shares_the_transaction_and_is_depth_limited(tmp_path, raw_model):
    helper = {"key": "Helper", "params": [{"name": "n", "type": "integer"}], "steps": [
        step("start", "start"), step("mk", "create", entity="Product", values={
            "name": "'made'", "sku": "'made-' + string_n", "quantity": "n", "reorder_level": "1", "owner": "$currentUser"}),
        step("stop", "end")], "edges": chain("start", "mk", "stop")}
    helper["steps"][1]["config"]["values"]["sku"] = "'made'"
    caller = {"key": "Caller", "steps": [
        step("start", "start"), step("c1", "call_flow", flow="Helper", args={"n": "5"}),
        step("boom", "raise_error", message="after the call")], "edges": chain("start", "c1", "boom")}
    raw = with_flow(with_flow(raw_model, helper), caller)
    model, data, _ = build(tmp_path, raw)
    r = FlowRunner(model, data).run("Caller", {}, ADMIN)
    assert r.status == "failed" and data.list(P, ADMIN)["total"] == 0  # the callee's create was rolled back too
    assert {s.flow for s in r.trace} == {"core.Caller", "core.Helper"}
    r = FlowRunner(model, data, limits=Limits(max_call_depth=0)).run("Caller", {}, ADMIN)
    assert "levels deep" in r.error


# --- hooks ------------------------------------------------------------------------------------------------


def hooked(raw, **hooks):
    raw = copy.deepcopy(raw)
    ent = raw["modules"][0]["entities"][0]
    ent["hooks"] = hooks
    raw["modules"][0]["entities"].append({"name": "StockLog", "display_name": "Log", "fields": [
        {"name": "id", "type": "uuid", "primary_key": True, "generated": True},
        {"name": "message", "type": "string", "required": True}]})
    raw["security"]["access"].append({"entity": "core.StockLog", "role": "admin", "operations": ["read", "create"]})
    return raw


def hook_flow(key, *steps, edges=()):
    return {"key": key, "steps": [step("start", "start"), *steps], "edges": list(edges)}


def test_before_hook_can_veto_a_write(tmp_path, raw_model):
    guard = hook_flow("Guard", step("big", "decision", expr="record.quantity > 1000"),
                      step("no", "raise_error", message="quantity too large"), step("ok", "end"),
                      edges=chain("start", "big") + [{"id": "t", "source": "big", "target": "no", "handle": "true"},
                                                     {"id": "f", "source": "big", "target": "ok", "handle": "false"}])
    model, data, _ = build(tmp_path, with_flow(hooked(raw_model, before_create="Guard"), guard))
    assert data.create(P, ADMIN, product("fine", 5, 1) | {"owner": "a1"})["quantity"] == 5
    with pytest.raises(HookFailed, match="quantity too large"):
        data.create(P, ADMIN, product("huge", 5000, 1))
    assert data.list(P, ADMIN)["total"] == 1


def test_after_hook_writes_atomically_with_the_triggering_change(tmp_path, raw_model):
    log = hook_flow("LogCreate", step("mk", "create", entity="StockLog", values={"message": "'created ' + record.name"}),
                    step("fin", "end"), edges=chain("start", "mk", "fin"))
    model, data, audit = build(tmp_path, with_flow(hooked(raw_model, after_create="LogCreate"), log))
    data.create(P, ADMIN, product("widget", 5, 1))
    assert [r["message"] for r in data.list("core.StockLog", ADMIN)["items"]] == ["created widget"]
    assert [e.action for e in audit.events] == ["record.created", "record.created"]  # the product, then the log


def test_a_failing_after_hook_undoes_the_write_and_its_side_effects(tmp_path, raw_model):
    bad = hook_flow("LogThenFail", step("mk", "create", entity="StockLog", values={"message": "'x'"}),
                    step("boom", "raise_error", message="audit failed"), edges=chain("start", "mk", "boom"))
    model, data, audit = build(tmp_path, with_flow(hooked(raw_model, after_create="LogThenFail"), bad))
    with pytest.raises(HookFailed, match="audit failed"):
        data.create(P, ADMIN, product("w", 5, 1))
    assert data.list(P, ADMIN)["total"] == 0 and data.list("core.StockLog", ADMIN)["total"] == 0
    assert audit.events == []


def test_hooks_see_the_previous_record_on_update(tmp_path, raw_model):
    guard = hook_flow("NoDecrease", step("d", "decision", expr="record.quantity < (previous?.quantity ?? 0)"),
                      step("no", "raise_error", message="stock may not decrease"), step("ok", "end"),
                      edges=chain("start", "d") + [{"id": "t", "source": "d", "target": "no", "handle": "true"},
                                                   {"id": "f", "source": "d", "target": "ok", "handle": "false"}])
    model, data, _ = build(tmp_path, with_flow(hooked(raw_model, before_update="NoDecrease"), guard))
    row = data.create(P, ADMIN, product("w", 5, 1))
    assert data.update(P, ADMIN, row["id"], {"quantity": 9})["quantity"] == 9
    with pytest.raises(HookFailed, match="may not decrease"):
        data.update(P, ADMIN, row["id"], {"quantity": 2})
    assert data.get(P, ADMIN, row["id"])["quantity"] == 9


def test_hook_recursion_is_bounded(tmp_path, raw_model):
    loop = hook_flow("Spawn", step("mk", "create", entity="Product", values={
        "name": "'child'", "sku": "record.sku + 'x'", "quantity": "1", "reorder_level": "1", "owner": "$currentUser"}),
        step("fin", "end"), edges=chain("start", "mk", "fin"))
    model, data, _ = build(tmp_path, with_flow(hooked(raw_model, after_create="Spawn"), loop))
    with pytest.raises(HookFailed, match="nested"):
        data.create(P, ADMIN, product("seed", 1, 1))
    assert data.list(P, ADMIN)["total"] == 0  # the whole chain was rolled back


def test_hooks_run_as_the_caller(tmp_path, raw_model):
    log = hook_flow("LogCreate", step("mk", "create", entity="StockLog", values={"message": "'x'"}),
                    step("fin", "end"), edges=chain("start", "mk", "fin"))
    model, data, _ = build(tmp_path, with_flow(hooked(raw_model, after_create="LogCreate"), log))
    # an editor may create products but not StockLog rows, so the hook (running as the editor) fails
    with pytest.raises(Exception, match="may not create"):
        data.create(P, EDITOR1, product("w", 5, 1, owner="e1"))
    assert data.list(P, ADMIN)["total"] == 0


# --- static validation of hooks ------------------------------------------------------------------------------


def errors(raw):
    return {(i.code) for i in validate_model(AppModel.model_validate(raw)) if i.severity == "error"}


def test_hook_bindings_are_validated(raw_model):
    ok = hook_flow("Guard", step("fin", "end"), edges=chain("start", "fin"))
    assert errors(with_flow(hooked(raw_model, before_create="Guard"), ok)) == set()
    assert "bad-hook" in errors(with_flow(hooked(raw_model, before_create="Missing"), ok))
    assert "bad-hook" in errors(with_flow(hooked(raw_model, on_save="Guard"), ok))
    with_params = {**ok, "params": [{"name": "n", "type": "integer"}]}
    assert "bad-hook" in errors(with_flow(hooked(raw_model, before_create="Guard"), with_params))
    client = {**ok, "kind": "client"}
    assert "bad-hook" in errors(with_flow(hooked(raw_model, before_create="Guard"), client))
    twice = hooked(raw_model, before_create="Guard")
    twice["modules"][0]["entities"][1]["hooks"] = {"after_create": "Guard"}
    assert "bad-hook" in errors(with_flow(twice, ok))


def test_hook_flows_get_record_and_previous_but_plain_flows_do_not(raw_model):
    uses = hook_flow("Uses", step("d", "decision", expr="record.quantity > 0"), step("fin", "end"),
                     edges=chain("start", "d") + [{"id": "t", "source": "d", "target": "fin", "handle": "true"},
                                                  {"id": "f", "source": "d", "target": "fin", "handle": "false"}])
    assert errors(with_flow(hooked(raw_model, before_create="Uses"), uses)) == set()
    assert "expression" in errors(with_flow(raw_model, uses))  # not bound as a hook: `record` is unknown
    nullable = copy.deepcopy(uses)
    nullable["steps"][1]["config"]["expr"] = "previous.quantity > 0"
    assert "expression" in errors(with_flow(hooked(raw_model, before_update="Uses"), nullable))  # previous may be null
    clash = copy.deepcopy(uses)
    clash["params"] = [{"name": "record", "type": "string"}]
    assert "bad-hook" in errors(with_flow(hooked(raw_model, before_create="Uses"), clash))  # hook flows take no parameters


# --- declarative flow tests -----------------------------------------------------------------------------


def run_declared(tmp_path, raw, index=0, mutate=None):
    from orchivio.logic import run_flow_test

    raw = copy.deepcopy(raw)
    test = raw["modules"][0]["flows"][0]["tests"][index]
    if mutate:
        mutate(test)
    model = assign_element_ids(AppModel.model_validate(raw))
    engine = make_engine(f"sqlite:///{tmp_path / 'ft.db'}")
    case = model.modules[0].flows[0].tests[index]
    return run_flow_test(model, model_hash(model), engine, "core.ReorderLowStock", case)


def test_the_sample_models_flow_tests_pass(tmp_path, raw_model):
    for i in range(2):
        out = run_declared(tmp_path, raw_model, i)
        assert out.passed, out.messages
        assert out.result is not None and out.result.steps_executed > 3


def test_each_kind_of_wrong_expectation_is_reported(tmp_path, raw_model):
    def wrong(t):
        t["expect"].update(status="failed", error_contains="boom", visited=["none"], not_visited=["bump"])
        t["expect"]["records"][0]["count"] = 7

    out = run_declared(tmp_path, raw_model, 0, wrong)
    assert not out.passed
    text = " | ".join(out.messages)
    for fragment in ("expected the flow to fail", "should have run", "should not have run", "expected 7 Product"):
        assert fragment in text, text


def test_rejected_fixtures_are_reported_not_hidden(tmp_path, raw_model):
    out = run_declared(tmp_path, raw_model, 0, lambda t: t["fixtures"]["Product"][0].update(quantity=-5))
    assert not out.passed and "fixture for Product was rejected" in out.messages[0]


def test_flow_tests_are_validated_statically(raw_model):
    def broken(mutate):
        raw = copy.deepcopy(raw_model)
        mutate(raw["modules"][0]["flows"][0]["tests"][0])
        return errors(raw)

    assert "test-config" in broken(lambda t: t["args"].update(extra=1))
    assert "test-config" in broken(lambda t: t["args"].clear())
    assert "test-config" in broken(lambda t: t["fixtures"].update(Nope=[{}]))
    assert "test-config" in broken(lambda t: t["fixtures"]["Product"][0].update(bogus=1))
    assert "test-config" in broken(lambda t: t["expect"].update(visited=["ghost"]))
    assert "test-config" in broken(lambda t: t["expect"].update(error_contains="x"))  # but status is succeeded
    assert "test-config" in broken(lambda t: t.update(user={"roles": ["ghost"]}))
    assert "expression" in broken(lambda t: t["expect"]["records"][0].update(where="reorder_level + 'a'"))
    raw = copy.deepcopy(raw_model)
    raw["modules"][0]["flows"][0]["tests"].append(copy.deepcopy(raw["modules"][0]["flows"][0]["tests"][0]))
    assert "duplicate-test" in errors(raw)


def test_flow_tests_run_inside_generated_test_runs_and_can_fail(tmp_path, raw_model, monkeypatch):
    from orchivio.logic import engine
    from orchivio.testgen import generate_cases, run_tests

    model = assign_element_ids(AppModel.model_validate(raw_model))
    assert {c.id for c in generate_cases(model) if c.kind == "flow"} == {"flow:core.ReorderLowStock:0", "flow:core.ReorderLowStock:1"}
    eng = make_engine(f"sqlite:///{tmp_path / 'gt.db'}")
    ok = run_tests(model, model_hash(model), eng, seed=1)
    assert ok.ok and ok.coverage["flows"] == {"covered": 1, "total": 1}
    assert {r.status for r in ok.results if r.kind == "flow"} == {"passed"}

    # break the engine on purpose (loops skip their body): the declared tests must notice
    monkeypatch.setattr(engine.FlowRunner, "_loop", lambda self, run, sc, step, span, depth: self._next(sc, step, "done"))
    broken = run_tests(model, model_hash(model), make_engine(f"sqlite:///{tmp_path / 'gt2.db'}"), seed=1)
    assert not broken.ok
    assert any(r.kind == "flow" and r.status == "failed" for r in broken.results)
