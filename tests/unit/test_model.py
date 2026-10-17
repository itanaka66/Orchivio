import copy

import pytest
from conftest import load_yaml
from pydantic import ValidationError

from orchivio.model import (
    AppModel,
    MissingElementId,
    UnsupportedSchemaVersion,
    assign_element_ids,
    canonical_json,
    diff_models,
    has_errors,
    migrate_to_latest,
    model_hash,
    validate_model,
)
from orchivio.policy import ApprovalLevel


def codes(issues):
    return {i.code for i in issues}


def mutated(raw, fn):
    data = copy.deepcopy(raw)
    fn(data)
    return AppModel.model_validate(data)


# --- schema / migration ----------------------------------------------------


def test_example_model_is_valid(model):
    issues = validate_model(model)
    assert [i for i in issues if i.severity == "error"] == []


def test_schema_1_0_migrates_and_validates():
    migrated = migrate_to_latest(load_yaml("model-1.0.yaml"))
    m = AppModel.model_validate(migrated)
    assert not has_errors(validate_model(m))
    assert m.modules[0].key == "core"
    assert m.security.roles == ["admin", "editor", "viewer"]
    rule = next(r for r in m.security.access if r.role == "editor")
    assert rule.operations == ["read", "create", "update"]
    assert m.security.pages["product-create"] == ["admin", "editor", "viewer"]


def test_unknown_schema_version_rejected():
    with pytest.raises(UnsupportedSchemaVersion):
        migrate_to_latest({"schema_version": "9.9"})


def test_unknown_properties_are_rejected(raw_model):
    raw_model["modules"][0]["entities"][0]["fields"][1]["bogus"] = 1
    with pytest.raises(ValidationError):
        AppModel.model_validate(raw_model)


def test_hash_is_stable_and_order_insensitive(model):
    assert model_hash(model) == model_hash(model.model_copy(deep=True))
    assert canonical_json(model) == canonical_json(model.model_dump(mode="json", exclude_none=True))
    changed = model.model_copy(deep=True)
    changed.application.name = "Other"
    assert model_hash(changed) != model_hash(model)


# --- validation ------------------------------------------------------------


@pytest.mark.parametrize(
    "edit,code",
    [
        (lambda d: d["modules"][0]["entities"].append(d["modules"][0]["entities"][0]), "duplicate-entity"),
        (lambda d: d["modules"][0]["entities"][0]["fields"].append({"name": "sku", "type": "string"}), "duplicate-field"),
        (lambda d: d["modules"][0]["entities"][0]["fields"][1].update(type="strng"), "bad-type"),
        (lambda d: d["modules"][0]["entities"][0]["fields"][1].update(type="enum:Nope"), "unknown-enum"),
        (lambda d: d["modules"][0]["entities"][0]["fields"][1].update(type="ref:Nope"), "unknown-entity"),
        (lambda d: d["modules"][0]["entities"][0]["fields"][0].update(primary_key=False), "no-primary-key"),
        (lambda d: d["modules"][0]["entities"][0]["fields"][3].update(max_length=5), "bad-constraint"),
        (lambda d: d["modules"][0]["entities"][0]["fields"][3].update(default="x"), "bad-default"),
        (lambda d: d["modules"][0]["entities"][0]["indexes"].append({"fields": ["zzz"]}), "unknown-index-field"),
        (lambda d: d["modules"][0]["pages"][0].update(entity="Nope"), "unknown-entity"),
        (lambda d: d["modules"][0]["pages"][0].update(columns=["zzz"]), "unknown-column"),
        (lambda d: d["modules"][0]["pages"][1].pop("mode"), "missing-mode"),
        (lambda d: d["modules"][0]["pages"][1].update(path="/products"), "duplicate-route"),
        (lambda d: d["security"]["roles"].append("admin"), "duplicate-role"),
        (lambda d: d["security"]["access"][0].update(role="ghost"), "unknown-role"),
        (lambda d: d["security"]["access"][0].update(entity="core.Ghost"), "unknown-entity"),
        (lambda d: d["security"].update(access=[]), "no-access-rule"),
        (lambda d: d["security"]["pages"].update(ghost=["admin"]), "unknown-page"),
        (lambda d: d["security"]["access"].append(dict(d["security"]["access"][0])), "duplicate-access"),
    ],
)
def test_validation_rules(raw_model, edit, code):
    assert code in codes(validate_model(mutated(raw_model, edit)))


def test_expression_errors_are_reported_with_location(raw_model):
    def edit(d):
        d["security"]["access"][1]["row_constraint"] = "record.owner == 'x'"
        d["modules"][0]["entities"][0]["fields"][5]["computed"]["expr"] = "quantity + 'a'"

    issues = [i for i in validate_model(mutated(raw_model, edit)) if i.code == "expression"]
    paths = {i.path for i in issues}
    assert "/security/access/1/row_constraint" in paths
    assert "/modules/0/entities/0/fields/5/computed/expr" in paths


