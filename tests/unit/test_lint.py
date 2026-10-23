import copy
from pathlib import Path

import pytest

from orchivio.model import (
    RULES,
    AppModel,
    assign_element_ids,
    blocking,
    build_refs,
    canonical_json,
    catalog_markdown,
    has_errors,
    lint,
    migrate_to_latest,
    model_hash,
    usages,
    validate_model,
)


def build(raw) -> AppModel:
    return assign_element_ids(AppModel.model_validate(migrate_to_latest(raw)))


def rule_ids(raw):
    return {f.rule_id for f in lint(build(raw)) if not f.suppressed}


def ent(raw):
    return raw["modules"][0]["entities"][0]


def add_entity(d, name, fields, **extra):
    d["modules"][0]["entities"].append({"name": name, "display_name": name, "fields": fields, **extra})


PK = {"name": "id", "type": "uuid", "primary_key": True, "generated": True}


def c_nullable(d):
    ent(d)["fields"].append({"name": "boss", "type": "ref:system.User"})
    d["security"]["access"][1]["row_constraint"] = "record.boss == $currentUser"


def c_write_without_read(d):
    d["security"]["roles"].append("clerk")
    d["security"]["access"].append({"entity": "core.Product", "role": "clerk", "operations": ["create"]})


def c_sensitive(d):
    ent(d)["fields"].append({"name": "api_key", "type": "string"})
    d["modules"][0]["pages"][0]["columns"].append("api_key")


def c_unindexed(d):
    ent(d)["fields"].append({"name": "region", "type": "string", "required": True})
    d["security"]["access"][1]["row_constraint"] = "record.region == 'eu'"


def c_ref_noindex(d):
    add_entity(d, "Order", [PK, {"name": "product", "type": "ref:Product"}])
    d["security"]["access"].append({"entity": "core.Order", "role": "admin", "operations": ["read"]})


def c_wide_list(d):
    ent(d)["fields"].extend({"name": f"c{i}", "type": "string"} for i in range(13))
    d["modules"][0]["pages"][0]["columns"].extend(f"c{i}" for i in range(13))


def c_orphan(d):
    add_entity(d, "Orphan", [PK])
    d["security"]["access"].append({"entity": "core.Orphan", "role": "admin", "operations": ["read"]})


def c_transient(d):
    add_entity(d, "Form", [{"name": "x", "type": "string"}], transient=True)
    d["security"]["access"].append({"entity": "core.Form", "role": "admin", "operations": ["read"]})


def c_no_timestamp(d):
    ent(d)["fields"] = [f for f in ent(d)["fields"] if f["name"] != "created_at"]


def c_dup_list(d):
    d["modules"][0]["pages"].append(
        {"key": "product-list-2", "path": "/products2", "type": "list", "entity": "Product", "columns": ["name"]})


def c_unreachable_flow(d):
    d["modules"][0]["flows"].append({
        "key": "UnusedFlow",
        "kind": "server",
        "steps": [{"id": "s1", "type": "start", "config": {}},
                  {"id": "s2", "type": "end", "config": {}}],
        "edges": [{"id": "e1", "source": "s1", "target": "s2"}],
    })


def c_process_no_start_roles(d):
    d["modules"][0].setdefault("processes", []).append({
        "key": "OrphanProcess",
        "steps": [{"id": "s1", "type": "start", "config": {}},
                  {"id": "s2", "type": "end", "config": {}}],
        "edges": [{"id": "e1", "source": "s1", "target": "s2"}],
    })


def c_hook_on_transient(d):
    d["modules"][0]["flows"].append({
        "key": "HookFlow",
        "kind": "server",
        "steps": [{"id": "s1", "type": "start", "config": {}},
                  {"id": "s2", "type": "end", "config": {}}],
        "edges": [{"id": "e1", "source": "s1", "target": "s2"}],
    })
    d["modules"][0]["entities"].append({
        "name": "TransientForm",
        "fields": [{"name": "x", "type": "string"}],
        "transient": True,
        "hooks": {"after_create": "HookFlow"},
    })


def c_integration_no_ops(d):
    d["modules"][0].setdefault("integrations", []).append({
        "key": "empty_api",
        "destination": "empty-api",
    })


def c_sensitive_detail(d):
    ent(d)["fields"].append({"name": "secret_key", "type": "string"})
    d["modules"][0]["pages"].append({
        "key": "product-sensitive-detail",
        "path": "/products/:id/detail",
        "type": "detail",
        "entity": "Product",
        "columns": ["secret_key"],
    })


