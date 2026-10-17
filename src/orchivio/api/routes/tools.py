"""Stateless model tooling: validate, diff, expression check. Requires authentication."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, ValidationError

from orchivio.expr import CheckEnv, ExprError, Type, check
from orchivio.expr.ast import BOOL, DATETIME, DECIMAL, INT, STRING
from orchivio.model import (
    RULES,
    assign_element_ids,
    blocking,
    diff_models,
    has_errors,
    lint,
    model_hash,
    validate_flow,
    validate_model,
)
from orchivio.model.schema import FlowDef
from orchivio.model.validate import ModelIndex
from orchivio.security import check_consistency

from ..deps import api_error, current_principal
from ..modelio import load_model

router = APIRouter(tags=["model tools"], dependencies=[Depends(current_principal)])

_TYPES: dict[str, Type] = {"bool": BOOL, "int": INT, "decimal": DECIMAL, "string": STRING, "datetime": DATETIME}


class ModelBody(BaseModel):
    model: dict[str, Any]


class DiffBody(BaseModel):
    base: dict[str, Any]
    new: dict[str, Any]


class ExpressionBody(BaseModel):
    model: dict[str, Any]
    entity: str | None = Field(default=None, description="Qualified entity, e.g. core.Product")
    expression: str = Field(min_length=1, max_length=2000)
    expected: str | None = Field(default=None, description="bool|int|decimal|string|datetime")


@router.post("/models/validate")
def validate(body: ModelBody) -> dict[str, Any]:
    model = load_model(body.model)
    issues = validate_model(model)
    return {
        "valid": not has_errors(issues),
        "schema_version": model.schema_version,
        "model_hash": model_hash(model),
        "issues": [i.as_dict() for i in issues],
        "security_findings": [
            {"severity": f.severity, "code": f.code, "path": f.path, "message": f.message}
            for f in check_consistency(model)
        ],
    }


@router.post("/models/lint")
def lint_model(body: ModelBody) -> dict[str, Any]:
    model = assign_element_ids(load_model(body.model))
    issues = validate_model(model)
    if has_errors(issues):
        raise api_error(422, "model_invalid", "fix validation errors before linting",
                        [i.as_dict() for i in issues if i.severity == "error"])
    findings = lint(model)
    return {"blocking": len(blocking(findings)), "items": [f.as_dict() for f in findings],
            "rules": [{"id": r.id, "severity": r.severity, "category": r.category, "title": r.title} for r in RULES]}


class FlowBody(BaseModel):
    model: dict[str, Any]
    module: str = Field(min_length=1, max_length=63)
    flow: dict[str, Any]


@router.post("/models/validate-flow")
def validate_one_flow(body: FlowBody) -> dict[str, Any]:
    """Validate one unsaved flow in the context of a model (live feedback for the flow editor).
    Issue paths contain ``steps/<id>`` or ``edges/<id>`` so the editor can attach them to nodes."""
    model = load_model(body.model)
    try:
        flow = FlowDef.model_validate(body.flow)
    except ValidationError as e:
        details = [{"loc": list(x["loc"]), "message": x["msg"]} for x in e.errors()[:50]]
        raise api_error(422, "invalid_flow", "the flow failed schema validation", details) from e
    try:
        issues = validate_flow(model, body.module, flow)
    except KeyError as e:
        raise api_error(422, "unknown_module", "unknown module") from e
    return {"valid": not has_errors(issues), "issues": [i.as_dict() for i in issues]}


@router.post("/models/diff")
def diff(body: DiffBody) -> dict[str, Any]:
    base = assign_element_ids(load_model(body.base))
    new = assign_element_ids(load_model(body.new), previous=base)
    d = diff_models(base, new)
    return {"level": d.level.name, "security_changed": d.security_changed,
            "changes": [c.as_dict() for c in d.changes]}


@router.post("/expressions/check")
def check_expression(body: ExpressionBody) -> dict[str, Any]:
    model = load_model(body.model)
    index = ModelIndex(model)
    module_key = (body.entity or "").split(".", 1)[0] or (model.modules[0].key if model.modules else "")
    if body.entity and body.entity not in index.entities:
        raise api_error(422, "unknown_entity", f"unknown entity {body.entity!r}")
    expected = None
    if body.expected is not None:
        expected = _TYPES.get(body.expected)
        if expected is None:
            raise api_error(422, "invalid_request", "unknown expected type")
    try:
        res = check(body.expression, CheckEnv(index.types_for(module_key), body.entity), expected)
    except ExprError as e:
        return {"valid": False, "error": {"message": e.message, "position": e.pos}}
    return {"valid": True, "type": str(res.type), "fields": sorted(res.fields)}

