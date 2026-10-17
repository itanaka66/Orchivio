import copy

import pytest

from orchivio.model import (
    AppModel,
    assign_element_ids,
    build_refs,
    diff_models,
    has_errors,
    lint,
    usages,
    validate_flow,
    validate_model,
)
from orchivio.model.schema import FlowDef


def flow_of(raw):
    return raw["modules"][0]["flows"][0]


def codes(raw, severity=None):
    issues = validate_model(AppModel.model_validate(raw))
    return {i.code for i in issues if severity is None or i.severity == severity}


def step(raw, sid):
    return next(s for s in flow_of(raw)["steps"] if s["id"] == sid)


def edge(raw, eid):
    return next(e for e in flow_of(raw)["edges"] if e["id"] == eid)


def test_sample_flow_is_valid(raw_model):
    assert codes(raw_model) == set()


@pytest.mark.parametrize("name,mutate,code", [
    ("no start", lambda r: flow_of(r).update(steps=[s for s in flow_of(r)["steps"] if s["id"] != "start"]), "flow-start"),
    ("two starts", lambda r: flow_of(r)["steps"].append({"id": "s2", "type": "start"}), "flow-start"),
    ("duplicate step id", lambda r: flow_of(r)["steps"].append({"id": "find", "type": "end"}), "duplicate-step"),
    ("duplicate edge id", lambda r: edge(r, "e2").update(id="e1"), "duplicate-edge"),
    ("unknown step", lambda r: edge(r, "e2").update(target="ghost"), "edge-unknown-step"),
    ("self loop", lambda r: edge(r, "e2").update(target="find"), "edge-self-loop"),
    ("bad handle", lambda r: edge(r, "e2").update(handle="true"), "edge-bad-handle"),
    ("into start", lambda r: flow_of(r)["edges"].append({"id": "e9", "source": "bump", "target": "start"}), "edge-into-start"),
    ("missing branch", lambda r: flow_of(r).update(edges=[e for e in flow_of(r)["edges"] if e["id"] != "e4"]), "missing-branch"),
    ("two edges one output", lambda r: flow_of(r)["edges"].append({"id": "e9", "source": "find", "target": "none"}), "multiple-edges"),
    ("unreachable", lambda r: flow_of(r)["steps"].append({"id": "lost", "type": "end"}), "unreachable-step"),
    ("start unconnected", lambda r: flow_of(r).update(edges=[e for e in flow_of(r)["edges"] if e["id"] != "e1"]), "start-unconnected"),
])
def test_structure_rules(raw_model, name, mutate, code):
    raw = copy.deepcopy(raw_model)
    mutate(raw)
    assert code in codes(raw), name


def test_cycles_are_rejected_and_loops_are_the_way_to_repeat(raw_model):
    raw = copy.deepcopy(raw_model)
    flow_of(raw)["steps"].append({"id": "again", "type": "decision", "config": {"expr": "true"}})
    flow_of(raw)["edges"] += [
        {"id": "e7", "source": "bump", "target": "again"},
        {"id": "e8", "source": "again", "target": "find", "handle": "true"},
        {"id": "e9", "source": "again", "target": "done", "handle": "false"},
    ]
    assert "flow-cycle" in codes(raw)


def test_loops_must_be_bounded_and_keep_their_body_separate(raw_model):
    for bad in (None, 0, 1001, "10", True):
        raw = copy.deepcopy(raw_model)
        cfg = step(raw, "each")["config"]
        cfg.pop("max_iterations")
        if bad is not None:
            cfg["max_iterations"] = bad
        assert "loop-unbounded" in codes(raw), bad
    raw = copy.deepcopy(raw_model)
    flow_of(raw)["edges"].append({"id": "e9", "source": "bump", "target": "done"})
    assert "loop-body-escapes" in codes(raw)


def test_variables_are_scoped_to_where_they_are_defined(raw_model):
    raw = copy.deepcopy(raw_model)
    step(raw, "any")["config"]["expr"] = "size(low) > 0 && product.reorder_level > 0"  # product only exists in the loop
    assert "expression" in codes(raw)
    raw = copy.deepcopy(raw_model)
    step(raw, "find")["config"]["where"] = "size(low) > 0"  # low is defined by this very step
    assert "expression" in codes(raw)
    raw = copy.deepcopy(raw_model)
    step(raw, "bump")["config"]["values"] = {"reorder_level": "product.reorder_level + min_gap"}  # a parameter
    assert codes(raw) == set()


def test_duplicate_variables_and_bad_targets(raw_model):
    raw = copy.deepcopy(raw_model)
    step(raw, "each")["config"]["item"] = "low"
    assert "duplicate-variable" in codes(raw)
    raw = copy.deepcopy(raw_model)
    step(raw, "each")["config"]["item"] = "min_gap"
    assert "duplicate-variable" in codes(raw)
    raw = copy.deepcopy(raw_model)
    step(raw, "bump")["config"]["target"] = "low"  # a list, not a record
    assert "config" in codes(raw)
    raw = copy.deepcopy(raw_model)
    step(raw, "each")["config"]["over"] = "min_gap"
    assert "config" in codes(raw)


def test_step_configuration_is_checked(raw_model):
    cases = [
        ("find", {"entity": "Nope"}), ("find", {"where": "quantity + 'a'"}), ("find", {"limit": 0}),
        ("find", {"extra": 1}), ("any", {"expr": "quantity"}), ("any", {"expr": "size(low)"}),
        ("none", {"message": ""}), ("bump", {"values": {"id": "'x'"}}), ("bump", {"values": {"nope": "1"}}),
        ("bump", {"values": {"reorder_level": "'text'"}}), ("find", {"into": "Bad Name"}),
    ]
    for sid, patch in cases:
        raw = copy.deepcopy(raw_model)
        step(raw, sid)["config"].update(patch)
        assert has_errors(validate_model(AppModel.model_validate(raw))), (sid, patch)