def test_computed_cycle_detected(raw_model):
    def edit(d):
        fields = d["modules"][0]["entities"][0]["fields"]
        fields.append({"name": "a", "type": "integer", "computed": {"expr": "b + 1"}})
        fields.append({"name": "b", "type": "integer", "computed": {"expr": "a + 1"}})

    issues = validate_model(mutated(raw_model, edit))
    cyc = [i for i in issues if i.code == "computed-cycle"]
    assert cyc and "a -> b -> a" in cyc[0].message


def test_nullable_computed_result_needs_coalesce(raw_model):
    def edit(d):
        fields = d["modules"][0]["entities"][0]["fields"]
        fields.append({"name": "note", "type": "string"})
        fields.append({"name": "shout", "type": "string", "computed": {"expr": "note.upper()"}})

    assert "expression" in codes(validate_model(mutated(raw_model, edit)))


def test_computed_cannot_be_unique_or_defaulted(raw_model):
    def edit(d):
        d["modules"][0]["entities"][0]["fields"][5]["unique"] = True

    assert "bad-computed" in codes(validate_model(mutated(raw_model, edit)))


# --- element ids and diff --------------------------------------------------


def test_assign_ids_is_complete_and_unique(model):
    m = assign_element_ids(model)
    from orchivio.model import iter_elements

    ids = [e.element_id for _, e in iter_elements(m)]
    assert all(ids) and len(set(ids)) == len(ids)


def test_assign_ids_inherits_from_previous(model):
    first = assign_element_ids(model)
    again = assign_element_ids(model, previous=first)
    assert canonical_json(first) == canonical_json(again)


def test_diff_requires_ids(model):
    with pytest.raises(MissingElementId):
        diff_models(model, model)


def test_diff_identical_is_empty(model):
    m = assign_element_ids(model)
    d = diff_models(m, m.model_copy(deep=True))
    assert d.is_empty and d.level == ApprovalLevel.A0


def _field(m, name):
    return next(f for f in m.modules[0].entities[0].fields if f.name == name)


def test_diff_rename_keeps_identity_and_is_flagged(model):
    base = assign_element_ids(model)
    new = base.model_copy(deep=True)
    _field(new, "sku").name = "code"
    d = diff_models(base, new)
    assert [(c.kind, c.element_type, c.properties) for c in d.changes] == [("modified", "field", ("name",))]
    assert d.changes[0].risks == ("rename",)


def test_diff_destructive_and_type_changes_need_a3(model):
    base = assign_element_ids(model)
    new = base.model_copy(deep=True)
    ent = new.modules[0].entities[0]
    ent.fields = [f for f in ent.fields if f.name != "reorder_level"]
    _field(new, "quantity").type = "decimal"
    d = diff_models(base, new)
    risks = {r for c in d.changes for r in c.risks}
    assert {"destructive", "type-change"} <= risks
    assert d.level == ApprovalLevel.A3


def test_diff_enum_value_removal_is_risky(model):
    base = assign_element_ids(model)
    new = base.model_copy(deep=True)
    new.modules[0].enumerations[0].values = ["in_stock", "low"]
    d = diff_models(base, new)
    assert d.changes[0].risks == ("enum-value-removed",)


def test_diff_permission_expansion_and_grants_need_a3(model):
    base = assign_element_ids(model)
    new = base.model_copy(deep=True)
    editor = next(r for r in new.security.access if r.role == "editor")
    editor.row_constraint = None
    d = diff_models(base, new)
    assert d.changes[0].risks == ("permission-expansion",)
    assert d.level == ApprovalLevel.A3 and d.security_changed

    new2 = base.model_copy(deep=True)
    new2.security.access.append(
        type(new2.security.access[0])(entity="core.Product", role="auditor", operations=["read"])
    )
    new2.security.roles.append("auditor")
    new2 = assign_element_ids(new2, previous=base)
    d2 = diff_models(base, new2)
    assert any("permission-grant" in c.risks for c in d2.changes)


def test_diff_anonymous_opt_in_is_flagged(model):
    base = assign_element_ids(model)
    new = base.model_copy(deep=True)
    new.security.anonymous = True
    d = diff_models(base, new)
    assert d.changes[0].risks == ("anonymous-access",)
    assert d.level == ApprovalLevel.A3


def test_diff_revoking_access_is_not_risky(model):
    base = assign_element_ids(model)
    new = base.model_copy(deep=True)
    new.security.access = [r for r in new.security.access if r.role != "viewer"]
    d = diff_models(base, new)
    assert d.changes[0].kind == "removed" and d.changes[0].risks == ()
