import copy
import io
import json
import os
import uuid
import zipfile

import pytest
import yaml
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from orchivio.model import (
    AppModel,
    CanonicalError,
    assign_element_ids,
    build_archive,
    diff_models,
    from_files,
    model_hash,
    read_archive,
    to_files,
    validate_model,
)
from orchivio.model.canonical import MANIFEST


@pytest.fixture
def ident(raw_model):
    return assign_element_ids(AppModel.model_validate(raw_model))


# --- example-based ---------------------------------------------------------------------------------------


def test_one_file_per_element_and_a_lossless_round_trip(ident):
    files = to_files(ident)
    assert set(files) == {
        "application.yaml", "security/security.yaml", "modules/core/module.yaml",
        "modules/core/entities/Product.yaml", "modules/core/enumerations/StockStatus.yaml",
        "modules/core/pages/product-list.yaml", "modules/core/pages/product-create.yaml",
        "modules/core/flows/ReorderLowStock.yaml",
        "security/access/core.Product__viewer.yaml", "security/access/core.Product__editor.yaml",
        "security/access/core.Product__admin.yaml",
    }
    back = from_files(files)
    assert back == ident and model_hash(back) == model_hash(ident)
    assert not [i for i in validate_model(back) if i.severity == "error"]


def test_output_is_stable_and_minimal(ident):
    assert to_files(ident) == to_files(ident.model_copy(deep=True))
    text = to_files(ident)["modules/core/entities/Product.yaml"]
    assert "required: false" not in text and "unique: false" not in text  # defaults are not written
    assert text.index("element_id") < text.index("name:") < text.index("fields:")  # schema declaration order


def test_a_change_to_one_element_touches_one_file(ident):
    before = to_files(ident)
    edited = ident.model_copy(deep=True)
    edited.modules[0].entities[0].fields[1].max_length = 120
    after = to_files(edited)
    assert [p for p in after if after[p] != before[p]] == ["modules/core/entities/Product.yaml"]
    flow_edit = ident.model_copy(deep=True)
    flow_edit.modules[0].flows[0].steps[1].config = {**flow_edit.modules[0].flows[0].steps[1].config, "limit": 5}
    changed = [p for p, t in to_files(flow_edit).items() if t != before[p]]
    assert changed == ["modules/core/flows/ReorderLowStock.yaml"]


def test_element_order_is_preserved(ident):
    m = ident.model_copy(deep=True)
    m.modules[0].entities[0].fields.reverse()
    m.security.access.reverse()
    back = from_files(to_files(m))
    assert [f.name for f in back.modules[0].entities[0].fields] == [f.name for f in m.modules[0].entities[0].fields]
    assert [r.role for r in back.security.access] == [r.role for r in m.security.access]
    assert back == m


def test_the_diff_between_reimported_models_is_empty(ident):
    assert diff_models(ident, from_files(to_files(ident))).is_empty


@pytest.mark.parametrize("breakage,fragment", [
    (lambda f: f.pop("modules/core/entities/Product.yaml"), "file is missing"),
    (lambda f: f.update({"modules/core/entities/Ghost.yaml": "name: Ghost\n"}), "not referenced"),
    (lambda f: f.update({"../evil.yaml": "x: 1\n"}), "not a recognised canonical file"),
    (lambda f: f.update({"/etc/passwd": "x"}), "not a recognised canonical file"),
    (lambda f: f.update({"modules/core/entities/Product.yaml": "- not a mapping\n"}), "expected a mapping"),
    (lambda f: f.update({"modules/core/entities/Product.yaml": "name: [unclosed\n"}), "not valid YAML"),
    (lambda f: f.update({"application.yaml": f["application.yaml"].replace("orchivio.model/1", "other/9")}), "unsupported format"),
    (lambda f: f.update({"modules/core/entities/Product.yaml": "name: Product\nbogus: 1\n"}), "schema"),
    (lambda f: f.update({"modules/core/module.yaml": f["modules/core/module.yaml"].replace("key: core", "key: other")}), "does not match its directory"),
])
def test_reading_is_strict(ident, breakage, fragment):
    files = to_files(ident)
    breakage(files)
    with pytest.raises(CanonicalError, match=fragment):
        from_files(files)


