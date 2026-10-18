import copy

import pytest

from orchivio.model import AppModel, assign_element_ids, migrate_to_latest
from orchivio.model.policies import PolicyRule, evaluate, parse_rules
from tests_support import with_process


def model_of(raw):
    return assign_element_ids(AppModel.model_validate(migrate_to_latest(raw)))


def rules(*items):
    return parse_rules(list(items))


def codes(model, *items):
    return [v.rule for v in evaluate(model, rules(*items))]


def test_a_compliant_model_has_no_violations(raw_model):
    m = model_of(raw_model)
    assert evaluate(m, rules({"rule": "no-anonymous-access"}, {"rule": "max-roles", "params": {"max": 10}})) == []


def test_no_anonymous_access(raw_model):
    raw = copy.deepcopy(raw_model)
    raw["security"]["roles"].append("anonymous")
    raw["security"]["access"].append({"entity": "core.Product", "role": "anonymous", "operations": ["read"]})
    raw["security"]["anonymous"] = True
    (v,) = evaluate(model_of(raw), rules({"rule": "no-anonymous-access"}))
    assert v.path == "/security/anonymous" and v.severity == "error"


def test_forbid_operations_by_role_or_for_everyone(raw_model):
    m = model_of(raw_model)
    assert codes(m, {"rule": "forbid-operations", "params": {"operations": ["delete"], "roles": ["admin"]}}) == ["forbid-operations"]
    assert codes(m, {"rule": "forbid-operations", "params": {"operations": ["delete"], "roles": ["viewer"]}}) == []
    assert len(evaluate(m, rules({"rule": "forbid-operations", "params": {"operations": ["read"]}}))) == 3  # roles default to everyone
    assert codes(m, {"rule": "forbid-operations", "params": {"operations": ["export"]}}) == []


def test_require_row_constraint_with_exemptions(raw_model):
    m = model_of(raw_model)
    found = evaluate(m, rules({"rule": "require-row-constraint", "params": {"operations": ["update"], "entities": ["core.Product"], "exempt_roles": ["editor"]}}))
    assert [v.message.split("'")[1] for v in found] == ["admin"]  # the editor has a constraint anyway, and is exempt in any case
    assert evaluate(m, rules({"rule": "require-row-constraint", "params": {"operations": ["read"], "entities": ["core.Other"]}})) == []
    assert len(evaluate(m, rules({"rule": "require-row-constraint", "params": {"operations": ["read"]}}))) == 2  # viewer and admin read everything


def test_max_roles(raw_model):
    m = model_of(raw_model)
    assert codes(m, {"rule": "max-roles", "params": {"max": 2}}) == ["max-roles"]
    assert codes(m, {"rule": "max-roles", "params": {"max": 3}}) == []


def test_integration_destinations(raw_model):
    raw = copy.deepcopy(raw_model)
    raw["modules"][0]["integrations"] = [{"key": "billing", "destination": "billing-api", "operations": [{"id": "ping", "method": "GET", "path": "/ping"}]}]
    m = model_of(raw)
    assert codes(m, {"rule": "integration-destinations", "params": {"allowed": []}}) == ["integration-destinations"]
    assert codes(m, {"rule": "integration-destinations", "params": {"allowed": ["billing-api"]}}) == []
    assert codes(model_of(raw_model), {"rule": "integration-destinations"}) == []


def test_automation_roles_and_flow_exposure(raw_model):
    m = model_of(with_process(raw_model))
    found = evaluate(m, rules({"rule": "forbid-automation-roles", "params": {"roles": ["admin"]}}))
    assert [v.path for v in found] == ["/modules/0/processes/Approval"]  # the process runs its system tasks as admin
    assert codes(m, {"rule": "limit-flow-run-roles", "params": {"roles": ["admin"]}}) == ["limit-flow-run-roles"]  # viewer and editor may run the sample flow
    assert codes(m, {"rule": "limit-flow-run-roles", "params": {"roles": ["viewer", "editor", "admin"]}}) == []


def test_severity_is_carried_and_defaults_to_error(raw_model):
    m = model_of(raw_model)
    (v,) = evaluate(m, rules({"rule": "max-roles", "params": {"max": 1}, "severity": "warning"}))
    assert v.severity == "warning"
    assert parse_rules([{"rule": "max-roles", "params": {"max": 1}}])[0].severity == "error"


@pytest.mark.parametrize("bad, why", [
    ("x", "1-50 rules"), ([], "1-50 rules"), ([{"rule": "nope"}], "invalid rule"), ([{"rule": "max-roles"}], "max must be"),
    ([{"rule": "max-roles", "params": {"max": 0}}], "max must be"), ([{"rule": "max-roles", "params": {"max": True}}], "max must be"),
    ([{"rule": "max-roles", "params": {"max": 2, "extra": 1}}], "unknown parameter"), ([{"rule": "forbid-operations", "params": {}}], "operations must be"),
    ([{"rule": "forbid-operations", "params": {"operations": ["fly"]}}], "operations must be"),
    ([{"rule": "forbid-operations", "params": {"operations": ["read"], "roles": "everyone"}}], "list of names"),
    ([{"rule": "forbid-automation-roles", "params": {}}], "at least one role"), ([{"rule": "integration-destinations", "params": {"allowed": "x"}}], "list of names"),
    ([{"rule": "no-anonymous-access", "params": {"x": 1}}], "unknown parameter"), ([{"rule": "no-anonymous-access", "severity": "fatal"}], "invalid rule"),
    ([{"rule": "no-anonymous-access", "surprise": 1}], "invalid rule"), ([{"rule": "no-anonymous-access"}] * 51, "1-50 rules"),
])
def test_invalid_packs_are_rejected_with_a_reason(bad, why):
    with pytest.raises(ValueError, match=why):
        parse_rules(bad)


def test_rules_roundtrip_through_json(raw_model):
    parsed = rules({"rule": "forbid-operations", "params": {"operations": ["delete"], "roles": "*"}})
    again = [PolicyRule.model_validate(r.model_dump(mode="json")) for r in parsed]
    m = model_of(raw_model)  # one model: element ids are generated per call
    assert evaluate(m, again) == evaluate(m, parsed)
