import copy
import os

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from orchivio.model import (
    AppModel,
    assign_element_ids,
    flatten,
    merge,
    unflatten,
    validate_model,
)


@pytest.fixture
def base(raw_model):
    return assign_element_ids(AppModel.model_validate(raw_model))


def edit(model, fn):
    m = model.model_copy(deep=True)
    fn(m)
    return m


def product(m):
    return m.modules[0].entities[0]


def field(m, name):
    return next(f for f in product(m).fields if f.name == name)


def step(m, sid):
    return next(s for s in m.modules[0].flows[0].steps if s.id == sid)


def ids(result):
    return sorted(c.id for c in result.all_conflicts)


# --- structure ----------------------------------------------------------------------------------------------


def test_flatten_and_unflatten_are_inverse(base):
    assert unflatten(flatten(base)) == base


def test_merging_nothing_changes_nothing(base):
    r = merge(base, base, base)
    assert r.clean and r.model == base and r.all_conflicts == []


def test_unidentified_models_are_refused(raw_model):
    m = AppModel.model_validate(raw_model)
    with pytest.raises(ValueError, match="element_id"):
        merge(m, m, m)


# --- changes on one side ------------------------------------------------------------------------------------


def test_one_sided_changes_are_taken(base):
    ours = edit(base, lambda m: setattr(product(m), "display_name", "Item"))
    assert merge(base, ours, base).model == ours  # only we changed
    assert merge(base, base, ours).model == ours  # only they changed


def test_the_same_change_on_both_sides_is_not_a_conflict(base):
    both = edit(base, lambda m: setattr(product(m), "display_name", "Item"))
    r = merge(base, both, copy.deepcopy(both))
    assert r.clean and r.model == both and r.all_conflicts == []


# --- automatic merges ----------------------------------------------------------------------------------------


def test_different_properties_of_one_element_combine(base):
    ours = edit(base, lambda m: setattr(field(m, "sku"), "max_length", 40))
    theirs = edit(base, lambda m: setattr(field(m, "sku"), "display_name", "SKU code"))
    r = merge(base, ours, theirs)
    assert r.clean and r.merged_from_both == 1
    f = field(r.model, "sku")
    assert (f.max_length, f.display_name) == (40, "SKU code")


def test_different_elements_combine(base):
    ours = edit(base, lambda m: setattr(field(m, "quantity"), "minimum", 1))
    theirs = edit(base, lambda m: setattr(field(m, "sku"), "max_length", 40))
    r = merge(base, ours, theirs)
    assert r.clean and field(r.model, "quantity").minimum == 1 and field(r.model, "sku").max_length == 40


def test_independent_additions_both_survive_in_a_stable_order(base):
    from orchivio.model.schema import FieldDef

    ours = edit(base, lambda m: product(m).fields.append(FieldDef(name="aaa", type="string")))
    theirs = edit(base, lambda m: product(m).fields.append(FieldDef(name="bbb", type="string")))
    ours, theirs = assign_element_ids(ours, previous=base), assign_element_ids(theirs, previous=base)
    names = [f.name for f in merge(base, ours, theirs).model.modules[0].entities[0].fields]
    assert names[-2:] == ["aaa", "bbb"] and names.count("aaa") == names.count("bbb") == 1
    assert [f.name for f in merge(base, theirs, ours).model.modules[0].entities[0].fields][-2:] == ["bbb", "aaa"]


def test_deleting_what_the_other_side_left_alone_deletes_it(base):
    ours = edit(base, lambda m: product(m).fields.__delitem__(slice(-1, None)))  # drops created_at
    r = merge(base, ours, base)
    assert r.clean and "created_at" not in [f.name for f in product(r.model).fields]
    assert merge(base, base, ours).model == r.model


def test_sets_merge_by_adding_and_removing_members(base):
    ours = edit(base, lambda m: m.modules[0].enumerations[0].values.append("discontinued"))
    theirs = edit(base, lambda m: m.modules[0].enumerations[0].values.remove("low"))
    values = merge(base, ours, theirs).model.modules[0].enumerations[0].values
    assert values == ["in_stock", "out_of_stock", "discontinued"]


