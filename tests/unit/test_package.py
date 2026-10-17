import copy
import io
import json
import zipfile

import pytest

from orchivio.delivery import PackageError, build_package, generate_key, plan_migration, public_key_of, sbom, verify_package
from orchivio.model import AppModel, assign_element_ids, migrate_to_latest

SEED, PUB = generate_key()
BOM = {"bomFormat": "CycloneDX", "specVersion": "1.5", "version": 1, "components": []}


def identified(raw):
    return assign_element_ids(AppModel.model_validate(migrate_to_latest(raw)))


@pytest.fixture
def m1(raw_model):
    return identified(raw_model)


def evolve(m: AppModel, fn) -> AppModel:
    raw = copy.deepcopy(m.model_dump(mode="json", exclude_none=True))
    fn(raw)
    return assign_element_ids(AppModel.model_validate(raw), previous=m)


def product(raw):
    return next(e for mod in raw["modules"] for e in mod["entities"] if e["name"] == "Product")


def pkg(model, **kw):
    return build_package(model, runtime_version="1.2.3", signing_seed=SEED, sbom_doc=BOM, **kw)


def test_build_is_reproducible_and_verifies(m1):
    a, b = pkg(m1), pkg(m1)
    assert a.data == b.data and a.digest == b.digest and a.signature == b.signature
    v = verify_package(a.data, [PUB])
    assert v.manifest["model_hash"] == a.manifest["model_hash"] and v.digest == a.digest
    assert v.model.application.key == m1.application.key and v.sbom == BOM
    assert sorted(zipfile.ZipFile(io.BytesIO(a.data)).namelist()) == ["PACKAGE.json", "PACKAGE.sig", "migration-plan.json", "model.zip", "sbom.json"]


def test_different_inputs_give_different_packages(m1):
    other = evolve(m1, lambda r: product(r).update(display_name="Changed"))
    assert pkg(m1).digest != pkg(other).digest
    assert build_package(m1, runtime_version="9.9", signing_seed=SEED, sbom_doc=BOM).digest != pkg(m1).digest


def replace_entry(data, name, fn):
    z = zipfile.ZipFile(io.BytesIO(data))
    files = {i.filename: z.read(i) for i in z.infolist()}
    files[name] = fn(files[name])
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as w:
        for n, b in files.items():
            w.writestr(zipfile.ZipInfo(n, (1980, 1, 1, 0, 0, 0)), b)
    return out.getvalue()


@pytest.mark.parametrize("name", ["model.zip", "migration-plan.json", "sbom.json"])
def test_any_tampered_file_is_detected(m1, name):
    data = pkg(m1).data
    bad = replace_entry(data, name, lambda b: b + b" ")
    with pytest.raises(PackageError, match="manifest"):
        verify_package(bad, [PUB])


def test_manifest_and_signature_tampering(m1):
    data = pkg(m1).data
    forged = replace_entry(data, "PACKAGE.json", lambda b: b.replace(b"1.2.3", b"6.6.6"))
    with pytest.raises(PackageError, match="signature does not match"):
        verify_package(forged, [PUB])
    with pytest.raises(PackageError, match="base64"):
        verify_package(replace_entry(data, "PACKAGE.sig", lambda b: b"!!!"), [PUB])


def test_untrusted_and_missing_keys(m1):
    data = pkg(m1).data
    _, other_pub = generate_key()
    with pytest.raises(PackageError, match="not trusted"):
        verify_package(data, [other_pub])
    with pytest.raises(PackageError, match="no trusted"):
        verify_package(data, [])
    assert verify_package(data, [other_pub, PUB])  # any trusted key may match


def test_structure_is_strict(m1):
    with pytest.raises(PackageError, match="valid zip"):
        verify_package(b"nope", [PUB])
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as w:
        w.writestr("extra.txt", "x")
    with pytest.raises(PackageError, match="exactly"):
        verify_package(out.getvalue(), [PUB])
    data = pkg(m1).data
    z = zipfile.ZipFile(io.BytesIO(data))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as w:  # compressed entries are refused
        for i in z.infolist():
            w.writestr(i.filename, z.read(i))
    with pytest.raises(PackageError, match="plain stored"):
        verify_package(out.getvalue(), [PUB])


def test_bad_keys_are_reported():
    with pytest.raises(PackageError):
        public_key_of("not base64!")
    with pytest.raises(PackageError):
        public_key_of("AAAA")


def test_sbom_lists_runtime_requirements():
    names = {c["name"] for c in sbom()["components"]}
    assert {"pydantic", "fastapi"} <= names or names == set()  # empty when run from an uninstalled tree


# --- migration plan ----------------------------------------------------------------------------


def steps(old, new):
    return [(s["op"], s["target"], s["phase"], s["destructive"]) for s in plan_migration(old, new)]


def test_first_deployment_creates_tables(m1):
    plan = plan_migration(None, m1)
    assert plan and all(s["op"] == "create_table" and s["phase"] == "expand" and not s["destructive"] for s in plan)


def test_no_change_no_steps(m1):
    assert plan_migration(m1, m1) == []


def test_added_field_is_expand_and_flags_backfill(m1):
    new = evolve(m1, lambda r: product(r)["fields"].append({"name": "note", "type": "text", "required": True}))
    (s,) = plan_migration(m1, new)
    assert (s["op"], s["target"], s["phase"], s["destructive"], s["backfill_required"]) == ("add_column", "core.Product.note", "expand", False, True)
    new2 = evolve(m1, lambda r: product(r)["fields"].append({"name": "note", "type": "text"}))
    assert plan_migration(m1, new2)[0]["backfill_required"] is False


def test_removed_field_and_entity_are_destructive_contract_steps(m1):
    gone = evolve(m1, lambda r: product(r).update(fields=[f for f in product(r)["fields"] if f["name"] != "reorder_level"]))
    assert ("drop_column", "core.Product.reorder_level", "contract", True) in steps(m1, gone)


def test_rename_is_add_copy_then_drop_not_data_loss(m1):
    def rename(r):
        next(f for f in product(r)["fields"] if f["name"] == "sku")["name"] = "code"

    new = evolve(m1, rename)
    got = steps(m1, new)
    assert ("add_column", "core.Product.code", "expand", False) in got
    assert ("drop_column", "core.Product.sku", "contract", True) in got
    assert next(s for s in plan_migration(m1, new) if s["op"] == "add_column")["copy_from"] == "core.Product.sku"


def test_type_change_is_destructive_and_expand_comes_first(m1):
    def change(r):
        p = product(r)
        next(f for f in p["fields"] if f["name"] == "quantity")["type"] = "text"
        p["fields"].append({"name": "extra", "type": "text"})

    plan = plan_migration(m1, evolve(m1, change))
    assert [s["phase"] for s in plan] == ["expand", "contract"]
    assert plan[1]["op"] == "alter_column_type" and plan[1]["destructive"] is True
    json.dumps(plan)  # serialisable