def c_sensitive_form(d):
    ent(d)["fields"].append({"name": "password_hash", "type": "string"})
    d["modules"][0]["pages"].append({
        "key": "product-sensitive-edit",
        "path": "/products/:id/edit",
        "type": "form",
        "entity": "Product",
        "mode": "edit",
        "columns": ["password_hash"],
    })


# rule id -> mutation that triggers it (None: the sample model already triggers it)
CASES = {
    "sec-constraint-not-compilable": lambda d: d["security"]["access"][1].update(row_constraint="record.quantity / 2 > 1"),
    "sec-constraint-on-nullable-field": c_nullable,
    "sec-unconstrained-write": None,
    "sec-write-without-read": c_write_without_read,
    "sec-anonymous-access": lambda d: d["security"].update(anonymous=True),
    "sec-superuser-role": None,
    "sec-sensitive-field-listed": c_sensitive,
    "sec-sensitive-field-in-detail": c_sensitive_detail,
    "sec-sensitive-field-in-form": c_sensitive_form,
    "sec-sensitive-field-default": lambda d: ent(d)["fields"].append(
        {"name": "api_key", "type": "string", "default": "sk-dev-placeholder"}),
    "perf-unindexed-constraint-field": c_unindexed,
    "perf-reference-without-index": c_ref_noindex,
    "perf-unique-nullable-field": lambda d: ent(d)["fields"].append({"name": "slug", "type": "string", "unique": True}),
    "perf-wide-entity": lambda d: ent(d)["fields"].extend({"name": f"f{i}", "type": "string"} for i in range(40)),
    "perf-wide-list-page": c_wide_list,
    "maint-unused-enumeration": lambda d: d["modules"][0]["enumerations"].append({"name": "Unused", "values": ["a", "b"]}),
    "maint-unused-entity": c_orphan,
    "maint-missing-display-name": lambda d: ent(d).pop("display_name"),
    "maint-single-value-enumeration": lambda d: d["modules"][0]["enumerations"].append({"name": "One", "values": ["only"]}),
    "maint-duplicate-list-page": c_dup_list,
    "maint-no-creation-timestamp": c_no_timestamp,
    "maint-long-expression": lambda d: d["security"]["access"][1].update(
        row_constraint="record.owner == $currentUser" + " && true" * 60),
    "maint-access-on-transient-entity": c_transient,
    "maint-unreachable-flow": c_unreachable_flow,
    "maint-process-no-start-roles": c_process_no_start_roles,
    "maint-hook-on-transient-entity": c_hook_on_transient,
    "maint-integration-no-operations": c_integration_no_ops,
    "maint-integration-no-description": lambda d: d["modules"][0].setdefault("integrations", []).append({
        "key": "undescribed_api", "destination": "undescribed-api",
        "operations": [{"id": "ping", "method": "GET", "path": "/ping"}],
    }),
    "maint-list-no-columns": lambda d: d["modules"][0]["pages"].append(
        {"key": "product-detail", "path": "/products/:id/view", "type": "detail", "entity": "Product"}),
    "maint-form-missing-required-field": lambda d: (
        d["modules"][0]["pages"].append({
            "key": "product-partial-create",
            "path": "/products/new2",
            "type": "form",
            "entity": "Product",
            "mode": "create",
            "columns": ["name"],
        }) or
        d["security"]["pages"].update({"product-partial-create": ["admin"]})
    ),
    "maint-flow-no-tests": lambda d: d["modules"][0]["flows"].append({
        "key": "UncoveredFlow",
        "kind": "server",
        "run_roles": ["admin"],
        "steps": [{"id": "s1", "type": "start", "config": {}}, {"id": "s2", "type": "end", "config": {}}],
        "edges": [{"id": "e1", "source": "s1", "target": "s2"}],
    }),
    "a11y-column-without-label": None,
    "a11y-entity-no-text-field": lambda d: (
        d["modules"][0]["entities"].append({
            "name": "NumericOnly",
            "display_name": "Numeric Only",
            "fields": [
                {"name": "id", "type": "uuid", "primary_key": True, "generated": True},
                {"name": "value", "type": "integer", "required": True},
                {"name": "rate", "type": "decimal", "required": True},
            ],
        }) or
        d["security"]["access"].append({"entity": "core.NumericOnly", "role": "admin", "operations": ["read"]}) or
        d["modules"][0]["pages"].append({
            "key": "numeric-list",
            "path": "/numeric",
            "type": "list",
            "entity": "NumericOnly",
            "columns": ["value", "rate"],
        }) or
        d["security"]["pages"].update({"numeric-list": ["admin"]})
    ),
    "i18n-default-locale-not-listed": lambda d: d["application"].update(default_locale="fr-FR"),
}