def test_names_that_differ_only_by_case_are_refused_on_write(ident):
    m = ident.model_copy(deep=True)
    twin = m.modules[0].entities[0].model_copy(deep=True)
    twin.name, twin.element_id = "PRODUCT", None
    m.modules[0].entities.append(twin)
    with pytest.raises(CanonicalError, match="letter case"):
        to_files(m)


def test_yaml_is_loaded_safely(ident):
    files = to_files(ident)
    files["modules/core/pages/product-list.yaml"] = "!!python/object/apply:os.system ['echo hacked']\n"
    with pytest.raises(CanonicalError, match="not valid YAML"):
        from_files(files)


# --- archives ----------------------------------------------------------------------------------------------


def test_archives_are_reproducible_and_verify_themselves(ident):
    z = build_archive(ident)
    assert build_archive(ident.model_copy(deep=True)) == z
    assert read_archive(z) == ident
    with zipfile.ZipFile(io.BytesIO(z)) as zf:
        assert {i.date_time for i in zf.infolist()} == {(1980, 1, 1, 0, 0, 0)}
        assert [i.filename for i in zf.infolist()] == sorted(i.filename for i in zf.infolist())
        manifest = json.loads(zf.read(MANIFEST))
    assert manifest["model_hash"] == model_hash(ident) and manifest["format"] == "orchivio.model/1"


def rewrite(z: bytes, change):
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(z)) as zin, zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as zout:
        entries = {i.filename: zin.read(i) for i in zin.infolist()}
        change(entries)
        for name, data in entries.items():
            zout.writestr(name, data)
    return out.getvalue()


def test_tampering_is_detected(ident):
    z = build_archive(ident)

    def edit_file(e):
        e["modules/core/entities/Product.yaml"] = e["modules/core/entities/Product.yaml"].replace(b"Product", b"Produkt", 1)

    with pytest.raises(CanonicalError, match="does not match the manifest"):
        read_archive(rewrite(z, edit_file))
    with pytest.raises(CanonicalError, match="manifest is missing"):
        read_archive(rewrite(z, lambda e: e.pop(MANIFEST)))
    with pytest.raises(CanonicalError, match="absent"):
        read_archive(rewrite(z, lambda e: e.pop("security/access/core.Product__viewer.yaml")))

    def change_hash(e):
        m = json.loads(e[MANIFEST])
        m["model_hash"] = "sha256:" + "0" * 64
        e[MANIFEST] = json.dumps(m).encode()

    with pytest.raises(CanonicalError, match="model hash"):
        read_archive(rewrite(z, change_hash))


def test_hostile_archives_are_rejected(ident):
    with pytest.raises(CanonicalError, match="not a valid zip"):
        read_archive(b"not a zip")
    z = build_archive(ident)
    with pytest.raises(CanonicalError, match="not a recognised canonical file|does not match"):
        read_archive(rewrite(z, lambda e: e.update({"../../etc/passwd": b"x"})))
    with pytest.raises(CanonicalError, match="too many files"):
        read_archive(rewrite(z, lambda e: e.update({f"modules/core/pages/p{i}.yaml": b"x" for i in range(2100)})))
    with pytest.raises(CanonicalError, match="larger than"):
        read_archive(rewrite(z, lambda e: e.update({"modules/core/pages/big.yaml": b"x" * 1_100_000})))
    with pytest.raises(CanonicalError, match="UTF-8"):
        read_archive(rewrite(z, lambda e: e.update({"modules/core/pages/bin.yaml": b"\xff\xfe"})))

    bomb = io.BytesIO()
    with zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("modules/core/pages/zero.yaml", b"0" * 900_000)
    with pytest.raises(CanonicalError, match="compression ratio"):
        read_archive(bomb.getvalue())

    link = io.BytesIO()
    with zipfile.ZipFile(link, "w") as zf:
        info = zipfile.ZipInfo("application.yaml")
        info.external_attr = 0o120777 << 16  # symlink
        zf.writestr(info, "x")
    with pytest.raises(CanonicalError, match="regular files"):
        read_archive(link.getvalue())