def test_roles_and_operations_are_sets(base):
    ours = edit(base, lambda m: m.security.roles.append("auditor"))
    theirs = edit(base, lambda m: m.security.roles.append("clerk"))
    assert merge(base, ours, theirs).model.security.roles == ["viewer", "editor", "admin", "auditor", "clerk"]
    o2 = edit(base, lambda m: next(r for r in m.security.access if r.role == "viewer").operations.append("create"))
    t2 = edit(base, lambda m: next(r for r in m.security.access if r.role == "viewer").operations.append("update"))
    ops = next(r for r in merge(base, o2, t2).model.security.access if r.role == "viewer").operations
    assert set(ops) == {"read", "create", "update"}


def test_maps_merge_by_key(base):
    ours = edit(base, lambda m: step(m, "find").config.update(limit=10))
    theirs = edit(base, lambda m: step(m, "find").config.update(where="quantity < 3"))
    cfg = step(merge(base, ours, theirs).model, "find").config
    assert cfg["limit"] == 10 and cfg["where"] == "quantity < 3" and cfg["many"] is True


def test_parts_of_a_flow_merge_independently(base):
    from orchivio.model.schema import FlowEdge, FlowStep

    def add_step(m):
        m.modules[0].flows[0].steps.append(FlowStep(id="extra", type="end"))
        m.modules[0].flows[0].edges.append(FlowEdge(id="ex", source="none", target="extra"))

    ours = edit(base, add_step)
    theirs = edit(base, lambda m: setattr(step(m, "bump"), "label", "Raise level"))
    r = merge(base, ours, theirs)
    flow = r.model.modules[0].flows[0]
    assert r.clean and "extra" in [s.id for s in flow.steps] and step(r.model, "bump").label == "Raise level"


def test_layout_never_conflicts(base):
    from orchivio.model.schema import Position

    ours = edit(base, lambda m: setattr(step(m, "find"), "position", Position(x=1, y=1)))
    theirs = edit(base, lambda m: setattr(step(m, "find"), "position", Position(x=9, y=9)))
    r = merge(base, ours, theirs)
    assert r.clean and step(r.model, "find").position.x == 1  # ours wins, silently


def test_reordering_on_one_side_is_adopted(base):
    ours = edit(base, lambda m: product(m).fields.reverse())
    assert merge(base, ours, base).model == ours
    assert merge(base, base, ours).model == ours


# --- conflicts -----------------------------------------------------------------------------------------------------


def test_same_property_changed_differently_conflicts_until_resolved(base):
    ours = edit(base, lambda m: setattr(product(m), "display_name", "Item"))
    theirs = edit(base, lambda m: setattr(product(m), "display_name", "Article"))
    r = merge(base, ours, theirs)
    assert not r.clean and r.model is None and len(r.conflicts) == 1
    c = r.conflicts[0]
    assert (c.kind, c.prop, c.base, c.ours, c.theirs) == ("property", "display_name", "Product", "Item", "Article")
    assert c.id == f"{product(base).element_id}#display_name"
    assert product(merge(base, ours, theirs, {c.id: "ours"}).model).display_name == "Item"
    assert product(merge(base, ours, theirs, {c.id: "theirs"}).model).display_name == "Article"


def test_conflicts_are_detected_the_same_way_from_either_side(base):
    ours = edit(base, lambda m: setattr(product(m), "display_name", "Item"))
    theirs = edit(base, lambda m: setattr(product(m), "display_name", "Article"))
    assert ids(merge(base, ours, theirs)) == ids(merge(base, theirs, ours))


def test_map_keys_conflict_individually(base):
    ours = edit(base, lambda m: step(m, "find").config.update(limit=10, where="a"))
    theirs = edit(base, lambda m: step(m, "find").config.update(limit=20, where="b"))
    r = merge(base, ours, theirs)
    assert sorted(c.prop for c in r.conflicts) == ["config.limit", "config.where"]
    chosen = {c.id: ("ours" if c.prop.endswith("limit") else "theirs") for c in r.conflicts}
    cfg = step(merge(base, ours, theirs, chosen).model, "find").config
    assert (cfg["limit"], cfg["where"]) == (10, "b")


