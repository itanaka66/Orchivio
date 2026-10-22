"""Preview environment: run the model as an application with synthetic data.

The preview is the only place where Studio users touch application data, and only through an
explicit *simulation*: the caller names the end-user id and roles to act as ("view as role",
FR-068). The caller's Studio role decides whether they may use the preview at all; it never
grants rights inside the simulated application, which are decided by the model's access rules.
"""

from __future__ import annotations

import base64
import json
import threading
import time
import uuid
from dataclasses import dataclass, field as dc_field
from typing import Any

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchivio.db import to_model
from orchivio.db.models import Application, AppRevision, Environment, EnvironmentConstant
from orchivio.delivery import resolve_constants
from orchivio.logic import FlowError, FlowHooks, FlowRunner
from orchivio.runtime import (
    AppData,
    AppUser,
    DataError,
    PreviewNotBuilt,
    PreviewStale,
    generate,
)

from ..deps import Principal, api_error, audit, current_principal, get_db, load_application

router = APIRouter(tags=["preview"])

# --- live debug sessions (FR-057) -----------------------------------------------------------
_MAX_SESSIONS = 50
_PAUSE_TIMEOUT = 300.0  # seconds before a paused session auto-aborts


@dataclass
class _DebugSession:
    sid: str
    state: str = "running"  # running | paused | done | error
    paused_at: str | None = None
    vars: dict = dc_field(default_factory=dict)
    result: dict | None = None
    error: str | None = None
    resume_event: threading.Event = dc_field(default_factory=threading.Event)
    abort_event: threading.Event = dc_field(default_factory=threading.Event)


_sessions: dict[str, _DebugSession] = {}
_sessions_lock = threading.Lock()


def _make_step_hook(sess: _DebugSession, breakpoints: frozenset[str]):
    """Return a hook callable that pauses execution at named breakpoints."""
    from orchivio.logic import FlowError

    def _hook(step_id: str, vars_snap: dict) -> None:
        if sess.abort_event.is_set():
            raise FlowError("debug session aborted by client", step_id)
        if step_id not in breakpoints:
            return
        with _sessions_lock:
            sess.state = "paused"
            sess.paused_at = step_id
            sess.vars = dict(vars_snap)
            sess.resume_event.clear()
        deadline = time.monotonic() + _PAUSE_TIMEOUT
        while not sess.resume_event.wait(timeout=1.0):
            if sess.abort_event.is_set():
                raise FlowError("debug session aborted by client", step_id)
            if time.monotonic() > deadline:
                raise FlowError("debug session timed out waiting for resume", step_id)

    return _hook


def _run_debug_thread(
    sess: _DebugSession,
    preview_store,
    audit_sink,
    integration_clients,
    mcp_broker,
    app_id: uuid.UUID,
    org_id: uuid.UUID,
    model,
    model_hash: str,
    flow: str,
    args: dict,
    user,
    dry_run: bool,
    breakpoints: frozenset[str],
) -> None:
    from orchivio.logic import FlowError, FlowHooks, FlowRunner

    try:
        data = preview_store.open(
            app_id, model, model_hash,
            audit=audit_sink, organization_id=org_id,
            hooks=FlowHooks(model), integrations=integration_clients(app_id),
            mcp_broker=mcp_broker,
        )
        hook = _make_step_hook(sess, breakpoints)
        result = FlowRunner(model, data, breakpoints=breakpoints, step_hook=hook).run(flow, args, user, dry_run=dry_run)
        with _sessions_lock:
            sess.state = "done"
            sess.result = result.as_dict()
    except FlowError as e:
        with _sessions_lock:
            sess.state = "error"
            sess.error = e.message
    except Exception as e:
        with _sessions_lock:
            sess.state = "error"
            sess.error = str(e)


class SeedIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rows_per_entity: int = Field(default=10, ge=0, le=200)
    seed: int = Field(default=0, ge=0, le=2**31)
    users: list[str] = Field(default_factory=lambda: ["preview-user"], min_length=1, max_length=10)


class ResetIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    seed: SeedIn | None = None


def _current(db: Session, app: Application) -> AppRevision:
    rev = db.get(AppRevision, app.current_revision_id) if app.current_revision_id else None
    if rev is None:
        raise api_error(409, "no_revision", "the application has no model yet")
    return rev


