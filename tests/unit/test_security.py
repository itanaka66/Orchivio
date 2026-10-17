import sqlite3

from orchivio.expr import EvalContext, SqlContext, UserRef
from orchivio.policy import PlatformRole, is_allowed, level_for_action
from orchivio.policy import ApprovalLevel as L
from orchivio.security import (
    check_consistency,
    record_allowed,
    resolve_access,
    row_predicate,
)

P = "core.Product"


def test_unknown_role_entity_or_operation_is_denied(model):
    assert not resolve_access(model, ["ghost"], P, "read").allowed
    assert not resolve_access(model, ["admin"], "core.Nope", "read").allowed
    assert not resolve_access(model, ["admin"], P, "truncate").allowed
    assert not resolve_access(model, [], P, "read").allowed


def test_operations_are_granted_per_role(model):
    assert resolve_access(model, ["viewer"], P, "read").allowed
    assert not resolve_access(model, ["viewer"], P, "update").allowed
    assert not resolve_access(model, ["editor"], P, "delete").allowed
    assert resolve_access(model, ["admin"], P, "delete").allowed


def test_union_semantics_unconstrained_rule_wins(model):
    d = resolve_access(model, ["editor", "viewer"], P, "read")
    assert d.allowed and d.unrestricted  # viewer read has no constraint
    d = resolve_access(model, ["editor"], P, "update")
    assert d.allowed and not d.unrestricted and len(d.constraints) == 1


def test_row_predicate_filters_rows_in_sql(model):
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE p (id INT, owner TEXT)")
    db.executemany("INSERT INTO p VALUES (?, ?)", [(1, "u1"), (2, "u2"), (3, "u1")])
    user = UserRef("u1", {"editor"})

    pred = row_predicate(resolve_access(model, ["editor"], P, "update"), SqlContext(current_user=user, dialect="sqlite"))
    rows = db.execute(f"SELECT id FROM p WHERE {pred.sql} ORDER BY id", pred.params).fetchall()
    assert rows == [(1,), (3,)]

    deny = row_predicate(resolve_access(model, ["editor"], P, "delete"), SqlContext(current_user=user))
    assert db.execute(f"SELECT count(*) FROM p WHERE {deny.sql}").fetchone() == (0,)

    allow = row_predicate(resolve_access(model, ["admin"], P, "read"), SqlContext(current_user=user))
    assert db.execute(f"SELECT count(*) FROM p WHERE {allow.sql}").fetchone() == (3,)


def test_multiple_constrained_rules_are_or_combined(model):
    m = model.model_copy(deep=True)
    m.security.roles.append("auditor")
    rule = m.security.access[1].model_copy(update={"role": "auditor", "element_id": None, "row_constraint": "record.quantity > 100"})
    m.security.access.append(rule)
    d = resolve_access(m, ["editor", "auditor"], P, "update")
    pred = row_predicate(d, SqlContext(current_user=UserRef("u1"), dialect="sqlite"))
    assert pred.sql.count(" OR ") == 1 and len(pred.params) == len(set(pred.params))

    ctx = EvalContext(current_user=UserRef("u1"))
    assert record_allowed(d, {"owner": "u1", "quantity": 1}, ctx)
    assert record_allowed(d, {"owner": "u9", "quantity": 500}, ctx)
    assert not record_allowed(d, {"owner": "u9", "quantity": 1}, ctx)


def test_consistency_clean_for_example(model):
    assert check_consistency(model) == []


def test_consistency_findings(model):
    m = model.model_copy(deep=True)
    m.security.pages["product-create"] = ["viewer", "editor"]  # viewer cannot create... but can read
    m.security.access = [r for r in m.security.access if r.role != "viewer"]
    m.security.pages["product-list"] = []
    m.security.roles.append("idle")
    m.security.anonymous = True
    codes = {f.code for f in check_consistency(m)}
    assert {
        "page-exposes-unreadable-entity",
        "unreachable-page",
        "role-without-access",
        "anonymous-access",
    } <= codes


def test_platform_rbac_is_deny_by_default():
    assert is_allowed(PlatformRole.VIEWER, "model.read")
    assert not is_allowed(PlatformRole.VIEWER, "model.propose")
    assert is_allowed("editor", "model.apply")
    assert not is_allowed("editor", "member.manage")
    assert is_allowed("owner", "project.delete") and not is_allowed("admin", "project.delete")
    assert not is_allowed("root", "model.read")
    assert not is_allowed(None, "model.read")
    assert not is_allowed("owner", "unlisted.action")


def test_unknown_action_needs_strictest_level():
    assert level_for_action("model.read") == L.A0
    assert level_for_action("model.apply") == L.A2
    assert level_for_action("never.heard.of.it") == L.A4