# --- property tests ------------------------------------------------------------------------------------------

# Arbitrary unicode (minus surrogates, which cannot be UTF-8) in every free-text position
text = st.text(st.characters(blacklist_categories=("Cs",)), min_size=1, max_size=40)
json_scalar = st.none() | st.booleans() | st.integers(-10**9, 10**9) | st.floats(allow_nan=False, allow_infinity=False, width=32) | text
json_value = st.recursive(json_scalar, lambda c: st.lists(c, max_size=3) | st.dictionaries(text, c, max_size=3), max_leaves=6)
uid = st.uuids().map(str)
maybe_id = st.none() | uid
low = st.from_regex(r"[a-z][a-z0-9_]{0,10}", fullmatch=True)
pascal = st.from_regex(r"[A-Z][A-Za-z0-9]{0,10}", fullmatch=True)
kebab = st.from_regex(r"[a-z][a-z0-9-]{0,10}", fullmatch=True)
expr = st.sampled_from(["true", "record.x > 1", "a == 'it''s: \"quoted\"'", "x ?? 0", "yes", "null"]) | text


def uniq(strategy, key=lambda x: x, max_size=3):
    return st.lists(strategy, max_size=max_size, unique_by=lambda x: key(x).lower())


field_def = st.fixed_dictionaries({"name": low, "type": st.sampled_from(["string", "integer", "enum:E", "ref:Other"])}, optional={
    "element_id": uid, "display_name": text, "required": st.booleans(), "unique": st.booleans(), "primary_key": st.booleans(),
    "generated": st.booleans(), "max_length": st.integers(1, 500), "minimum": st.integers(-100, 0) | st.floats(-9, 0, allow_nan=False, width=32),
    "default": text | st.integers() | st.booleans(), "default_expr": expr, "computed": st.fixed_dictionaries({"expr": expr}),
})
entity_def = st.fixed_dictionaries({"name": pascal}, optional={
    "element_id": uid, "display_name": text, "transient": st.booleans(),
    "fields": uniq(field_def, lambda f: f["name"], 4),
    "indexes": st.lists(st.fixed_dictionaries({"fields": st.lists(low, min_size=1, max_size=2)}, optional={"unique": st.booleans()}), max_size=2),
    "hooks": st.dictionaries(st.sampled_from(["before_create", "after_update"]), pascal, max_size=2),
})
enum_def = st.fixed_dictionaries({"name": pascal, "values": st.lists(low, min_size=1, max_size=3, unique=True)}, optional={"element_id": uid})
page_def = st.fixed_dictionaries({"key": kebab, "path": st.from_regex(r"/[a-z0-9/]{0,10}", fullmatch=True),
                                  "type": st.sampled_from(["list", "form", "detail"]), "entity": pascal}, optional={
    "element_id": uid, "mode": st.sampled_from(["create", "edit"]), "columns": st.lists(low, max_size=3),
    "actions": st.lists(low | st.fixed_dictionaries({"key": kebab}, optional={"visible_expr": expr}), max_size=2),
})
step_def = st.fixed_dictionaries({"id": low, "type": st.sampled_from(["start", "retrieve", "decision", "end"])}, optional={
    "label": text, "config": st.dictionaries(low, json_value, max_size=3),
    "position": st.fixed_dictionaries({"x": st.integers(-500, 500).map(float), "y": st.integers(-500, 500).map(float)}),
})
flow_test = st.fixed_dictionaries({"name": text}, optional={
    "args": st.dictionaries(low, json_value, max_size=2),
    "fixtures": st.dictionaries(pascal, st.lists(st.dictionaries(low, json_value, max_size=2), max_size=2), max_size=2),
    "expect": st.fixed_dictionaries({}, optional={"status": st.sampled_from(["succeeded", "failed"]), "visited": st.lists(low, max_size=2)}),
})
flow_def = st.fixed_dictionaries({"key": pascal}, optional={
    "element_id": uid, "kind": st.sampled_from(["server", "client"]), "description": text,
    "params": st.lists(st.fixed_dictionaries({"name": low, "type": st.just("string")}), max_size=2),
    "steps": uniq(step_def, lambda s: s["id"], 3),
    "edges": st.lists(st.fixed_dictionaries({"id": low, "source": low, "target": low}, optional={"handle": st.sampled_from(["out", "true", "false"])}), max_size=3),
    "tests": st.lists(flow_test, max_size=2),
})
module_def = st.fixed_dictionaries({"key": low}, optional={
    "element_id": uid, "enumerations": uniq(enum_def, lambda e: e["name"], 2), "entities": uniq(entity_def, lambda e: e["name"], 3),
    "pages": uniq(page_def, lambda p: p["key"], 2), "flows": uniq(flow_def, lambda f: f["key"], 2),
})
access_def = st.fixed_dictionaries({"entity": st.just("core.Thing") | st.just("m.Other"), "role": low,
                                    "operations": st.lists(st.sampled_from(["read", "create", "update", "delete"]), min_size=1, max_size=4, unique=True)},
                                   optional={"element_id": uid, "row_constraint": expr})