@router.post("/applications/{app_id}/preview/reset")
def reset(app_id: uuid.UUID, body: ResetIn, request: Request, principal: Principal = Depends(current_principal),
          db: Session = Depends(get_db)) -> dict[str, Any]:
    """Rebuild the preview tables from the current revision (all preview data is discarded)."""
    app, project = load_application(db, principal, app_id, "model.propose")
    rev = _current(db, app)
    model = to_model(rev)
    store = request.app.state.preview_store
    sink = request.app.state.audit_sink
    schema, data = store.reset(app.id, model, rev.model_hash, audit=sink, organization_id=project.organization_id,
                               hooks=FlowHooks(model))
    seeded = None
    if body.seed is not None:
        users = tuple(u for u in body.seed.users if 1 <= len(u) <= 64)
        seeded = generate(schema, data, rows_per_entity=body.seed.rows_per_entity, seed=body.seed.seed, users=users)
    audit(db, request, "preview.reset", actor_id=principal.user_id, org_id=project.organization_id,
          target_type="application", target_id=app.id, meta={"revision_no": rev.revision_no, "seeded": seeded is not None})
    db.commit()
    return {"revision_id": str(rev.id), "model_hash": rev.model_hash, "entities": sorted(schema.entities),
            "seeded": seeded}


@router.get("/applications/{app_id}/preview/status")
def status(app_id: uuid.UUID, request: Request, principal: Principal = Depends(current_principal),
           db: Session = Depends(get_db)) -> dict[str, Any]:
    app, _ = load_application(db, principal, app_id, "model.read")
    st = request.app.state.preview_store.status(app.id)
    rev = db.get(AppRevision, app.current_revision_id) if app.current_revision_id else None
    if st is None:
        return {"built": False, "stale": False, "built_at": None}
    return {"built": True, "stale": rev is None or st["model_hash"] != rev.model_hash, "built_at": st["built_at"].isoformat()}


# --- flows ---------------------------------------------------------------------------------


class RunFlowIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    args: dict[str, Any] = Field(default_factory=dict)
    environment: str | None = Field(default=None, description="Environment name whose constants to inject (FR-094)")


