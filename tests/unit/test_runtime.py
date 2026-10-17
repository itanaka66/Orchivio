import copy
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select

from orchivio.db import make_engine
from orchivio.model import AppModel, assign_element_ids, model_hash
from orchivio.ports import InMemoryAuditSink
from orchivio.runtime import (
    AppData,
    AppUser,
    Conflict,
    Forbidden,
    Invalid,
    PreviewNotBuilt,
    PreviewStale,
    PreviewStore,
    RowNotFound,
    RuntimeSchema,
    UnknownEntity,
    generate,
    table_prefix,
)

P = "core.Product"
VIEWER = AppUser("v1", frozenset({"viewer"}))
ED1 = AppUser("e1", frozenset({"editor"}))
ED2 = AppUser("e2", frozenset({"editor"}))
ADMIN = AppUser("a1", frozenset({"admin"}))
BOTH = AppUser("e1", frozenset({"editor", "viewer"}))


def build(tmp_path, raw, audit=None, name="rt.db"):
    engine = make_engine(f"sqlite:///{tmp_path / name}")
    model = assign_element_ids(AppModel.model_validate(raw))
    app_id = uuid.uuid4()
    store = PreviewStore(engine)
    schema, data = store.reset(app_id, model, model_hash(model), audit=audit)
    return data, store, app_id, model, engine


@pytest.fixture
def rt(tmp_path, raw_model):
    audit = InMemoryAuditSink()
    data, store, app_id, model, engine = build(tmp_path, raw_model, audit)
    data.audit_sink = audit
    return data


def product(**kw):
    return {"name": "Widget", "sku": "W-1", "quantity": 5, "reorder_level": 2, **kw}


# --- creation, validation, defaults, computed ----------------------------------------------


def test_create_fills_generated_defaults_and_computed(rt):
    row = rt.create(P, ADMIN, product(quantity=0))
    assert uuid.UUID(row["id"]) and row["created_at"] and row["owner"] == "a1"  # default_expr: $currentUser
    assert row["status"] == "out_of_stock"  # virtual computed field
    assert rt.create(P, ADMIN, product(sku="W-2", quantity=2))["status"] == "low"
    assert rt.create(P, ADMIN, product(sku="W-3", quantity=9))["status"] == "in_stock"


@pytest.mark.parametrize("data,field,fragment", [
    ({"sku": "S", "quantity": 1, "reorder_level": 1}, "name", "required"),
    (product(bogus=1), "bogus", "unknown field"),
    (product(id=str(uuid.uuid4())), "id", "set by the system"),
    (product(status="low"), "status", "set by the system"),
    (product(quantity=-1), "quantity", "at least 0"),
    (product(quantity="5"), "quantity", "integer"),
    (product(quantity=True), "quantity", "integer"),
    (product(name="x" * 201), "name", "at most 200"),
    (product(name=5), "name", "string"),
])
def test_server_side_validation(rt, data, field, fragment):
    with pytest.raises(Invalid) as e:
        rt.create(P, ADMIN, data)
    assert any(d["field"] == field and fragment in d["message"] for d in e.value.details)


def test_unknown_entity(rt):
    with pytest.raises(UnknownEntity):
        rt.list("core.Nope", ADMIN)
    with pytest.raises(UnknownEntity):
        rt.list("system.User", ADMIN)


def test_unique_values_conflict(rt):
    rt.create(P, ADMIN, product())
    with pytest.raises(Conflict) as e:
        rt.create(P, ADMIN, product(name="Other"))
    assert e.value.details == {"fields": ["sku"]}


# --- role and row-level access ---------------------------------------------------------------