def test_editing_what_the_other_side_deleted_is_a_conflict(base):
    ours = edit(base, lambda m: product(m).fields.__delitem__(slice(-1, None)))
    theirs = edit(base, lambda m: setattr(field(m, "created_at"), "display_name", "Created"))
    r = merge(base, ours, theirs)
    (c,) = r.conflicts
    assert c.kind == "delete-edit" and c.element_type == "field" and c.name == "created_at"
    assert "created_at" not in [f.name for f in product(merge(base, ours, theirs, {c.id: "ours"}).model).fields]
    kept = product(merge(base, ours, theirs, {c.id: "theirs"}).model)
    assert field_named(kept, "created_at").display_name == "Created"


def field_named(entity, name):
    return next(f for f in entity.fields if f.name == name)


def test_a_child_added_under_a_deleted_parent_is_an_orphan(base):
    from orchivio.model.schema import FieldDef

    ours = edit(base, lambda m: m.modules[0].entities.clear())
    theirs = assign_element_ids(edit(base, lambda m: product(m).fields.append(FieldDef(name="extra", type="string"))), previous=base)
    r = merge(base, ours, theirs)
    orphans = [c for c in r.conflicts if c.kind == "orphan"]
    assert len(orphans) == 1 and orphans[0].name == "extra"
    dropped = merge(base, ours, theirs, {orphans[0].id: "drop"})
    assert dropped.clean and dropped.model.modules[0].entities == []
    kept = merge(base, ours, theirs, {orphans[0].id: "keep"})
    assert kept.clean and [f.name for f in kept.model.modules[0].entities[0].fields] == ["extra"]


def test_diverging_reorders_conflict(base):
    ours = edit(base, lambda m: product(m).fields.reverse())
    theirs = edit(base, lambda m: product(m).fields.append(product(m).fields.pop(0)))
    r = merge(base, ours, theirs)
    (c,) = r.conflicts
    assert c.kind == "order"
    assert merge(base, ours, theirs, {c.id: "ours"}).model.modules[0].entities[0].fields == ours.modules[0].entities[0].fields
    assert [f.name for f in merge(base, ours, theirs, {c.id: "theirs"}).model.modules[0].entities[0].fields] == \
        [f.name for f in theirs.modules[0].entities[0].fields]


def test_the_same_new_id_added_twice_with_different_content_conflicts(base):
    from orchivio.model.schema import FieldDef

    new_id = "99999999-9999-4999-8999-999999999999"
    ours = edit(base, lambda m: product(m).fields.append(FieldDef(element_id=new_id, name="x", type="string", max_length=5)))
    theirs = edit(base, lambda m: product(m).fields.append(FieldDef(element_id=new_id, name="x", type="string", max_length=9)))
    r = merge(base, ours, theirs)
    assert [(c.kind, c.prop) for c in r.conflicts] == [("property", "max_length")]


# --- the merge result still has to be valid ---------------------------------------------------------------------------


def test_a_textually_clean_merge_can_still_be_semantically_broken(base):
    """One side drops a field, the other makes a page show it: no textual conflict, but invalid."""
    ours = edit(base, lambda m: product(m).fields.__delitem__(slice(2, 3)))  # drops sku
    theirs = base
    r = merge(base, ours, theirs)
    assert r.clean
    assert any(i.code == "unknown-column" for i in validate_model(r.model))  # the product-list page still lists it


# --- properties ------------------------------------------------------------------------------------------------------------

PROP = settings(max_examples=int(os.environ.get("ORCHIVIO_PROP_EXAMPLES", "40")), deadline=None,
                suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture])