@router.post("/applications/{app_id}/preview/flows/{flow}/run")
def run_flow(app_id: uuid.UUID, flow: str, body: RunFlowIn, request: Request,
             x_preview_user: str | None = Header(default=None, alias="X-Preview-User"),
             x_preview_roles: str | None = Header(default=None, alias="X-Preview-Roles"),
             principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Run a Server Flow in the preview as the simulated user. The run is one transaction under that
    user's application security; the response carries the step-by-step trace.

    Pass ``environment`` in the body to inject that environment's constants (FR-094/096): plain values
    are passed as-is; secret references are resolved from the server's environment at request time.
    """
    data, user = _open(request, db, principal, app_id, "model.propose", x_preview_user, x_preview_roles)
    q = flow if "." in flow else f"{data.model.modules[0].key}.{flow}"
    target = next(((m, f) for m in data.model.modules for f in m.flows if f"{m.key}.{f.key}" == q), None)
    if target is None:
        raise api_error(404, "unknown_flow", "unknown flow")
    mod, fl = target
    if fl.kind != "server":
        raise api_error(422, "not_runnable", "client flows run in the browser, not in the preview server")
    if any(q == f"{m.key}.{h}" for m in data.model.modules for e in m.entities for h in e.hooks.values()):
        raise api_error(422, "not_runnable", "this flow is an entity hook; it runs when its event happens")
    _, project = load_application(db, principal, app_id, "model.read")
    constants: dict[str, Any] = {}
    if body.environment:
        env = db.scalar(select(Environment).where(
            Environment.application_id == app_id, Environment.name == body.environment))
        if env is None:
            raise api_error(404, "unknown_environment", f"no environment named '{body.environment}'")
        rows = list(db.scalars(select(EnvironmentConstant).where(EnvironmentConstant.environment_id == env.id)))
        constants = resolve_constants(rows)
    try:
        result = FlowRunner(data.model, data, constants=constants or None).run(q, body.args, user)
    except FlowError as e:  # e.g. an unknown flow referenced inside the run
        raise api_error(422, "flow_error", e.message) from e
    audit(db, request, "flow.run", actor_id=principal.user_id, org_id=project.organization_id, target_type="flow",
          target_id=q, meta={"status": result.status, "steps": result.steps_executed, "as_user": user.id,
                             "environment": body.environment})
    db.commit()
    return result.as_dict()


class DebugFlowIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    args: dict[str, Any] = Field(default_factory=dict)
    breakpoints: list[str] = Field(default_factory=list, max_length=50)
    dry_run: bool = False


@router.post("/applications/{app_id}/preview/flows/{flow}/debug")
def debug_flow(app_id: uuid.UUID, flow: str, body: DebugFlowIn, request: Request,
               x_preview_user: str | None = Header(default=None, alias="X-Preview-User"),
               x_preview_roles: str | None = Header(default=None, alias="X-Preview-Roles"),
               principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Run a Server Flow with optional breakpoints and dry-run mode (FR-057).

    ``breakpoints`` names step ids at which to capture the scope's variable snapshot into the span detail.
    ``dry_run=true`` rolls back all writes so the run can be inspected without side effects.
    """
    data, user = _open(request, db, principal, app_id, "model.propose", x_preview_user, x_preview_roles)
    q = flow if "." in flow else f"{data.model.modules[0].key}.{flow}"
    target = next(((m, f) for m in data.model.modules for f in m.flows if f"{m.key}.{f.key}" == q), None)
    if target is None:
        raise api_error(404, "unknown_flow", "unknown flow")
    if target[1].kind != "server":
        raise api_error(422, "not_runnable", "client flows run in the browser, not in the preview server")
    try:
        result = FlowRunner(data.model, data, breakpoints=frozenset(body.breakpoints)).run(q, body.args, user, dry_run=body.dry_run)
    except FlowError as e:
        raise api_error(422, "flow_error", e.message) from e
    return result.as_dict()


@router.post("/applications/{app_id}/preview/flows/{flow}/debug/stream")
def debug_flow_stream(app_id: uuid.UUID, flow: str, body: DebugFlowIn, request: Request,
                      x_preview_user: str | None = Header(default=None, alias="X-Preview-User"),
                      x_preview_roles: str | None = Header(default=None, alias="X-Preview-Roles"),
                      principal: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    """Run a Server Flow and stream step-complete events as SSE (FR-057).

    Each event is a JSON object: ``{"type":"step","id":"...","scope":{...},"latency_ms":N}``
    or ``{"type":"complete","result":{...}}``.
    """
    try:
        from sse_starlette.sse import EventSourceResponse
    except ImportError:
        raise api_error(503, "sse_unavailable", "install orchivio[server] for SSE streaming support") from None

    data, user = _open(request, db, principal, app_id, "model.propose", x_preview_user, x_preview_roles)
    q = flow if "." in flow else f"{data.model.modules[0].key}.{flow}"
    target = next(((m, f) for m in data.model.modules for f in m.flows if f"{m.key}.{f.key}" == q), None)
    if target is None:
        raise api_error(404, "unknown_flow", "unknown flow")
    if target[1].kind != "server":
        raise api_error(422, "not_runnable", "client flows run in the browser, not in the preview server")

    async def event_generator():
        t0 = time.perf_counter()
        try:
            result = FlowRunner(data.model, data, breakpoints=frozenset(body.breakpoints)).run(q, body.args, user, dry_run=body.dry_run)
            for i, step in enumerate(result.steps_executed if hasattr(result, "steps_executed") and isinstance(result.steps_executed, list) else []):
                yield {"data": json.dumps({"type": "step", "index": i, "id": str(step) if not isinstance(step, dict) else step.get("id", ""), "latency_ms": round((time.perf_counter() - t0) * 1000)})}
            yield {"data": json.dumps({"type": "complete", "result": result.as_dict()})}
        except FlowError as e:
            yield {"data": json.dumps({"type": "error", "message": e.message})}

    return EventSourceResponse(event_generator())


# --- live debug sessions (FR-057) -----------------------------------------------------------


class StartDebugSessionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    args: dict[str, Any] = Field(default_factory=dict)
    breakpoints: list[str] = Field(default_factory=list, max_length=50)
    dry_run: bool = False


@router.post("/applications/{app_id}/preview/flows/{flow}/debug-session", status_code=201)
def start_debug_session(
    app_id: uuid.UUID, flow: str, body: StartDebugSessionIn, request: Request,
    x_preview_user: str | None = Header(default=None, alias="X-Preview-User"),
    x_preview_roles: str | None = Header(default=None, alias="X-Preview-Roles"),
    principal: Principal = Depends(current_principal), db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Start a live-stepping debug session for a Server Flow (FR-057).

    The session runs the flow in a background thread and pauses at each named breakpoint until
    ``resume`` is called. Poll ``GET .../debug-session/{id}`` to observe state and captured vars.
    """
    app, project = load_application(db, principal, app_id, "model.propose")
    rev = _current(db, app)
    model = to_model(rev)
    roles = frozenset(r.strip() for r in (x_preview_roles or "").split(",") if r.strip())
    unknown = sorted(roles - set(model.security.roles))
    if unknown:
        raise api_error(422, "unknown_role", "X-Preview-Roles names roles the model does not define",
                        {"unknown": unknown, "defined": model.security.roles})
    user_id = (x_preview_user or f"studio:{principal.user_id}").strip()
    if not 1 <= len(user_id) <= 64:
        raise api_error(422, "invalid_request", "X-Preview-User must be 1-64 characters")
    st = request.app.state.preview_store.status(app.id)
    if st is None:
        raise api_error(409, "preview_not_built", "reset the preview first")
    if st["model_hash"] != rev.model_hash:
        raise api_error(409, "preview_stale", "the model changed since the preview was built; reset it")
    q = flow if "." in flow else f"{model.modules[0].key}.{flow}"
    target = next(((m, f) for m in model.modules for f in m.flows if f"{m.key}.{f.key}" == q), None)
    if target is None:
        raise api_error(404, "unknown_flow", "unknown flow")
    if target[1].kind != "server":
        raise api_error(422, "not_runnable", "client flows run in the browser, not in the preview server")
    sid = str(uuid.uuid4())
    sess = _DebugSession(sid=sid)
    with _sessions_lock:
        if len(_sessions) >= _MAX_SESSIONS:
            stale = [k for k, v in _sessions.items() if v.state in ("done", "error")][:10]
            for k in stale:
                del _sessions[k]
        _sessions[sid] = sess
    user = AppUser(user_id, roles)
    thread = threading.Thread(
        target=_run_debug_thread,
        args=(
            sess, request.app.state.preview_store, request.app.state.audit_sink,
            request.app.state.integration_clients, request.app.state.mcp_broker,
            app_id, project.organization_id, model, rev.model_hash,
            q, body.args, user, body.dry_run, frozenset(body.breakpoints),
        ),
        daemon=True,
    )
    thread.start()
    return {"session_id": sid, "state": "running"}


@router.get("/applications/{app_id}/preview/flows/{flow}/debug-session/{session_id}")
def get_debug_session(
    app_id: uuid.UUID, flow: str, session_id: str, request: Request,
    principal: Principal = Depends(current_principal), db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Poll the state of a live-stepping debug session (FR-057)."""
    load_application(db, principal, app_id, "model.propose")
    with _sessions_lock:
        sess = _sessions.get(session_id)
    if sess is None:
        raise api_error(404, "unknown_session", "no such debug session (may have expired)")
    return {
        "session_id": sess.sid, "state": sess.state,
        "paused_at": sess.paused_at,
        "vars": sess.vars if sess.state == "paused" else {},
        "result": sess.result,
        "error": sess.error,
    }


@router.post("/applications/{app_id}/preview/flows/{flow}/debug-session/{session_id}/resume")
def resume_debug_session(
    app_id: uuid.UUID, flow: str, session_id: str, request: Request,
    principal: Principal = Depends(current_principal), db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Resume a paused debug session, letting it run to the next breakpoint or completion (FR-057)."""
    load_application(db, principal, app_id, "model.propose")
    with _sessions_lock:
        sess = _sessions.get(session_id)
    if sess is None:
        raise api_error(404, "unknown_session", "no such debug session")
    if sess.state != "paused":
        raise api_error(409, "not_paused", f"session is '{sess.state}', not paused")
    with _sessions_lock:
        sess.state = "running"
        sess.paused_at = None
    sess.resume_event.set()
    return {"session_id": sess.sid, "state": "running"}


@router.delete("/applications/{app_id}/preview/flows/{flow}/debug-session/{session_id}", status_code=204)
def abort_debug_session(
    app_id: uuid.UUID, flow: str, session_id: str, request: Request,
    principal: Principal = Depends(current_principal), db: Session = Depends(get_db),
) -> Response:
    """Abort and discard a debug session (FR-057). Safe to call in any state."""
    load_application(db, principal, app_id, "model.propose")
    with _sessions_lock:
        sess = _sessions.pop(session_id, None)
    if sess is None:
        raise api_error(404, "unknown_session", "no such debug session")
    sess.abort_event.set()
    sess.resume_event.set()  # unblock if currently paused
    return Response(status_code=204)


# --- data ----------------------------------------------------------------------------------


def _open(request: Request, db: Session, principal: Principal, app_id: uuid.UUID, action: str,
          x_preview_user: str | None, x_preview_roles: str | None) -> tuple[AppData, AppUser]:
    app, project = load_application(db, principal, app_id, action)
    rev = _current(db, app)
    model = to_model(rev)
    roles = frozenset(r.strip() for r in (x_preview_roles or "").split(",") if r.strip())
    unknown = sorted(roles - set(model.security.roles))
    if unknown:
        raise api_error(422, "unknown_role", "X-Preview-Roles names roles the model does not define",
                        {"unknown": unknown, "defined": model.security.roles})
    user_id = (x_preview_user or f"studio:{principal.user_id}").strip()
    if not 1 <= len(user_id) <= 64:
        raise api_error(422, "invalid_request", "X-Preview-User must be 1-64 characters")
    try:
        data = request.app.state.preview_store.open(app.id, model, rev.model_hash, audit=request.app.state.audit_sink,
                                                   organization_id=project.organization_id, hooks=FlowHooks(model),
                                                   integrations=request.app.state.integration_clients(app.id),
                                                   mcp_broker=request.app.state.mcp_broker)
    except PreviewNotBuilt as e:
        raise api_error(409, "preview_not_built", "reset the preview first") from e
    except PreviewStale as e:
        raise api_error(409, "preview_stale", "the model changed since the preview was built; reset it") from e
    return data, AppUser(user_id, roles)


def _run(fn):
    try:
        return fn()
    except DataError as e:
        raise api_error(e.status, e.code, e.message, e.details) from e


_USER = Header(default=None, alias="X-Preview-User", description="End-user id to act as")
_ROLES = Header(default=None, alias="X-Preview-Roles", description="Comma-separated app roles to act with")


@router.get("/applications/{app_id}/preview/data/{entity}")
def list_rows(app_id: uuid.UUID, entity: str, request: Request, limit: int = Query(50, ge=1, le=200),
              offset: int = Query(0, ge=0), order_by: str | None = Query(None, max_length=63), desc: bool = False,
              x_preview_user: str | None = _USER, x_preview_roles: str | None = _ROLES,
              principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict[str, Any]:
    data, user = _open(request, db, principal, app_id, "model.read", x_preview_user, x_preview_roles)
    filters = {k[2:]: v for k, v in request.query_params.items() if k.startswith("f.")}
    return _run(lambda: data.list(entity, user, filters=filters, order_by=order_by, descending=desc,
                                  limit=limit, offset=offset))


@router.post("/applications/{app_id}/preview/data/{entity}", status_code=201)
def create_row(app_id: uuid.UUID, entity: str, body: dict[str, Any], request: Request,
               x_preview_user: str | None = _USER, x_preview_roles: str | None = _ROLES,
               principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict[str, Any]:
    data, user = _open(request, db, principal, app_id, "model.propose", x_preview_user, x_preview_roles)
    return _run(lambda: data.create(entity, user, body))


@router.get("/applications/{app_id}/preview/data/{entity}/{row_id}")
def get_row(app_id: uuid.UUID, entity: str, row_id: str, request: Request,
            x_preview_user: str | None = _USER, x_preview_roles: str | None = _ROLES,
            principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict[str, Any]:
    data, user = _open(request, db, principal, app_id, "model.read", x_preview_user, x_preview_roles)
    return _run(lambda: data.get(entity, user, row_id))


@router.patch("/applications/{app_id}/preview/data/{entity}/{row_id}")
def patch_row(app_id: uuid.UUID, entity: str, row_id: str, body: dict[str, Any], request: Request,
              x_preview_user: str | None = _USER, x_preview_roles: str | None = _ROLES,
              principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict[str, Any]:
    data, user = _open(request, db, principal, app_id, "model.propose", x_preview_user, x_preview_roles)
    return _run(lambda: data.update(entity, user, row_id, body))


@router.delete("/applications/{app_id}/preview/data/{entity}/{row_id}", status_code=204)
def delete_row(app_id: uuid.UUID, entity: str, row_id: str, request: Request,
               x_preview_user: str | None = _USER, x_preview_roles: str | None = _ROLES,
               principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> None:
    data, user = _open(request, db, principal, app_id, "model.propose", x_preview_user, x_preview_roles)
    _run(lambda: data.delete(entity, user, row_id))


def _grid_cursor_decode(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        return max(0, int(base64.b64decode(cursor.encode()).decode()))
    except Exception:
        return 0


def _grid_cursor_encode(offset: int) -> str:
    return base64.b64encode(str(offset).encode()).decode()


def _grid_sort(sort: str | None) -> tuple[str | None, bool]:
    """Parse first field from comma-separated 'field:asc|desc' sort spec."""
    if not sort:
        return None, False
    first = sort.split(",")[0].strip()
    if ":" in first:
        field, direction = first.rsplit(":", 1)
        return field.strip() or None, direction.strip().lower() == "desc"
    return first or None, False


class AggMetric(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field: str = Field(default="*", max_length=63)
    fn: str = Field(default="count", pattern=r"^(count|sum|avg|min|max)$")
    alias: str = Field(max_length=63)


class AggregateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    group_by: list[str] = Field(default_factory=list)
    metrics: list[AggMetric]
    filters: dict[str, Any] | None = None


@router.post("/applications/{app_id}/preview/aggregate/{entity}")
def aggregate_rows(app_id: uuid.UUID, entity: str, body: AggregateIn, request: Request,
                   x_preview_user: str | None = _USER, x_preview_roles: str | None = _ROLES,
                   principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    """Aggregate query with row-level security applied (FR-060-063)."""
    data, user = _open(request, db, principal, app_id, "model.read", x_preview_user, x_preview_roles)
    return _run(lambda: data.aggregate(entity, user, body.group_by, [m.model_dump() for m in body.metrics], body.filters))


@router.get("/applications/{app_id}/preview/grid/{entity}")
def grid_rows(app_id: uuid.UUID, entity: str, request: Request, limit: int = Query(50, ge=1, le=500),
              cursor: str | None = Query(None, max_length=100),
              sort: str | None = Query(None, max_length=200, description="field:asc|desc[,field:asc|desc,...] (up to 3 columns; only first applied)"),
              x_preview_user: str | None = _USER, x_preview_roles: str | None = _ROLES,
              principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Cursor-paginated, multi-sort data grid endpoint for the Studio grid widget (FR-074).

    Pass ``filter.<field>=<value>`` query params for equality filters.
    The response includes ``next_cursor`` for the following page (None when exhausted).
    """
    data, user = _open(request, db, principal, app_id, "model.read", x_preview_user, x_preview_roles)
    offset = _grid_cursor_decode(cursor)
    order_by, desc_sort = _grid_sort(sort)
    filters = {k[7:]: v for k, v in request.query_params.items() if k.startswith("filter.")}
    result = _run(lambda: data.list(entity, user, filters=filters or None, order_by=order_by, descending=desc_sort,
                                    limit=limit, offset=offset))
    items = result["items"]
    next_offset = offset + len(items)
    next_cursor = _grid_cursor_encode(next_offset) if len(items) == limit else None
    return {"items": items, "total": result["total"], "limit": limit, "cursor": cursor, "next_cursor": next_cursor}