def test_roles_gate_operations(rt):
    row = rt.create(P, ADMIN, product())
    assert rt.list(P, VIEWER)["total"] == 1 and rt.get(P, VIEWER, row["id"])["sku"] == "W-1"
    with pytest.raises(Forbidden):
        rt.create(P, VIEWER, product(sku="X"))
    with pytest.raises(Forbidden):
        rt.update(P, VIEWER, row["id"], {"quantity": 1})
    with pytest.raises(Forbidden):
        rt.delete(P, ED1, row["id"])  # editors have no delete rule
    with pytest.raises(Forbidden):
        rt.list(P, AppUser("x", frozenset({"ghost"})))
    with pytest.raises(Forbidden):
        rt.list(P, AppUser("x", frozenset()))


def test_row_constraint_limits_reads_updates_and_counts(rt):
    mine = rt.create(P, ED1, product(sku="E1"))
    theirs = rt.create(P, ED2, product(sku="E2"))
    assert rt.list(P, ED1)["total"] == 1 and rt.list(P, ED1)["items"][0]["sku"] == "E1"
    assert rt.list(P, ED2)["total"] == 1
    with pytest.raises(RowNotFound):
        rt.get(P, ED1, theirs["id"])
    with pytest.raises(RowNotFound):
        rt.update(P, ED1, theirs["id"], {"quantity": 1})
    assert rt.update(P, ED1, mine["id"], {"quantity": 7})["quantity"] == 7
    assert rt.get(P, ED2, theirs["id"])["quantity"] == 5  # untouched by ED1's failed attempt


def test_union_of_roles_makes_the_unconstrained_rule_win(rt):
    rt.create(P, ED2, product(sku="E2"))
    assert rt.list(P, ED1)["total"] == 0
    assert rt.list(P, BOTH)["total"] == 1  # viewer read is unconstrained


def test_constraint_applies_to_the_new_and_updated_record(rt):
    with pytest.raises(Forbidden):
        rt.create(P, ED1, product(owner="e2"))  # cannot create rows owned by someone else
    mine = rt.create(P, ED1, product())
    with pytest.raises(Forbidden):
        rt.update(P, ED1, mine["id"], {"owner": "e2"})  # cannot hand a row over either
    assert rt.get(P, ED1, mine["id"])["owner"] == "e1"
    assert rt.update(P, ADMIN, mine["id"], {"owner": "e2"})["owner"] == "e2"  # admin may


def test_filters_ordering_and_pagination(rt):
    for i in range(5):
        rt.create(P, ADMIN, product(sku=f"S{i}", quantity=i))
    page = rt.list(P, ADMIN, order_by="quantity", descending=True, limit=2, offset=1)
    assert [r["quantity"] for r in page["items"]] == [3, 2] and page["total"] == 5
    assert rt.list(P, ADMIN, limit=10_000)["limit"] == 200
    assert rt.list(P, ADMIN, filters={"quantity": "4"})["total"] == 1
    assert rt.list(P, ADMIN, filters={"sku": "x' OR '1'='1"})["total"] == 0  # a value, never SQL
    for bad in ({"nope": "1"}, {"quantity": "abc"}, {"status": "low"}):
        with pytest.raises(Invalid):
            rt.list(P, ADMIN, filters=bad)
    with pytest.raises(Invalid):
        rt.list(P, ADMIN, order_by="status")


def test_bad_ids_are_not_found(rt):
    for rid in ("not-a-uuid", str(uuid.uuid4()), 5):
        with pytest.raises(RowNotFound):
            rt.get(P, ADMIN, rid)
        with pytest.raises(RowNotFound):
            rt.delete(P, ADMIN, rid)


def test_delete_and_audit(tmp_path, raw_model):
    audit = InMemoryAuditSink()
    data, *_ = build(tmp_path, raw_model, audit)
    row = data.create(P, ADMIN, product())
    data.update(P, ADMIN, row["id"], {"quantity": 1})
    data.delete(P, ADMIN, row["id"])
    assert data.list(P, ADMIN)["total"] == 0
    assert [e.action for e in audit.events] == ["record.created", "record.updated", "record.deleted"]
    assert all(e.plane == "application" and e.metadata["app_user"] == "a1" for e in audit.events)
    assert "Widget" not in str([e.metadata for e in audit.events])  # no record contents in audit