model_def = st.fixed_dictionaries({
    "application": st.fixed_dictionaries({"key": kebab, "name": text}, optional={"version": text, "locales": st.lists(text, max_size=2)}),
    "modules": uniq(module_def, lambda m: m["key"], 3),
}, optional={
    "security": st.fixed_dictionaries({}, optional={
        "roles": st.lists(low, max_size=3, unique=True), "anonymous": st.booleans(),
        "access": uniq(access_def, lambda a: f"{a['entity']}__{a['role']}", 3),
        "pages": st.dictionaries(kebab, st.lists(low, max_size=2), max_size=2)}),
    "lint_suppressions": st.lists(st.fixed_dictionaries({"rule": kebab.filter(lambda s: len(s) >= 3), "element_id": uid,
                                                         "justification": st.text(st.characters(blacklist_categories=("Cs",)), min_size=10, max_size=40)}), max_size=2),
}).map(lambda d: AppModel.model_validate(d))

PROP = settings(max_examples=int(os.environ.get("ORCHIVIO_PROP_EXAMPLES", "40")), deadline=None, suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much])


@PROP
@given(model_def)
def test_round_trip_is_lossless_for_any_schema_valid_model(model):
    files = to_files(model)
    back = from_files(files)
    assert back == model
    assert to_files(back) == files  # idempotent: the format is a fixed point
    assert model_hash(back) == model_hash(model)


@PROP
@given(model_def)
def test_archives_round_trip_and_are_deterministic(model):
    z = build_archive(model)
    assert z == build_archive(from_files(to_files(model)))
    assert read_archive(z) == model


@PROP
@given(model_def, st.data())
def test_editing_one_element_changes_only_its_file(model, data):
    files = to_files(model)
    entities = [(m.key, i) for m in model.modules for i in range(len(m.entities))]
    if not entities:
        return
    key, i = data.draw(st.sampled_from(entities))
    edited = copy.deepcopy(model)
    mod = next(m for m in edited.modules if m.key == key)
    mod.entities[i].display_name = (mod.entities[i].display_name or "") + "!"
    changed = [p for p, t in to_files(edited).items() if files.get(p) != t]
    assert changed == [f"modules/{key}/entities/{mod.entities[i].name}.yaml"]


def test_uuid_helper_is_available():  # keeps the import used and documents the id format
    assert uuid.UUID(str(uuid.uuid4()))
    assert yaml.safe_load("a: 1") == {"a": 1}