def random_edits(model, rnd, count, *, additive=True):
    """Apply ``count`` random edits to a copy of ``model``. ``additive=False`` restricts them to edits whose
    merge does not depend on which side is ours (no appends to ordered lists, no layout)."""
    from orchivio.model.schema import FieldDef

    m = model.model_copy(deep=True)
    for n in range(count):
        kinds = ["field-prop", "entity-prop", "step-config", "label", "remove-field"]
        if additive:
            kinds += ["enum-value", "role", "move-step", "add-field", "reorder"]
        kind = rnd.choice(kinds)
        ent = product(m)
        if kind == "field-prop":
            f = rnd.choice(ent.fields)
            f.display_name = f"d{rnd.randint(0, 3)}"
        elif kind == "entity-prop":
            ent.display_name = f"E{rnd.randint(0, 3)}"
        elif kind == "enum-value":
            vals = m.modules[0].enumerations[0].values
            if len(vals) > 1 and rnd.random() < 0.4:  # an enumeration keeps at least one value
                vals.pop(rnd.randrange(len(vals)))
            else:
                v = f"v{rnd.randint(0, 5)}"
                if v not in vals:
                    vals.append(v)
        elif kind == "role":
            r = f"r{rnd.randint(0, 4)}"
            if r not in m.security.roles:
                m.security.roles.append(r)
        elif kind == "step-config":
            step(m, "find").config[rnd.choice(["limit", "where", "extra"])] = rnd.randint(0, 3)
        elif kind == "move-step":
            from orchivio.model.schema import Position

            step(m, rnd.choice(["find", "any", "each"])).position = Position(x=rnd.randint(0, 9), y=rnd.randint(0, 9))
        elif kind == "label":
            step(m, rnd.choice(["find", "any", "bump"])).label = f"L{rnd.randint(0, 3)}"
        elif kind == "remove-field" and len(ent.fields) > 3:
            ent.fields.pop(rnd.randrange(len(ent.fields)))
        elif kind == "add-field":
            ent.fields.append(FieldDef(name=f"n{rnd.randint(0, 99)}", type="string"))
        elif kind == "reorder":
            rnd.shuffle(ent.fields)
    return assign_element_ids(m, previous=model)


edits = st.randoms(use_true_random=False)
counts = st.integers(0, 6)


@PROP
@given(edits, counts)
def test_property_flatten_round_trip(base, rnd, n):
    m = random_edits(base, rnd, n)
    assert unflatten(flatten(m)) == m


@PROP
@given(edits, counts)
def test_property_identity_and_one_sided_merges(base, rnd, n):
    o = random_edits(base, rnd, n)
    assert merge(base, o, o).model == o
    assert merge(base, o, base).model == o
    assert merge(base, base, o).model == o


@PROP
@given(edits, counts, counts)
def test_property_conflicts_do_not_depend_on_which_side_is_ours(base, rnd, n1, n2):
    o, t = random_edits(base, rnd, n1), random_edits(base, rnd, n2)
    assert ids(merge(base, o, t)) == ids(merge(base, t, o))


@PROP
@given(edits, counts, counts)
def test_property_clean_merges_of_non_additive_edits_commute(base, rnd, n1, n2):
    o, t = random_edits(base, rnd, n1, additive=False), random_edits(base, rnd, n2, additive=False)
    a, b = merge(base, o, t), merge(base, t, o)
    if a.clean and b.clean:
        assert a.model == b.model


@PROP
@given(edits, counts, counts, st.sampled_from(["ours", "theirs"]))
def test_property_resolving_every_conflict_always_yields_a_model(base, rnd, n1, n2, side):
    o, t = random_edits(base, rnd, n1), random_edits(base, rnd, n2)
    first = merge(base, o, t)
    chosen = {c.id: side for c in first.all_conflicts}
    for c in first.all_conflicts:
        if c.kind == "orphan":
            chosen[c.id] = "drop"
    final = merge(base, o, t, chosen)
    if not final.clean:  # the only thing a choice cannot fix: both sides together broke a schema rule
        assert [c.kind for c in final.conflicts] == ["invalid"]
        return
    assert isinstance(final.model, AppModel) and unflatten(flatten(final.model)) == final.model
    if not first.all_conflicts:
        assert first.clean


def test_changes_that_are_each_valid_but_not_together_are_reported_not_crashed(base):
    """Found by property testing: both sides remove different values and the enumeration ends up empty."""
    from orchivio.model.schema import EnumDef  # noqa: F401  (documents which rule is involved: min 1 value)

    ours = edit(base, lambda m: m.modules[0].enumerations[0].values.remove("low"))
    ours = edit(ours, lambda m: m.modules[0].enumerations[0].values.remove("in_stock"))
    theirs = edit(base, lambda m: m.modules[0].enumerations[0].values.remove("out_of_stock"))
    r = merge(base, ours, theirs)
    assert not r.clean and [c.kind for c in r.conflicts] == ["invalid"]
    assert "at least 1 item" in r.conflicts[0].message
