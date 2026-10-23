"""FastAPI application factory (resources and conventions: docs/API-SPEC.md)."""

from __future__ import annotations

import secrets
import threading
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

from orchivio import __version__, telemetry
from orchivio.db import make_engine, make_session_factory
from orchivio.db.schema_revision import SCHEMA_REVISION
from orchivio.gitsync import GitSyncService
from orchivio.integrations import (
    PinnedTransport,
    check_url,
    destination_policy,
    dispatcher_from_settings,
    integration_client_factory,
)
from orchivio.integrations.mcp_client import ToolBroker
from orchivio.integrations.oidc import OidcClient
from orchivio.jobs import Worker
from orchivio.llm import AnthropicProvider, LLMProvider
from orchivio.ports import LocalArtifactStore
from orchivio.process import ticker
from orchivio.runtime import PreviewStore

from . import app_log
from .audit_sink import DbAuditSink
from .metrics import Metrics
from .observability import configure_logging, logger, new_request_id, request_id_var
from .routes import ALL
from .routes.mcp import RateLimiter
from .settings import Settings, get_settings
from .throttle import DbLoginThrottle, DbRateLimiter


def error_body(code: str, message: str, details: Any = None) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "details": details}}


def _provider_from_settings(settings: Settings) -> LLMProvider | None:
    if settings.llm_provider == "anthropic":
        if settings.anthropic_api_key is None:
            raise RuntimeError("ORCHIVIO_ANTHROPIC_API_KEY is required when ORCHIVIO_LLM_PROVIDER=anthropic")
        kwargs = {"model": settings.llm_model} if settings.llm_model else {}
        return AnthropicProvider(settings.anthropic_api_key.get_secret_value(), **kwargs)
    return None


def route_template(request: Request) -> str:
    """The matched route with its path parameters in braces, e.g. ``/api/v1/projects/{project_id}``.
    Unmatched requests share one label so scanners cannot create unbounded series."""
    if request.scope.get("route") is None:
        return "unmatched"
    path = request.url.path
    for name, value in sorted(request.scope.get("path_params", {}).items(), key=lambda kv: -len(str(kv[1]))):
        path = path.replace(str(value), "{" + name + "}", 1)
    return path