def test_catalog_has_at_least_twenty_unique_rules():
    ids = [r.id for r in RULES]
    assert len(ids) >= 20 and len(set(ids)) == len(ids)
    assert {r.category for r in RULES} == {"security", "performance", "maintainability", "accessibility"}


def test_catalog_doc_is_up_to_date():
    doc = (Path(__file__).resolve().parents[2] / "docs" / "LINT-RULES.md").read_text(encoding="utf-8")
    assert doc.replace("\r\n", "\n") == catalog_markdown()


def test_every_rule_has_a_case():
    assert set(CASES) == {r.id for r in RULES}


def test_example_model_has_no_blocking_findings(raw_model):
    assert blocking(lint(build(raw_model))) == []


@pytest.mark.parametrize("rule_id", sorted(CASES))
def test_rule_fires(raw_model, rule_id):
    raw = copy.deepcopy(raw_model)
    if CASES[rule_id]:
        CASES[rule_id](raw)
    assert not has_errors(validate_model(build(raw))), "case must be a valid model"
    assert rule_id in rule_ids(raw)


def test_a_clean_variant_has_no_findings(raw_model):
    raw = copy.deepcopy(raw_model)
    for f in ent(raw)["fields"]:
        f.setdefault("display_name", f["name"].title())
    next(r for r in raw["security"]["access"] if r["role"] == "admin")["row_constraint"] = "record.owner == $currentUser"
    assert rule_ids(raw) == set()


def test_suppression_needs_justification_and_a_known_element(raw_model):
    raw = copy.deepcopy(raw_model)
    raw["security"]["access"][1]["row_constraint"] = "record.quantity / 2 > 1"
    m = build(raw)
    rule = next(r for r in m.security.access if r.row_constraint)
    assert [f.rule_id for f in blocking(lint(m))] == ["sec-constraint-not-compilable"]

    raw["lint_suppressions"] = [
        {"rule": "sec-constraint-not-compilable", "element_id": rule.element_id, "justification": "short"}]
    with pytest.raises(ValueError):
        AppModel.model_validate(raw)

    raw["lint_suppressions"][0]["justification"] = "Handled by the stored procedure in release 2"
    m2 = assign_element_ids(AppModel.model_validate(raw), previous=m)
    assert blocking(lint(m2)) == []
    f = next(f for f in lint(m2) if f.rule_id == "sec-constraint-not-compilable")
    assert f.suppressed and "stored procedure" in f.justification

    raw["lint_suppressions"][0]["element_id"] = "00000000-0000-4000-8000-000000000000"
    assert "unknown-suppression-element" in {i.code for i in validate_model(AppModel.model_validate(raw))}


def test_empty_suppressions_do_not_change_the_hash(raw_model):
    m = build(raw_model)
    assert "lint_suppressions" not in canonical_json(m)
    assert model_hash(m) == model_hash(m.model_copy(deep=True))


# --- dependency graph -------------------------------------------------------------------


def test_refs_cover_types_expressions_pages_and_rules(raw_model):
    m = build(raw_model)
    ent_ = m.modules[0].entities[0]
    f = {x.name: x for x in ent_.fields}
    enum = m.modules[0].enumerations[0]
    edges = {(r.from_id, r.to_id, r.kind) for r in build_refs(m)}
    assert (f["status"].element_id, enum.element_id, "field-enum") in edges
    assert (f["status"].element_id, enum.element_id, "expr-enum") in edges
    assert (f["status"].element_id, f["quantity"].element_id, "expr-field") in edges
    page = m.modules[0].pages[0]
    assert (page.element_id, ent_.element_id, "page-entity") in edges
    assert (page.element_id, f["sku"].element_id, "page-column") in edges
    rule = next(r for r in m.security.access if r.row_constraint)
    assert (rule.element_id, f["owner"].element_id, "expr-field") in edges
    assert (rule.element_id, ent_.element_id, "access-entity") in edges


def test_usages_answer_what_breaks_if_i_remove_this(raw_model):
    m = build(raw_model)
    f = {x.name: x for x in m.modules[0].entities[0].fields}
    users = {r.from_id for r in usages(build_refs(m), f["quantity"].element_id)}
    assert f["status"].element_id in users and m.modules[0].pages[0].element_id in users
    assert usages(build_refs(m), "00000000-0000-4000-8000-000000000000") == []