def test_create_requires_all_required_fields_and_types(raw_model):
    raw = copy.deepcopy(raw_model)
    flow_of(raw)["steps"].append({"id": "mk", "type": "create", "config": {
        "entity": "Product", "values": {"name": "'N'", "sku": "'S'", "quantity": "1"}, "into": "made"}})
    flow_of(raw)["edges"].append({"id": "e9", "source": "bump", "target": "mk"})
    issues = validate_model(AppModel.model_validate(raw))
    assert any("reorder_level" in i.message for i in issues)  # required field missing
    step(raw, "mk")["config"]["values"]["reorder_level"] = "min_gap"
    step(raw, "mk")["config"]["values"]["owner"] = "$currentUser"
    assert not has_errors(validate_model(AppModel.model_validate(raw)))


def test_client_flows_cannot_touch_data(raw_model):
    raw = copy.deepcopy(raw_model)
    flow_of(raw)["kind"] = "client"
    assert "step-not-allowed" in codes(raw)


def test_flow_calls(raw_model):
    raw = copy.deepcopy(raw_model)
    raw["modules"][0]["flows"].append({"key": "Helper", "kind": "server", "params": [{"name": "n", "type": "integer"}],
                                       "steps": [{"id": "start", "type": "start"}, {"id": "stop", "type": "end"}],
                                       "edges": [{"id": "e1", "source": "start", "target": "stop"}]})
    flow_of(raw)["steps"].append({"id": "help", "type": "call_flow", "config": {"flow": "Helper", "args": {"n": "min_gap"}}})
    flow_of(raw)["edges"] = [e for e in flow_of(raw)["edges"] if e["id"] != "e6"] + [
        {"id": "e6", "source": "each", "target": "help", "handle": "done"}, {"id": "e7", "source": "help", "target": "done"}]
    assert codes(raw) == set()
    for patch, expected in [({"flow": "Nope"}, "config"), ({"args": {}}, "config"), ({"args": {"n": "'x'"}}, "expression"),
                            ({"args": {"n": "1", "z": "2"}}, "config")]:
        bad = copy.deepcopy(raw)
        step(bad, "help")["config"].update(patch)
        assert expected in codes(bad), patch
    cyc = copy.deepcopy(raw)  # Helper calls ReorderLowStock which calls Helper
    cyc["modules"][0]["flows"][1]["steps"].insert(1, {"id": "back", "type": "call_flow",
                                                      "config": {"flow": "ReorderLowStock", "args": {"min_gap": "n"}}})
    cyc["modules"][0]["flows"][1]["edges"] = [{"id": "e1", "source": "start", "target": "back"},
                                              {"id": "e2", "source": "back", "target": "stop"}]
    assert "flow-call-cycle" in codes(cyc)
    srv = copy.deepcopy(raw)
    srv["modules"][0]["flows"][1]["kind"] = "client"
    assert "config" in codes(srv)  # server flows may not call client flows


def test_duplicate_flow_keys(raw_model):
    raw = copy.deepcopy(raw_model)
    raw["modules"][0]["flows"].append(copy.deepcopy(flow_of(raw)))
    assert "duplicate-flow" in codes(raw)


def test_unknown_step_settings_and_extra_properties(raw_model):
    raw = copy.deepcopy(raw_model)
    flow_of(raw)["steps"][0]["bogus"] = 1
    with pytest.raises(ValueError):
        AppModel.model_validate(raw)


# --- diff, refs, lint ---------------------------------------------------------------------------


def test_layout_changes_are_not_model_changes(raw_model):
    base = assign_element_ids(AppModel.model_validate(raw_model))
    moved = base.model_copy(deep=True)
    for s in moved.modules[0].flows[0].steps:
        s.position = None if s.position is None else s.position.model_copy(update={"x": s.position.x + 40})
    assert diff_models(base, moved).is_empty
    edited = base.model_copy(deep=True)
    edited.modules[0].flows[0].steps[1].config = {**edited.modules[0].flows[0].steps[1].config, "limit": 5}
    d = diff_models(base, edited)
    assert [(c.element_type, c.kind, c.properties) for c in d.changes] == [("flow", "modified", ("steps",))]


def test_flows_appear_in_the_dependency_graph(raw_model):
    m = assign_element_ids(AppModel.model_validate(raw_model))
    flow, entity = m.modules[0].flows[0], m.modules[0].entities[0]
    assert flow.element_id in {r.from_id for r in usages(build_refs(m), entity.element_id)}
    assert not [f for f in lint(m) if f.rule_id == "maint-unused-entity"]


# --- validate_flow (live editor feedback) ---------------------------------------------------------


def test_validate_flow_reports_paths_by_step_id(raw_model):
    m = assign_element_ids(AppModel.model_validate(raw_model))
    draft = copy.deepcopy(flow_of(raw_model))
    next(s for s in draft["steps"] if s["id"] == "any")["config"]["expr"] = "size(low)"
    issues = validate_flow(m, "core", FlowDef.model_validate(draft))
    assert [(i.code, i.path) for i in issues] == [("expression", "/modules/0/flows/ReorderLowStock/steps/any/config/expr")]
    with pytest.raises(KeyError):
        validate_flow(m, "nope", FlowDef.model_validate(draft))


def test_validate_flow_accepts_a_brand_new_flow(raw_model):
    m = assign_element_ids(AppModel.model_validate(raw_model))
    new = FlowDef.model_validate({"key": "Draft", "steps": [{"id": "start", "type": "start"}], "edges": []})
    assert {i.code for i in validate_flow(m, "core", new)} == {"start-unconnected"}