def _auto_migrate(engine: Engine) -> None:
    """Run ``alembic upgrade head`` programmatically (ORCHIVIO_AUTO_MIGRATE=true).

    Looks for alembic.ini in the current working directory first, then relative to
    this file (project root). Skips with a warning when neither is found.
    """
    from pathlib import Path

    from alembic import command as alembic_command
    from alembic.config import Config as AlembicConfig

    ini = Path("alembic.ini")
    if not ini.exists():
        ini = Path(__file__).resolve().parents[3] / "alembic.ini"
    if not ini.exists():
        logger.warning("auto_migrate: alembic.ini not found; skipping (set cwd to project root or disable auto_migrate)")
        return
    cfg = AlembicConfig(str(ini))
    cfg.attributes["configure_logger"] = False
    with engine.connect() as conn:
        cfg.attributes["connection"] = conn
        alembic_command.upgrade(cfg, "head")
    logger.info("auto_migrate: database schema is up to date")


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

    _db_engine = engine or make_engine(settings.database_url)
    if session_factory is None:
        session_factory = make_session_factory(_db_engine)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if settings.auto_migrate:
            _auto_migrate(app.state.db_engine)
        stop, thread = threading.Event(), None
        if settings.worker_enabled:
            dispatcher = dispatcher_from_settings(settings, session_factory, app.state.preview_store.engine)
            worker = Worker(session_factory, app.state.preview_store, audit=app.state.audit_sink,
                            lease_seconds=settings.worker_lease_seconds, per_app_limit=settings.worker_per_app_limit,
                            extra_steps=(dispatcher.tick, ticker(session_factory, app.state.preview_store, audit=app.state.audit_sink,
                                                                 integrations=app.state.integration_clients)),
                            integrations=app.state.integration_clients)
            thread = threading.Thread(target=worker.run_forever, args=(settings.worker_interval_seconds, stop),
                                      name="orchivio-jobs", daemon=True)
            thread.start()
        try:
            yield
        finally:
            telemetry.shutdown()
            stop.set()
            if thread is not None:
                thread.join(timeout=5)

    app = FastAPI(title="Orchivio API", version=__version__, docs_url="/api/docs", openapi_url="/api/openapi.json",
                  lifespan=lifespan)
    app.state.settings = settings
    app.state.telemetry = telemetry.setup(settings, version=__version__)
    app.state.db_engine = _db_engine
    app.state.session_factory = session_factory
    app.state.preview_store = PreviewStore(preview_engine or make_engine(settings.preview_database_url))
    app.state.audit_sink = DbAuditSink(session_factory)
    app.state.git_sync = GitSyncService(allowed_hosts=tuple(settings.git_sync_allowed_hosts), allow_private=settings.webhook_allow_private_networks,
                                        timeout=settings.git_sync_timeout_seconds, git=settings.git_executable)
    app_log.install(session_factory, settings.app_log_retention_days)
    app.state.artifacts = LocalArtifactStore(settings.artifact_dir)
    app.state.llm_provider = llm_provider or _provider_from_settings(settings)
    app.state.metrics = Metrics()
    app.state.mcp_limiter = RateLimiter(settings.mcp_rate_per_minute)
    app.state.integration_clients = integration_client_factory(settings, session_factory)  # application id -> client
    app.state.mcp_transport = PinnedTransport(destination_policy(settings), timeout=settings.webhook_timeout_seconds)
    app.state.oidc = OidcClient(app.state.mcp_transport, check_endpoint=lambda u: check_url(u, destination_policy(settings)))
    app.state.mcp_broker = ToolBroker(session_factory, app.state.mcp_transport, timeout=settings.webhook_timeout_seconds)
    app.state.inbound_limiter = DbRateLimiter(session_factory, settings.inbound_rate_per_minute)
    app.state.throttle = DbLoginThrottle(session_factory, settings.login_max_failures, settings.login_window_seconds)

    @app.middleware("http")
    async def request_context(request: Request, call_next):  # noqa: ANN001, ANN202
        rid = new_request_id(request.headers.get("x-request-id"))
        token = request_id_var.set(rid)
        started = time.perf_counter()
        try:
            declared = request.headers.get("content-length")
            limit = settings.max_import_bytes if request.url.path.endswith("/import") else settings.max_body_bytes
            with telemetry.span(f"{request.method} {request.url.path}", kind="server", parent=telemetry.extract(request.headers),
                                **{"http.request.method": request.method, "request.id": rid}) as sp:
                if declared and declared.isdigit() and int(declared) > limit:
                    response = JSONResponse(error_body("payload_too_large", "request body too large"), status_code=413)
                else:
                    response = await call_next(request)
                template = route_template(request)
                sp.update_name(f"{request.method} {template}")  # the route template, never the raw path (it holds ids)
                sp.set_attributes({"http.route": template, "http.response.status_code": response.status_code})
                if response.status_code >= 500:
                    sp.set_error(f"HTTP {response.status_code}")
            elapsed = time.perf_counter() - started
            telemetry.record("orchivio.http.server.duration", elapsed, **{
                "http.request.method": request.method, "http.route": template,
                "http.response.status_class": f"{response.status_code // 100}xx"})
            app.state.metrics.observe(request.method, template, response.status_code, elapsed)
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
        """Liveness: the process is up. Does not touch the database."""
        return {"status": "ok", "version": __version__}

    def _database_checks() -> dict[str, str]:
        checks: dict[str, str] = {}
        try:
            with session_factory() as s:
                s.execute(text("SELECT 1"))
                try:
                    current = s.execute(text("SELECT version_num FROM alembic_version")).scalar()
                    checks["migrations"] = "ok" if current == SCHEMA_REVISION else "behind"
                except Exception:  # noqa: BLE001 - the table is missing: never migrated
                    checks["migrations"] = "missing"
            checks["database"] = "ok"
        except Exception:  # noqa: BLE001 - details go to the log, not to the caller
            logger.exception("readiness: database check failed")
            checks["database"] = "unavailable"
        try:
            with app.state.preview_store.engine.connect() as c:
                c.execute(text("SELECT 1"))
            checks["preview_database"] = "ok"
        except Exception:  # noqa: BLE001
            logger.exception("readiness: preview database check failed")
            checks["preview_database"] = "unavailable"
        return checks

    @app.get("/readyz", tags=["ops"])
    def readyz() -> JSONResponse:
        """Readiness: the databases answer and the schema is at the revision this code expects."""
        checks = _database_checks()
        ready = all(v == "ok" for v in checks.values())
        return JSONResponse({"status": "ready" if ready else "not_ready", "checks": checks}, status_code=200 if ready else 503)

    @app.get("/metrics", tags=["ops"], include_in_schema=False)
    def metrics(authorization: str | None = Header(default=None)) -> PlainTextResponse:
        token = settings.metrics_token
        if token is None:
            raise HTTPException(404, error_body("not_found", "not found"))  # disabled unless configured
        if authorization is None or not secrets.compare_digest(authorization.encode(), f"Bearer {token.get_secret_value()}".encode()):
            raise HTTPException(401, error_body("unauthorized", "invalid credentials"))
        db_ok = _database_checks().get("database") == "ok"
        return PlainTextResponse(app.state.metrics.render(__version__, {"orchivio_database_up": 1.0 if db_ok else 0.0}),
                                 media_type="text/plain; version=0.0.4")

    for module in ALL:
        app.include_router(module.router, prefix="/api/v1")
    return app

