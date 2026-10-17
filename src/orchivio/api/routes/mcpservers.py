"""External MCP servers the AI Builder may consult (FR-087, docs/AI-BUILDER.md §6).

Registering a server and enabling its tools are organization-admin actions (A3): they decide which third parties the
Builder may read from and therefore which third-party text can reach a model prompt. Only read-only tools at level A0
can be enabled, because the Builder has no human in the loop for anything stronger.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchivio.db.models import McpServer
from orchivio.integrations import DestinationError, SecretUnavailable, check_url, destination_policy, resolve_secret
from orchivio.integrations.dispatcher import SECRET_REF
from orchivio.integrations.mcp_client import McpClient, McpError

from ..deps import Principal, api_error, audit, authorize, current_principal, get_db, not_found

router = APIRouter(tags=["mcp client"])

NAME = r"^[a-z][a-z0-9-]{0,62}$"


class ServerIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=NAME)
    url: str = Field(max_length=2000)
    secret_ref: str | None = Field(default=None, max_length=300, description="env:NAME or file:/path with the bearer token")


class ServerPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str | None = Field(default=None, max_length=2000)
    secret_ref: str | None = Field(default=None, max_length=300)
    clear_secret: bool = False
    status: str | None = Field(default=None, pattern="^(active|disabled)$")


class ToolPolicyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool
    level: str = Field(default="A0", pattern="^A[0-4]$")


def server_out(s: McpServer) -> dict[str, Any]:
    policy = s.tool_policy or {}
    return {"id": str(s.id), "name": s.name, "url": s.url, "secret_ref": s.secret_ref, "status": s.status,
            "discovered_at": s.discovered_at.isoformat() if s.discovered_at else None,
            "tools": [{"name": t["name"], "description": t.get("description", ""), "input_schema": t.get("input_schema", {}),
                       "enabled": bool(policy.get(t["name"], {}).get("enabled")), "level": policy.get(t["name"], {}).get("level", "A0")}
                      for t in s.tools or []]}


def _load(db: Session, principal: Principal, org_id: uuid.UUID, name: str) -> McpServer:
    authorize(db, principal, org_id, "mcpserver.manage")
    s = db.scalar(select(McpServer).where(McpServer.organization_id == org_id, McpServer.name == name))
    if s is None:
        raise not_found("mcp server")
    return s


def _check(request: Request, url: str | None, secret_ref: str | None) -> None:
    if url is not None:
        try:
            check_url(url, destination_policy(request.app.state.settings))
        except DestinationError as e:
            raise api_error(422, "destination_refused", str(e)) from e
    if secret_ref is not None:
        if not SECRET_REF.match(secret_ref):
            raise api_error(422, "invalid_secret_ref", "use env:NAME or file:/path")
        try:
            resolve_secret(secret_ref)
        except SecretUnavailable as e:
            raise api_error(422, "secret_unavailable", str(e)) from e


@router.post("/organizations/{org_id}/mcp-servers", status_code=201)
def register(org_id: uuid.UUID, body: ServerIn, request: Request, principal: Principal = Depends(current_principal),
             db: Session = Depends(get_db)) -> dict[str, Any]:
    authorize(db, principal, org_id, "mcpserver.manage")
    _check(request, body.url, body.secret_ref)
    if db.scalar(select(McpServer.id).where(McpServer.organization_id == org_id, McpServer.name == body.name)):
        raise api_error(409, "name_taken", "a server with this name is already registered")
    s = McpServer(organization_id=org_id, name=body.name, url=body.url, secret_ref=body.secret_ref, created_by=principal.user_id)
    db.add(s)
    db.flush()
    audit(db, request, "mcp_server.registered", actor_id=principal.user_id, org_id=org_id, target_type="mcp_server", target_id=s.id,
          meta={"name": s.name, "authenticated": bool(s.secret_ref)})  # the URL may carry tokens: not logged
    db.commit()
    return server_out(s)


@router.get("/organizations/{org_id}/mcp-servers")
def list_servers(org_id: uuid.UUID, principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict[str, Any]:
    authorize(db, principal, org_id, "mcpserver.manage")
    rows = db.scalars(select(McpServer).where(McpServer.organization_id == org_id).order_by(McpServer.name))
    return {"items": [server_out(s) for s in rows]}


@router.get("/organizations/{org_id}/mcp-servers/{name}")
def get_server(org_id: uuid.UUID, name: str, principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict[str, Any]:
    return server_out(_load(db, principal, org_id, name))


@router.patch("/organizations/{org_id}/mcp-servers/{name}")
def update_server(org_id: uuid.UUID, name: str, body: ServerPatch, request: Request, principal: Principal = Depends(current_principal),
                  db: Session = Depends(get_db)) -> dict[str, Any]:
    s = _load(db, principal, org_id, name)
    _check(request, body.url, body.secret_ref)
    changed = []
    if body.url is not None and body.url != s.url:
        s.url, s.tools, s.tool_policy, s.discovered_at = body.url, [], {}, None  # a different server: nothing stays approved
        changed.append("url")
    if body.secret_ref is not None:
        s.secret_ref = body.secret_ref
        changed.append("secret_ref")
    if body.clear_secret:
        s.secret_ref = None
        changed.append("secret_ref")
    if body.status is not None:
        s.status = body.status
        changed.append("status")
    audit(db, request, "mcp_server.updated", actor_id=principal.user_id, org_id=org_id, target_type="mcp_server", target_id=s.id,
          meta={"name": s.name, "changed": changed})
    db.commit()
    return server_out(s)


@router.delete("/organizations/{org_id}/mcp-servers/{name}", status_code=204)
def delete_server(org_id: uuid.UUID, name: str, request: Request, principal: Principal = Depends(current_principal),
                  db: Session = Depends(get_db)) -> Response:
    s = _load(db, principal, org_id, name)
    audit(db, request, "mcp_server.deleted", actor_id=principal.user_id, org_id=org_id, target_type="mcp_server", target_id=s.id,
          meta={"name": s.name})
    db.delete(s)
    db.commit()
    return Response(status_code=204)


@router.post("/organizations/{org_id}/mcp-servers/{name}/discover")
def discover(org_id: uuid.UUID, name: str, request: Request, principal: Principal = Depends(current_principal),
             db: Session = Depends(get_db)) -> dict[str, Any]:
    """Ask the server what tools it offers. New tools start disabled; tools that disappeared lose their approval."""
    s = _load(db, principal, org_id, name)
    secret = None
    if s.secret_ref:
        try:
            secret = resolve_secret(s.secret_ref)
        except SecretUnavailable as e:
            raise api_error(422, "secret_unavailable", str(e)) from e
    client = McpClient(s.url, request.app.state.mcp_transport, secret=secret, timeout=request.app.state.settings.webhook_timeout_seconds)
    try:
        client.initialize()
        tools = client.list_tools()
    except McpError as e:
        raise api_error(502, "mcp_server_error", str(e)) from e
    names = {t["name"] for t in tools}
    policy = {k: v for k, v in (s.tool_policy or {}).items() if k in names}
    s.tools, s.tool_policy, s.discovered_at = tools, policy, datetime.now(UTC)
    audit(db, request, "mcp_server.discovered", actor_id=principal.user_id, org_id=org_id, target_type="mcp_server", target_id=s.id,
          meta={"name": s.name, "tools": len(tools), "dropped_policies": len((s.tool_policy or {}).keys() - policy.keys())})
    db.commit()
    return server_out(s)


@router.put("/organizations/{org_id}/mcp-servers/{name}/tools/{tool}")
def set_tool_policy(org_id: uuid.UUID, name: str, tool: str, body: ToolPolicyIn, request: Request,
                    principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict[str, Any]:
    s = _load(db, principal, org_id, name)
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", tool) or not any(t["name"] == tool for t in s.tools or []):
        raise api_error(404, "unknown_tool", "discover the server's tools first; this server does not offer that tool")
    if body.enabled and body.level != "A0":
        raise api_error(422, "approval_not_supported",
                        "only read-only tools (level A0) can be enabled: the AI Builder has no way to ask a person before a "
                        "tool changes something")
    policy = dict(s.tool_policy or {})
    policy[tool] = {"enabled": body.enabled, "level": body.level}
    s.tool_policy = policy
    audit(db, request, "mcp_server.tool_policy", actor_id=principal.user_id, org_id=org_id, target_type="mcp_server", target_id=s.id,
          meta={"name": s.name, "tool": tool, "enabled": body.enabled, "level": body.level})
    db.commit()
    return server_out(s)
