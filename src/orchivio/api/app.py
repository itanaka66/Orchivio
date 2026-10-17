"""FastAPI application factory.

Scope of this scaffold: health checks and the *stateless* model tooling endpoints
(validate, diff, expression check). Persisted resources (auth, organizations, projects,
revisions) are specified in docs/API-SPEC.md and arrive with Phase 1.
"""

from __future__ import annotations

import secrets
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError

from orchivio import __version__
from orchivio.expr import CheckEnv, ExprError, Type, check
from orchivio.expr.ast import BOOL, DATETIME, DECIMAL, INT, STRING
from orchivio.model import (
    AppModel,
    MissingElementId,
    UnsupportedSchemaVersion,
    assign_element_ids,
    diff_models,
    has_errors,
    migrate_to_latest,
    model_hash,
    validate_model,
)
from orchivio.model.validate import ModelIndex
from orchivio.security import check_consistency

from .settings import Settings, get_settings

_TYPES: dict[str, Type] = {
    "bool": BOOL,
    "int": INT,
    "decimal": DECIMAL,
    "string": STRING,
    "datetime": DATETIME,
}


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


def error_body(code: str, message: str, details: Any = None) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "details": details}}


def _load_model(raw: dict[str, Any]) -> AppModel:
    try:
        return AppModel.model_validate(migrate_to_latest(raw))
    except UnsupportedSchemaVersion as e:
        raise HTTPException(422, error_body("unsupported_schema_version", str(e))) from e
    except ValidationError as e:
        # Never echo submitted values back; report locations and messages only.
        details = [{"loc": list(x["loc"]), "message": x["msg"]} for x in e.errors()[:50]]
        raise HTTPException(422, error_body("invalid_model", "model failed schema validation", details)) from e


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    if settings.env == "prod" and settings.api_token is None:
        raise RuntimeError("ORCHIVIO_API_TOKEN must be set when ORCHIVIO_ENV=prod")

    app = FastAPI(title="Orchivio API", version=__version__, docs_url="/api/docs", openapi_url="/api/openapi.json")

    def require_auth(authorization: str | None = Header(default=None)) -> None:
        if settings.api_token is None:
            return  # dev only: create_app refuses to start in prod without a token
        expected = f"Bearer {settings.api_token.get_secret_value()}"
        if authorization is None or not secrets.compare_digest(authorization.encode(), expected.encode()):
            raise HTTPException(401, error_body("unauthorized", "missing or invalid bearer token"))

    @app.middleware("http")
    async def limit_body(request: Request, call_next):  # noqa: ANN001, ANN202
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > settings.max_body_bytes:
            return JSONResponse(error_body("payload_too_large", "request body too large"), status_code=413)
        return await call_next(request)

    @app.exception_handler(HTTPException)
    async def http_error(_: Request, exc: HTTPException) -> JSONResponse:
        body = exc.detail if isinstance(exc.detail, dict) and "error" in exc.detail else error_body("http_error", str(exc.detail))
        return JSONResponse(body, status_code=exc.status_code, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def bad_request(_: Request, exc: RequestValidationError) -> JSONResponse:
        details = [{"loc": list(e["loc"]), "message": e["msg"]} for e in exc.errors()[:50]]
        return JSONResponse(error_body("invalid_request", "request body is invalid", details), status_code=422)

    @app.exception_handler(Exception)
    async def unexpected(_: Request, exc: Exception) -> JSONResponse:
        # No stack traces or internals in responses (DESIGN.md section 10.1).
        return JSONResponse(error_body("internal_error", "unexpected server error"), status_code=500)

    @app.get("/healthz", tags=["ops"])
    def healthz() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    api = "/api/v1"

    @app.post(f"{api}/models/validate", tags=["model"], dependencies=[Depends(require_auth)])
    def validate(body: ModelBody) -> dict[str, Any]:
        model = _load_model(body.model)
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

    @app.post(f"{api}/models/diff", tags=["model"], dependencies=[Depends(require_auth)])
    def diff(body: DiffBody) -> dict[str, Any]:
        base = assign_element_ids(_load_model(body.base))
        new = assign_element_ids(_load_model(body.new), previous=base)
        try:
            d = diff_models(base, new)
        except MissingElementId as e:  # pragma: no cover - ids were just assigned
            raise HTTPException(500, error_body("internal_error", str(e))) from e
        return {
            "level": d.level.name,
            "security_changed": d.security_changed,
            "changes": [c.as_dict() for c in d.changes],
        }

    @app.post(f"{api}/expressions/check", tags=["model"], dependencies=[Depends(require_auth)])
    def check_expression(body: ExpressionBody) -> dict[str, Any]:
        model = _load_model(body.model)
        index = ModelIndex(model)
        module_key = (body.entity or "").split(".", 1)[0] or (model.modules[0].key if model.modules else "")
        if body.entity and body.entity not in index.entities:
            raise HTTPException(422, error_body("unknown_entity", f"unknown entity {body.entity!r}"))
        expected = None
        if body.expected is not None:
            expected = _TYPES.get(body.expected)
            if expected is None:
                raise HTTPException(422, error_body("invalid_request", "unknown expected type"))
        try:
            res = check(body.expression, CheckEnv(index.types_for(module_key), body.entity), expected)
        except ExprError as e:
            return {"valid": False, "error": {"message": e.message, "position": e.pos}}
        return {"valid": True, "type": str(res.type), "fields": sorted(res.fields)}

    return app
