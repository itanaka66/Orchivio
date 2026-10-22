"""Studio MCP server (FR-086): JSON-RPC 2.0 over Streamable HTTP, request/response only (no SSE stream).

Governance (DESIGN.md §7.5):

* Bearer API tokens only. Sessions are refused, so the cookie/CSRF path cannot be abused by a page.
* Each tool runs the same code, with the same permission checks and token scopes, as the REST route it
  mirrors. A token scoped to ``model.read`` therefore cannot call ``proposal.create`` or ``tests.run``.
* Writes are proposals (``source = "mcp"``) and always wait for a human decision in the Studio. There is no
  tool that applies a proposal or creates a revision, so an agent cannot raise its own approval level.
* Every call is audited with the tool, a hash of the arguments and the outcome; a per-token rate limit applies.
* Arguments are validated strictly (unknown keys are rejected); tool failures are tool results with
  ``isError`` so the agent can read them, protocol failures are JSON-RPC errors.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections import deque
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.exceptions import HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.orm import Session

from orchivio import __version__
from orchivio.db import ModelInvalid, ProposalRepository, to_model
from orchivio.db.proposals import NoChanges
from orchivio.model import flatten, validate_model
from orchivio.model import lint as lint_model

from ..deps import (
    Principal,
    api_error,
    audit,
    current_principal,
    get_db,
    load_application,
    load_revision,
)
from ..modelio import load_model
from .projects import element_usages, list_applications, revision_lint
from .proposals import proposal_detail
from .testruns import RunIn, start

router = APIRouter(tags=["mcp"])

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26")
MAX_BODY = 1_000_000
MAX_RESULT_CHARS = 200_000

PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS = -32700, -32600, -32601, -32602


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProjectArgs(_Args):
    project_id: uuid.UUID


class Target(_Args):
    """A revision, or an application (meaning its current revision)."""

    revision_id: uuid.UUID | None = None
    application_id: uuid.UUID | None = None


class ReadArgs(Target):
    element_id: str | None = Field(default=None, description="Return only this element (stable id)")


class SearchArgs(Target):
    query: str = Field(min_length=1, max_length=200)
    limit: int = Field(default=25, ge=1, le=100)


class UsagesArgs(Target):
    element_id: uuid.UUID


class ValidateArgs(_Args):
    model: dict[str, Any]


class LintArgs(Target):
    include_suppressed: bool = False


class ProposeArgs(_Args):
    application_id: uuid.UUID
    model: dict[str, Any]
    title: str = Field(min_length=1, max_length=300)


class ProposalGetArgs(_Args):
    proposal_id: uuid.UUID
    include_model: bool = False


class ProposalListArgs(_Args):
    application_id: uuid.UUID
    status: str | None = Field(default=None, pattern="^(pending|rejected|applied|superseded)$")
    limit: int = Field(default=20, ge=1, le=100)


class ExplainArgs(_Args):
    application_id: uuid.UUID
    element_id: uuid.UUID
    question: str | None = Field(default=None, max_length=2000)


class ProposalAwaitArgs(_Args):
    proposal_id: uuid.UUID
    timeout_seconds: int = Field(default=60, ge=5, le=300)


class TestsArgs(Target):
    seed: int = Field(default=0, ge=0, le=2**31)
    rows: int = Field(default=20, ge=1, le=100)


class Ctx:
    def __init__(self, request: Request, principal: Principal, db: Session) -> None:
        self.request, self.principal, self.db = request, principal, db

    def revision(self, a: Target, action: str = "model.read"):
        if (a.revision_id is None) == (a.application_id is None):
            raise api_error(422, "invalid_arguments", "give exactly one of revision_id and application_id")
        if a.revision_id is not None:
            return load_revision(self.db, self.principal, a.revision_id, action)[0]
        app, _ = load_application(self.db, self.principal, a.application_id, action)
        if app.current_revision_id is None:
            raise api_error(409, "no_revision", "the application has no revision yet")
        return load_revision(self.db, self.principal, app.current_revision_id, action)[0]


# --- tools -------------------------------------------------------------------


def t_applications(c: Ctx, a: ProjectArgs) -> Any:
    page = list_applications(a.project_id, 100, 0, c.principal, c.db)
    return {"items": [i.model_dump(mode="json") for i in page.items], "total": page.total}


def t_read(c: Ctx, a: ReadArgs) -> Any:
    rev = c.revision(a)
    model = to_model(rev)
    if a.element_id is None:
        return {"revision_id": str(rev.id), "revision_no": rev.revision_no, "model_hash": rev.model_hash,
                "model": model.model_dump(mode="json", exclude_none=True)}
    node = flatten(model).nodes.get(a.element_id)
    if node is None:
        raise api_error(404, "not_found", "no such element in this revision")
    return {"revision_id": str(rev.id), "element_id": node.id, "kind": node.kind, "parent": node.parent, "properties": node.props}


def t_search(c: Ctx, a: SearchArgs) -> Any:
    rev = c.revision(a)
    q = a.query.lower()
    hits = [{"element_id": n.id, "kind": n.kind, "name": n.label, "parent": n.parent}
            for n in flatten(to_model(rev)).nodes.values()
            if n.kind not in ("root",) and (q in n.label.lower() or q in n.kind)]
    hits.sort(key=lambda h: (h["name"].lower() != q, h["kind"], h["name"]))
    return {"revision_id": str(rev.id), "total": len(hits), "items": hits[: a.limit]}


def t_usages(c: Ctx, a: UsagesArgs) -> Any:
    rev = c.revision(a)
    return element_usages(rev.id, a.element_id, c.principal, c.db)


def t_validate(c: Ctx, a: ValidateArgs) -> Any:
    model = load_model(a.model)
    return {"issues": [i.as_dict() for i in validate_model(model)],
            "lint": [f.as_dict() for f in lint_model(model) if not f.suppressed]}


def t_lint(c: Ctx, a: LintArgs) -> Any:
    rev = c.revision(a)
    return revision_lint(rev.id, a.include_suppressed, c.principal, c.db)


def t_propose(c: Ctx, a: ProposeArgs) -> Any:
    app, project = load_application(c.db, c.principal, a.application_id, "model.propose")
    try:
        p = ProposalRepository(c.db).create(app.id, load_model(a.model), source="mcp", title=a.title,
                                            created_by=c.principal.user_id, organization_id=project.organization_id)
    except ModelInvalid as e:
        raise api_error(422, "model_invalid", "the model has validation errors",
                        [i.as_dict() for i in e.issues if i.severity == "error"]) from e
    except NoChanges as e:
        raise api_error(409, "no_changes", str(e)) from e
    d = proposal_detail(p).model_dump(mode="json")
    d["note"] = "Created as pending. A person must approve it in the Studio; agents cannot apply proposals."
    return d


def t_proposal_get(c: Ctx, a: ProposalGetArgs) -> Any:
    from .proposals import get_proposal

    return get_proposal(a.proposal_id, a.include_model, c.principal, c.db).model_dump(mode="json")


def t_proposal_list(c: Ctx, a: ProposalListArgs) -> Any:
    from .proposals import list_proposals

    page = list_proposals(a.application_id, a.status, a.limit, 0, c.principal, c.db)
    return {"total": page.total, "items": [i.model_dump(mode="json") for i in page.items]}


def t_explain(c: Ctx, a: ExplainArgs) -> Any:
    from .ai import ExplainIn
    from .ai import explain as _explain
    return _explain(a.application_id, ExplainIn(element_id=a.element_id, question=a.question), c.request, c.principal, c.db)


def t_proposal_await(c: Ctx, a: ProposalAwaitArgs) -> Any:
    import time as _time

    from .proposals import get_proposal
    deadline = _time.monotonic() + a.timeout_seconds
    while True:
        p = get_proposal(a.proposal_id, False, c.principal, c.db).model_dump(mode="json")
        if p["status"] != "pending":
            return {"approved": p["status"] == "applied", "status": p["status"],
                    "decision_by": p.get("reviewed_by"), "decided_at": p.get("reviewed_at")}
        remaining = deadline - _time.monotonic()
        if remaining <= 0:
            return {"approved": False, "status": "pending", "timed_out": True}
        _time.sleep(min(5.0, remaining))


def t_tests(c: Ctx, a: TestsArgs) -> Any:
    rev = c.revision(a, "model.propose")
    return start(rev.id, RunIn(seed=a.seed, rows=a.rows), c.request, c.principal, c.db)


# name -> (description, approval level, argument model, handler)
TOOLS: dict[str, tuple[str, str, type[_Args], Callable[[Ctx, Any], Any]]] = {
    "application.list": ("List the applications of a project.", "A0", ProjectArgs, t_applications),
    "model.read": ("Read the model of a revision (or of an application's current revision), or one element by id.",
                   "A0", ReadArgs, t_read),
    "model.search": ("Find elements by name or kind; returns stable element ids.", "A0", SearchArgs, t_search),
    "model.usages": ("Find the elements that depend on an element.", "A0", UsagesArgs, t_usages),
    "model.validate": ("Validate and lint a model document without storing it.", "A0", ValidateArgs, t_validate),
    "model.lint": ("Stored lint findings of a revision.", "A0", LintArgs, t_lint),
    "proposal.create": ("Submit a complete model as a change proposal. It is never applied directly: a person approves "
                        "it in the Studio. Keep element ids stable by starting from model.read output.",
                        "A1", ProposeArgs, t_propose),
    "proposal.get": ("Read a proposal: status, diff, risk level, lint and decision.", "A0", ProposalGetArgs, t_proposal_get),
    "proposal.list": ("List proposals of an application.", "A0", ProposalListArgs, t_proposal_list),
    "tests.run": ("Run the generated tests of a revision in an isolated preview database.", "A1", TestsArgs, t_tests),
    "model.explain": ("Explain one model element (entity, field, flow, process, role…) in plain language, with citations. "
                      "Read-only; requires ai_allow_external to be enabled for the project.",
                      "A0", ExplainArgs, t_explain),
    "proposal.await": ("Poll a proposal until it leaves 'pending' state (approved or rejected) or a timeout elapses. "
                       "Returns {approved, status, decision_by, decided_at}. Use after proposal.create.",
                       "A0", ProposalAwaitArgs, t_proposal_await),
}


def _tool_list() -> list[dict[str, Any]]:
    out = []
    for name, (desc, level, args, _) in TOOLS.items():
        out.append({"name": name, "description": f"{desc} (approval level {level})",
                    "inputSchema": args.model_json_schema(),
                    "annotations": {"readOnlyHint": level == "A0", "destructiveHint": False, "idempotentHint": level == "A0",
                                    "openWorldHint": False}})
    return out


# --- protocol ----------------------------------------------------------------


class RateLimiter:
    def __init__(self, per_minute: int) -> None:
        self.per_minute = per_minute
        self.hits: dict[uuid.UUID, deque[float]] = {}

    def allow(self, key: uuid.UUID, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        q = self.hits.setdefault(key, deque())
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= self.per_minute:
            return False
        q.append(now)
        return True


def _rpc_error(id_: Any, code: int, message: str, status: int = 200, data: Any = None) -> JSONResponse:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return JSONResponse({"jsonrpc": "2.0", "id": id_, "error": err}, status_code=status)


def _ok(id_: Any, result: Any) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": id_, "result": result})


def _tool_result(data: Any, error: bool = False) -> dict[str, Any]:
    text = json.dumps(data, ensure_ascii=False, default=str)
    if len(text) > MAX_RESULT_CHARS:
        data = {"truncated": True, "message": "result too large; narrow the request (element_id, query, limit)"}
        text, error = json.dumps(data), True
    out: dict[str, Any] = {"content": [{"type": "text", "text": text}], "isError": error}
    if not error and isinstance(data, dict):
        out["structuredContent"] = data
    return out


def _http_error_payload(e: HTTPException) -> dict[str, Any]:
    d = e.detail
    err = d.get("error", {}) if isinstance(d, dict) else {}
    return {"error": {"code": err.get("code", "error"), "message": err.get("message", str(d)), "details": err.get("details"),
                      "status": e.status_code}}


def _call_tool(ctx: Ctx, params: dict[str, Any]) -> tuple[dict[str, Any], str, bool]:
    name = params.get("name")
    entry = TOOLS.get(name) if isinstance(name, str) else None
    if entry is None:
        return _tool_result({"error": {"code": "unknown_tool", "message": f"unknown tool {name!r}"}}, True), str(name)[:80], False
    _, _, args_model, handler = entry
    raw = params.get("arguments") or {}
    try:
        if not isinstance(raw, dict):
            raise TypeError("arguments must be an object")
        args = args_model.model_validate(raw)
        data = handler(ctx, args)
        ctx.db.commit()
        return _tool_result(data), name, True
    except ValidationError as e:
        ctx.db.rollback()
        problems = [{"path": "/".join(str(p) for p in x["loc"]), "message": x["msg"]} for x in e.errors()][:20]
        return _tool_result({"error": {"code": "invalid_arguments", "message": "invalid arguments", "details": problems}}, True), name, False
    except TypeError as e:
        ctx.db.rollback()
        return _tool_result({"error": {"code": "invalid_arguments", "message": str(e)}}, True), name, False
    except HTTPException as e:
        ctx.db.rollback()
        return _tool_result(_http_error_payload(e), True), name, False


@router.get("/mcp")
def mcp_get(request: Request, principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> Response:
    """SSE stream of proposal status-change events for the current API token (FR-087)."""
    if principal.via != "token" or principal.token is None:
        raise api_error(401, "unauthorized", "the MCP SSE endpoint needs a bearer API token")
    if request.headers.get("accept") != "text/event-stream":
        return Response(status_code=405, headers={"Allow": "POST"})

    import asyncio

    from sqlalchemy import select as _select
    from sse_starlette.sse import EventSourceResponse

    from orchivio.db.models import Proposal

    org_id = principal.token.organization_id

    async def _stream():
        # Send initial ping, then poll every 5s for proposal changes created by this token.
        # In production this should use a notify/listen pattern instead of polling.
        from orchivio.db.models import Application, Project
        sent: set[str] = set()
        yield {"event": "ping", "data": "connected"}
        while True:
            await asyncio.sleep(5)
            try:
                proposals = db.scalars(
                    _select(Proposal)
                    .join(Application, Application.id == Proposal.application_id)
                    .join(Project, Project.id == Application.project_id)
                    .where(
                        Project.organization_id == org_id,
                        Proposal.created_by == principal.user_id,
                        Proposal.source == "mcp",
                    ).order_by(Proposal.created_at.desc()).limit(50)
                ).all()
                for p in proposals:
                    key = f"{p.id}:{p.status}"
                    if key not in sent:
                        sent.add(key)
                        yield {"event": "proposal_status", "data": json.dumps({
                            "proposal_id": str(p.id),
                            "status": p.status,
                            "title": p.title,
                        })}
            except Exception:
                yield {"event": "error", "data": "internal error"}
                return

    return EventSourceResponse(_stream())


@router.post("/mcp")
async def mcp_endpoint(request: Request, principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> Response:
    if principal.via != "token" or principal.token is None:
        raise api_error(401, "unauthorized", "the MCP endpoint needs a bearer API token")
    if int(request.headers.get("content-length") or 0) > MAX_BODY:
        return _rpc_error(None, INVALID_REQUEST, "request too large", 413)
    body = await request.body()
    if len(body) > MAX_BODY:
        return _rpc_error(None, INVALID_REQUEST, "request too large", 413)
    try:
        msg = json.loads(body)
    except ValueError:
        return _rpc_error(None, PARSE_ERROR, "parse error")
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return _rpc_error(None, INVALID_REQUEST, "expected a single JSON-RPC 2.0 object (batches are not supported)")
    method, id_, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if method is None:  # a response from the client; nothing to do
        return Response(status_code=202)
    if not isinstance(method, str) or not isinstance(params, dict) or isinstance(id_, (dict, list, bool)):
        return _rpc_error(id_ if isinstance(id_, (str, int)) else None, INVALID_REQUEST, "invalid request")
    if "id" not in msg:  # notification
        return Response(status_code=202)

    limiter: RateLimiter = request.app.state.mcp_limiter
    if method == "tools/call" and not limiter.allow(principal.token.id):
        return _rpc_error(id_, -32000, "rate limit exceeded; retry later", 429)

    if method == "initialize":
        wanted = params.get("protocolVersion")
        version = wanted if wanted in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
        return _ok(id_, {"protocolVersion": version, "capabilities": {"tools": {"listChanged": False}},
                         "serverInfo": {"name": "orchivio-studio", "version": __version__},
                         "instructions": "Read models with model.read/model.search, then submit changes with proposal.create. "
                                         "Proposals are applied only after a person approves them in the Studio."})
    if method == "ping":
        return _ok(id_, {})
    if method == "tools/list":
        return _ok(id_, {"tools": _tool_list()})
    if method == "tools/call":
        ctx = Ctx(request, principal, db)
        result, tool, ok = _call_tool(ctx, params)
        digest = hashlib.sha256(json.dumps(params.get("arguments"), sort_keys=True, default=str).encode()).hexdigest()
        org = principal.token.organization_id
        audit(db, request, "mcp.tool_call", actor_id=principal.user_id, org_id=org, target_type="api_token",
              target_id=principal.token.id, meta={"tool": tool, "args_sha256": digest, "ok": ok and not result["isError"],
                                                  "error": None if not result["isError"] else
                                                  json.loads(result["content"][0]["text"]).get("error", {}).get("code")})
        db.commit()
        return _ok(id_, result)
    return _rpc_error(id_, METHOD_NOT_FOUND, f"method not found: {method}")