def test_uncompilable_constraint_denies_instead_of_skipping_the_filter(tmp_path, raw_model):
    raw = copy.deepcopy(raw_model)
    raw["security"]["access"][1]["row_constraint"] = "record.quantity / 2 > 1"
    data, *_ = build(tmp_path, raw)
    data.seed(P, product(owner="a1"))
    with pytest.raises(Forbidden):
        data.list(P, ED1)
    assert data.list(P, ADMIN)["total"] == 1


# --- types, stored computed, references ------------------------------------------------------


@pytest.fixture
def typed(tmp_path):
    pk = {"name": "id", "type": "uuid", "primary_key": True, "generated": True}
    raw = {"schema_version": "1.1", "application": {"key": "t", "name": "T"}, "modules": [{"key": "m", "entities": [
        {"name": "Supplier", "display_name": "S", "fields": [pk, {"name": "name", "type": "string", "required": True, "unique": True}]},
        {"name": "Item", "display_name": "I", "fields": [pk,
            {"name": "name", "type": "string", "required": True, "max_length": 20},
            {"name": "qty", "type": "integer", "required": True, "minimum": 0, "maximum": 100},
            {"name": "price", "type": "decimal", "required": True},
            {"name": "total", "type": "decimal", "computed": {"mode": "stored", "expr": "price * qty"}},
            {"name": "label", "type": "string", "computed": {"mode": "virtual", "expr": "name + ' x' + 'y'"}},
            {"name": "active", "type": "boolean", "default": True},
            {"name": "due", "type": "date"},
            {"name": "seen", "type": "datetime"},
            {"name": "meta", "type": "json"},
            {"name": "supplier", "type": "ref:Supplier"},
            {"name": "created_at", "type": "datetime", "generated": True}],
         "indexes": [{"fields": ["name", "qty"], "unique": True}]}]}],
        "security": {"roles": ["admin"], "access": [
            {"entity": "m.Supplier", "role": "admin", "operations": ["read", "create", "update", "delete"]},
            {"entity": "m.Item", "role": "admin", "operations": ["read", "create", "update", "delete"]}]}}
    return build(tmp_path, raw)[0]


I = "m.Item"


def test_types_roundtrip_and_stored_computed(typed):
    s = typed.create("m.Supplier", ADMIN, {"name": "ACME"})
    row = typed.create(I, ADMIN, {"name": "bolt", "qty": 4, "price": "2.50", "due": "2026-03-01",
                                  "seen": "2026-03-01T09:00:00+09:00", "meta": {"k": [1]}, "supplier": s["id"]})
    assert row["total"] == "10.00000000" and row["active"] is True and row["label"] == "bolt xy"
    assert row["due"] == "2026-03-01" and row["seen"].startswith("2026-03-01T00:00:00") and row["meta"] == {"k": [1]}
    assert typed.update(I, ADMIN, row["id"], {"qty": 10})["total"] == "25.00000000"  # recomputed on update
    assert typed.update(I, ADMIN, row["id"], {"price": 1})["total"] == "10.00000000"


@pytest.mark.parametrize("patch,field", [
    ({"qty": 101}, "qty"), ({"price": "abc"}, "price"), ({"price": "NaN"}, "price"), ({"due": "03/01/2026"}, "due"),
    ({"seen": "yesterday"}, "seen"), ({"active": "yes"}, "active"), ({"supplier": "nope"}, "supplier"),
    ({"supplier": str(uuid.uuid4())}, "supplier"), ({"meta": {"x": "y" * 200_000}}, "meta"),
])
def test_type_validation(typed, patch, field):
    with pytest.raises(Invalid) as e:
        typed.create(I, ADMIN, {"name": "n", "qty": 1, "price": 1, **patch})
    assert e.value.details[0]["field"] == field


