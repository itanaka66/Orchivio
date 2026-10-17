"""FastAPI application factory (resources and conventions: docs/API-SPEC.md)."""

from __future__ import annotations

import time
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from orchivio import __version__
from orchivio.db import make_engine, make_session_factory
from orchivio.llm import AnthropicProvider, LLMProvider
from orchivio.ports import LocalArtifactStore
from orchivio.runtime import PreviewStore

from .audit_sink import DbAuditSink
from .observability import configure_logging, logger, new_request_id, request_id_var
from .routes import ALL
from .settings import Settings, get_settings
from .throttle import LoginThrottle


def error_body(code: str, message: str, details: Any = None) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "details": details}}


def _provider_from_settings(settings: Settings) -> LLMProvider | None:
    if settings.llm_provider == "anthropic":
        if settings.anthropic_api_key is None:
            raise RuntimeError("ORCHIVIO_ANTHROPIC_API_KEY is required when ORCHIVIO_LLM_PROVIDER=anthropic")
        kwargs = {"model": settings.llm_model} if settings.llm_model else {}
        return AnthropicProvider(settings.anthropic_api_key.get_secret_value(), **kwargs)
    return None


def create_app(
    settings: Settings | None = None,
    *,
    engine: Engine | None = None,
    session_factory: sessionmaker[Session] | None = None,
    llm_provider: LLMProvider | None = None,
    preview_engine: Engine | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    if settings.env == "prod" and not settings.secure_cookies:
        raise RuntimeError("ORCHIVIO_COOKIE_SECURE must not be disabled when ORCHIVIO_ENV=prod")
    if settings.env == "prod" and settings.preview_database_url == settings.database_url:
        raise RuntimeError("ORCHIVIO_PREVIEW_DATABASE_URL must differ from ORCHIVIO_DATABASE_URL in prod")
    configure_logging(settings.log_json)

    if session_factory is None:
        session_factory = make_session_factory(engine or make_engine(settings.database_url))

    app = FastAPI(title="Orchivio API", version=__version__, docs_url="/api/docs", openapi_url="/api/openapi.json")
    app.state.settings = settings
    app.state.session_factory = session_factory
    app.state.preview_store = PreviewStore(preview_engine or make_engine(settings.preview_database_url))
    app.state.audit_sink = DbAuditSink(session_factory)
    app.state.artifacts = LocalArtifactStore(settings.artifact_dir)
    app.state.llm_provider = llm_provider or _provider_from_settings(settings)
    app.state.throttle = LoginThrottle(settings.login_max_failures, settings.login_window_seconds)

    @app.middleware("http")
    async def request_context(request: Request, call_next):  # noqa: ANN001, ANN202
        rid = new_request_id(request.headers.get("x-request-id"))
        token = request_id_var.set(rid)
        started = time.perf_counter()
        try:
            declared = request.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > settings.max_body_bytes:
                response = JSONResponse(error_body("payload_too_large", "request body too large"), status_code=413)
            else:
                response = await call_next(request)
            response.headers["X-Request-ID"] = rid
            response.headers.setdefault("X-Content-Type-Options", "nosniff")
            response.headers.setdefault("Cache-Control", "no-store")
            logger.info("%s %s -> %s", request.method, request.url.path, response.status_code,
                        extra={"fields": {"status": response.status_code,
                                          "duration_ms": round((time.perf_counter() - started) * 1000, 1)}})
            return response
        finally:
            request_id_var.reset(token)

    @app.exception_handler(HTTPException)
    async def http_error(_: Request, exc: HTTPException) -> JSONResponse:
        body = exc.detail if isinstance(exc.detail, dict) and "error" in exc.detail else error_body("http_error", str(exc.detail))
        return JSONResponse(body, status_code=exc.status_code, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def bad_request(_: Request, exc: RequestValidationError) -> JSONResponse:
        details = [{"loc": list(e["loc"]), "message": e["msg"]} for e in exc.errors()[:50]]
        return JSONResponse(error_body("invalid_request", "request is invalid", details), status_code=422)

    @app.exception_handler(Exception)
    async def unexpected(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled error")  # details go to the log, never to the client
        return JSONResponse(error_body("internal_error", "unexpected server error"), status_code=500)

    @app.get("/healthz", tags=["ops"])
    def healthz() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    for module in ALL:
        app.include_router(module.router, prefix="/api/v1")
    return app

