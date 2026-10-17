import copy
import uuid

import pytest

from orchivio.db import make_engine
from orchivio.model import AppModel, assign_element_ids, model_hash
from orchivio.runtime import service as svc
from orchivio.security import ALLOW_ALL, AccessDecision
from orchivio.testgen import generate_cases, run_tests


def prepared(raw):
    m = assign_element_ids(AppModel.model_validate(raw))
    return m, model_hash(m)


def run(tmp_path, raw, seed=1, prebuilt=None):
    m, h = prebuilt or prepared(raw)
    return run_tests(m, h, make_engine(f"sqlite:///{tmp_path / f't{uuid.uuid4().hex[:6]}.db'}"), seed=seed)


def failed(report):
    return {r.id for r in report.results if r.status == "failed"}


def test_case_ids_come_from_element_keys(raw_model):
    ids = {c.id for c in generate_cases(prepared(raw_model)[0])}
    assert "access:core.Product:viewer:read" in ids and "access:core.Product:viewer:delete" in ids
    assert "crud:core.Product" in ids and "validation:core.Product:sku:unique" in ids
    assert "validation:core.Product:name:max_length" in ids and "validation:core.Product:quantity:minimum" in ids
    assert "validation:core.Product:*:unknown_field" in ids
    assert not any(i.startswith("validation:core.Product:owner:required") for i in ids)  # has default_expr
    assert len([i for i in ids if i.startswith("access:")]) == 12  # 1 entity x 3 roles x 4 operations
    assert len([i for i in ids if i.startswith("row:")]) == 3  # editor read, create, update are constrained
    m, _ = prepared(raw_model)
    assert generate_cases(m) == generate_cases(m)
    # ids embed element ids, so a new revision of the same model keeps them
    assert generate_cases(assign_element_ids(m.model_copy(deep=True), previous=m)) == generate_cases(m)


def test_sample_model_passes_with_full_coverage(tmp_path, raw_model):
    report = run(tmp_path, raw_model)
    assert report.ok and report.counts["failed"] == 0 and report.counts["passed"] >= 20
    assert report.counts["skipped"] == 0, [r for r in report.results if r.status == "skipped"]
    assert report.coverage == {"flows": {"covered": 1, "total": 1}, "entities": {"covered": 1, "total": 1},
                               "access_rules": {"covered": 3, "total": 3},
                               "row_constraints": {"covered": 1, "total": 1}}


def test_runs_are_deterministic(tmp_path, raw_model):
    built = prepared(raw_model)
    a, b = run(tmp_path, raw_model, 5, built), run(tmp_path, raw_model, 5, built)
    assert [(r.id, r.status) for r in a.results] == [(r.id, r.status) for r in b.results]


# --- the suite must be able to fail ------------------------------------------------------------


def test_detects_a_runtime_that_ignores_role_permissions(tmp_path, raw_model, monkeypatch):
    monkeypatch.setattr(svc, "resolve_access", lambda *a, **k: AccessDecision(True, True))
    report = run(tmp_path, raw_model)
    assert "access:core.Product:viewer:create" in failed(report)
    assert "access:core.Product:editor:delete" in failed(report)
    assert not report.ok


def test_detects_a_runtime_that_drops_the_row_filter(tmp_path, raw_model, monkeypatch):
    monkeypatch.setattr(svc, "row_predicate", lambda decision, ctx: ALLOW_ALL)
    report = run(tmp_path, raw_model)
    assert any(i.startswith("row:core.Product:editor:read") for i in failed(report))


def test_detects_a_runtime_that_skips_server_side_validation(tmp_path, raw_model, monkeypatch):
    monkeypatch.setattr(svc, "coerce_value", lambda schema, mod, f, value: value)
    report = run(tmp_path, raw_model)
    assert {"validation:core.Product:name:max_length", "validation:core.Product:quantity:minimum"} <= failed(report)


def test_detects_a_runtime_that_lets_rows_escape_the_constraint(tmp_path, raw_model, monkeypatch):
    """Creates and updates must be checked against the row constraint, not only reads."""
    monkeypatch.setattr(svc, "record_allowed", lambda *a, **k: True)
    report = run(tmp_path, raw_model)
    assert any(i.startswith("row:core.Product:editor:create") for i in failed(report))
    assert any(i.startswith("row:core.Product:editor:update") for i in failed(report))


# --- honest skipping -----------------------------------------------------------------------------


def test_cases_that_cannot_be_exercised_are_skipped_not_passed(tmp_path, raw_model):
    raw = copy.deepcopy(raw_model)
    for rule in raw["security"]["access"]:  # nobody may create Products
        rule["operations"] = [o for o in rule["operations"] if o != "create"]
    report = run(tmp_path, raw)
    by_id = {r.id: r for r in report.results}
    assert report.ok
    assert by_id["crud:core.Product"].status == "skipped" and "allow creating" in by_id["crud:core.Product"].message
    assert by_id["validation:core.Product:name:max_length"].status == "skipped"
    assert by_id["access:core.Product:admin:create"].status == "failed" or by_id["access:core.Product:admin:create"].status == "passed"
    assert report.counts["skipped"] >= 3