def test_composite_unique_and_reference_protection(typed):
    s = typed.create("m.Supplier", ADMIN, {"name": "ACME"})
    base = {"name": "bolt", "qty": 1, "price": 1, "supplier": s["id"]}
    typed.create(I, ADMIN, base)
    with pytest.raises(Conflict):
        typed.create(I, ADMIN, base)
    typed.create(I, ADMIN, {**base, "qty": 2})
    with pytest.raises(Conflict):
        typed.delete("m.Supplier", ADMIN, s["id"])  # still referenced


# --- synthetic data ----------------------------------------------------------------------------


def test_synthetic_data_is_valid_deterministic_and_constraint_aware(tmp_path, raw_model):
    def run(name, seed):
        data, _, _, _, engine = build(tmp_path, raw_model, name=name)
        summary = generate(data.schema, data, rows_per_entity=15, seed=seed, users=("u1", "u2"))
        rows = data.list(P, ADMIN, limit=200, order_by="sku")["items"]
        return summary, rows

    s1, a = run("a.db", 1)
    s2, b = run("b.db", 1)
    s3, c = run("c.db", 2)
    assert s1[P] == {"inserted": 15, "skipped": 0} and s1 == s2
    strip = lambda rows: [{k: v for k, v in r.items() if k not in ("id", "created_at")} for r in rows]  # noqa: E731
    assert strip(a) == strip(b) and strip(a) != strip(c)  # same seed => same data
    assert len({r["sku"] for r in a}) == 15
    assert all(r["quantity"] >= 0 and r["reorder_level"] >= 0 and r["owner"] in ("u1", "u2") for r in a)
    assert {r["status"] for r in a} <= {"in_stock", "low", "out_of_stock"}


def test_synthetic_references_respect_dependencies(typed):
    summary = generate(typed.schema, typed, rows_per_entity=8, seed=3)
    assert summary["m.Supplier"]["inserted"] == 8 and summary["m.Item"]["inserted"] > 0
    suppliers = {r["id"] for r in typed.list("m.Supplier", ADMIN, limit=200)["items"]}
    items = typed.list(I, ADMIN, limit=200)["items"]
    assert all(i["supplier"] is None or i["supplier"] in suppliers for i in items)
    assert all(Decimal(i["total"]) == Decimal(i["price"]) * i["qty"] for i in items)
    assert generate(typed.schema, typed, rows_per_entity=10_000, seed=1)["m.Supplier"]["inserted"] <= 200


# --- preview store -----------------------------------------------------------------------------


def test_store_lifecycle_isolation_and_staleness(tmp_path, raw_model):
    engine = make_engine(f"sqlite:///{tmp_path / 's.db'}")
    store = PreviewStore(engine)
    model = assign_element_ids(AppModel.model_validate(raw_model))
    h = model_hash(model)
    a, b = uuid.uuid4(), uuid.uuid4()
    with pytest.raises(PreviewNotBuilt):
        store.open(a, model, h)
    _, da = store.reset(a, model, h)
    _, db = store.reset(b, model, h)
    da.create(P, ADMIN, product())
    assert da.list(P, ADMIN)["total"] == 1 and db.list(P, ADMIN)["total"] == 0  # separate tables

    assert store.open(a, model, h).list(P, ADMIN)["total"] == 1
    with pytest.raises(PreviewStale):
        store.open(a, model, "sha256:other")
    _, again = store.reset(a, model, h)
    assert again.list(P, ADMIN)["total"] == 0  # reset rebuilds from scratch
    store.drop(a)
    assert store.status(a) is None and store.status(b) is not None


def test_table_names_are_prefixed_and_bounded(raw_model):
    model = assign_element_ids(AppModel.model_validate(raw_model))
    schema = RuntimeSchema(model, table_prefix(uuid.uuid4()))
    assert all(n.startswith("app_") and len(n) <= 60 for n in schema.table_names())
    assert select  # sqlalchemy imported for parity with other test modules
