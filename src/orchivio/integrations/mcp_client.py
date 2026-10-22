"""MCP client for the AI Builder (FR-087, DESIGN.md §7.5).

What it will and will not do:

* Talks to **registered servers only**. Registration is an organization-admin act; the URL passes the same SSRF
  checks as every other outbound destination and every call is made through the pinned transport.
* Only tools an admin has **explicitly enabled** at approval level A0 (read-only background information) can be
  called, and only those the server still advertises. Anything that could change something outside needs a human in
  the loop, which the Builder does not have, so it cannot be enabled at all.
* A model only *requests* calls; each is re-checked against that allowlist and the tool's input schema here.
  Tool output is untrusted text: only ``text`` content is kept, control characters are stripped, the size is capped,
  and it is shown to the model inside delimited data blocks. It can never cause another call.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from orchivio import telemetry
from orchivio.db.models import McpServer

from .dispatcher import SecretUnavailable, resolve_secret
from .rest import RequestTransport
from .ssrf import DestinationError

PROTOCOLS = ("2025-06-18", "2025-03-26")
MAX_RESPONSE_BYTES = 256 * 1024
MAX_TOOL_TEXT = 8000
MAX_RESOURCE_TEXT = 32_000
MAX_PROMPT_TEXT = 16_000
MAX_ARGUMENT_BYTES = 2000
MAX_TOOLS = 100
MAX_RESOURCES = 200
MAX_PROMPTS = 100
TOOL_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
PROMPT_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class McpError(Exception):
    """A server problem, phrased safely for an admin (never includes server-supplied text beyond a short code)."""


@dataclass(frozen=True)
class ToolOutput:
    text: str
    is_error: bool
    truncated: bool


@dataclass(frozen=True)
class ResourceOutput:
    text: str
    mime_type: str
    truncated: bool


@dataclass(frozen=True)
class PromptOutput:
    text: str
    truncated: bool


def clean_text(value: Any, limit: int) -> str:
    text = _CONTROL.sub("", str(value or ""))
    return text[:limit]


def trim_schema(schema: Any) -> dict[str, Any]:
    """The part of a tool's input schema the platform keeps: top-level property names, simple types and `required`."""
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}}
    props: dict[str, Any] = {}
    for name, spec in list((schema.get("properties") or {}).items())[:30] if isinstance(schema.get("properties"), dict) else []:
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.\-]{1,64}", name) or not isinstance(spec, dict):
            continue
        kept: dict[str, Any] = {}
        t = spec.get("type")
        if t in ("string", "integer", "number", "boolean", "array", "object"):
            kept["type"] = t
        if isinstance(spec.get("enum"), list) and len(spec["enum"]) <= 20 and all(isinstance(x, (str, int, float, bool)) for x in spec["enum"]):
            kept["enum"] = spec["enum"]
        if spec.get("description"):
            kept["description"] = clean_text(spec["description"], 200)
        props[name] = kept
    req = [r for r in (schema.get("required") or []) if isinstance(r, str) and r in props]
    out: dict[str, Any] = {"type": "object", "properties": props, "required": req}
    if schema.get("additionalProperties") is False:
        out["additionalProperties"] = False
    return out


def check_arguments(schema: dict[str, Any], args: Any) -> str | None:
    """None when ``args`` satisfies the (trimmed) schema, else a short reason."""
    if not isinstance(args, dict):
        return "arguments must be an object"
    if len(json.dumps(args, separators=(",", ":"), default=str)) > MAX_ARGUMENT_BYTES:
        return "arguments are too large"
    props = schema.get("properties") or {}
    for r in schema.get("required") or []:
        if r not in args:
            return f"missing required argument '{r}'"
    for k, v in args.items():
        if k not in props:
            if schema.get("additionalProperties") is False or props:
                return f"unknown argument '{k}'"
            continue
        spec = props[k]
        t = spec.get("type")
        ok = {"string": isinstance(v, str), "integer": isinstance(v, int) and not isinstance(v, bool),
              "number": isinstance(v, (int, float)) and not isinstance(v, bool), "boolean": isinstance(v, bool),
              "array": isinstance(v, list), "object": isinstance(v, dict)}.get(t, True)
        if not ok:
            return f"argument '{k}' must be of type {t}"
        if "enum" in spec and v not in spec["enum"]:
            return f"argument '{k}' must be one of the listed values"
    return None


class McpClient:
    """One session with one server (Streamable HTTP; the server may answer with JSON or a single SSE message)."""

    def __init__(self, url: str, transport: RequestTransport, *, secret: str | None = None, timeout: float = 15.0) -> None:
        self.url, self.transport, self.secret, self.timeout = url, transport, secret, timeout
        self.session_id: str | None = None
        self.version = PROTOCOLS[0]
        self._next_id = 1

    def _rpc(self, method: str, params: dict[str, Any] | None = None, *, notify: bool = False) -> Any:
        msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        rid = None
        if not notify:
            rid = self._next_id
            self._next_id += 1
            msg["id"] = rid
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                   "MCP-Protocol-Version": self.version, "User-Agent": "Orchivio-MCP-Client/1"}
        if self.secret:
            headers["Authorization"] = f"Bearer {self.secret}"
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        try:
            resp = self.transport.request("POST", self.url, headers, json.dumps(msg).encode(), timeout=self.timeout,
                                          max_bytes=MAX_RESPONSE_BYTES)
        except DestinationError as e:
            raise McpError(f"destination refused: {e}") from e
        except OSError as e:
            raise McpError(f"network error ({type(e).__name__})") from e
        sid = getattr(resp, "headers", {}).get("mcp-session-id")
        if sid and method == "initialize":
            self.session_id = sid[:200]
        if notify:
            if resp.status not in (200, 202, 204):
                raise McpError(f"server answered HTTP {resp.status}")
            return None
        if resp.status != 200:
            raise McpError(f"server answered HTTP {resp.status}")
        if len(resp.body) >= MAX_RESPONSE_BYTES:
            raise McpError("the response is larger than the limit")
        payload = _find_response(resp, rid)
        if payload is None:
            raise McpError("the server's answer is not a valid JSON-RPC response")
        if "error" in payload:
            code = payload["error"].get("code") if isinstance(payload["error"], dict) else None
            raise McpError(f"the server returned a JSON-RPC error ({code if isinstance(code, int) else 'unknown'})")
        return payload.get("result")

    def initialize(self) -> None:
        result = self._rpc("initialize", {"protocolVersion": PROTOCOLS[0], "capabilities": {},
                                          "clientInfo": {"name": "orchivio-ai-builder", "version": "1"}})
        version = result.get("protocolVersion") if isinstance(result, dict) else None
        if version not in PROTOCOLS:
            raise McpError("the server speaks an unsupported protocol version")
        self.version = version
        self._rpc("notifications/initialized", notify=True)

    def list_tools(self) -> list[dict[str, Any]]:
        """Normalised tool descriptions (names validated, text cleaned and bounded, schema trimmed)."""
        out: list[dict[str, Any]] = []
        cursor = None
        for _ in range(5):
            result = self._rpc("tools/list", {"cursor": cursor} if cursor else {})
            for t in (result.get("tools") if isinstance(result, dict) else None) or []:
                if isinstance(t, dict) and isinstance(t.get("name"), str) and TOOL_NAME.match(t["name"]):
                    out.append({"name": t["name"], "description": clean_text(t.get("description"), 500),
                                "input_schema": trim_schema(t.get("inputSchema"))})
            cursor = result.get("nextCursor") if isinstance(result, dict) else None
            if not cursor or len(out) >= MAX_TOOLS:
                break
        return out[:MAX_TOOLS]

    def call_tool(self, name: str, arguments: dict[str, Any]) -> ToolOutput:
        result = self._rpc("tools/call", {"name": name, "arguments": arguments})
        if not isinstance(result, dict):
            raise McpError("the tool result is malformed")
        parts = [c.get("text", "") for c in result.get("content") or [] if isinstance(c, dict) and c.get("type") == "text"]
        text = clean_text("\n".join(p for p in parts if isinstance(p, str)), MAX_TOOL_TEXT + 1)
        truncated = len(text) > MAX_TOOL_TEXT
        return ToolOutput(text[:MAX_TOOL_TEXT], bool(result.get("isError")), truncated)

    def list_resources(self) -> list[dict[str, Any]]:
        """Normalised resource descriptions (URI, name, description, mimeType)."""
        out: list[dict[str, Any]] = []
        cursor = None
        for _ in range(10):
            result = self._rpc("resources/list", {"cursor": cursor} if cursor else {})
            for r in (result.get("resources") if isinstance(result, dict) else None) or []:
                if not isinstance(r, dict):
                    continue
                uri = r.get("uri")
                if not isinstance(uri, str) or not uri or len(uri) > 2000:
                    continue
                out.append({"uri": uri,
                            "name": clean_text(r.get("name") or uri, 200),
                            "description": clean_text(r.get("description"), 500),
                            "mimeType": clean_text(r.get("mimeType") or "text/plain", 100)})
            cursor = result.get("nextCursor") if isinstance(result, dict) else None
            if not cursor or len(out) >= MAX_RESOURCES:
                break
        return out[:MAX_RESOURCES]

    def read_resource(self, uri: str) -> ResourceOutput:
        result = self._rpc("resources/read", {"uri": uri})
        if not isinstance(result, dict):
            raise McpError("the resource result is malformed")
        parts: list[str] = []
        mime = "text/plain"
        for c in result.get("contents") or []:
            if not isinstance(c, dict):
                continue
            if c.get("mimeType"):
                mime = clean_text(c["mimeType"], 100)
            if c.get("type") == "text" or "text" in c:
                parts.append(str(c.get("text") or ""))
            elif c.get("type") == "blob":
                parts.append(f"[binary content, mimeType={mime}]")
        text = clean_text("\n".join(parts), MAX_RESOURCE_TEXT + 1)
        truncated = len(text) > MAX_RESOURCE_TEXT
        return ResourceOutput(text[:MAX_RESOURCE_TEXT], mime, truncated)

    def list_prompts(self) -> list[dict[str, Any]]:
        """Normalised prompt descriptions (name, description, arguments)."""
        out: list[dict[str, Any]] = []
        cursor = None
        for _ in range(5):
            result = self._rpc("prompts/list", {"cursor": cursor} if cursor else {})
            for p in (result.get("prompts") if isinstance(result, dict) else None) or []:
                if isinstance(p, dict) and isinstance(p.get("name"), str) and PROMPT_NAME.match(p["name"]):
                    args: list[dict[str, Any]] = []
                    for a in p.get("arguments") or []:
                        if isinstance(a, dict) and isinstance(a.get("name"), str):
                            args.append({"name": a["name"], "description": clean_text(a.get("description"), 200),
                                         "required": bool(a.get("required"))})
                    out.append({"name": p["name"], "description": clean_text(p.get("description"), 500),
                                "arguments": args})
            cursor = result.get("nextCursor") if isinstance(result, dict) else None
            if not cursor or len(out) >= MAX_PROMPTS:
                break
        return out[:MAX_PROMPTS]

    def get_prompt(self, name: str, arguments: dict[str, str] | None = None) -> PromptOutput:
        params: dict[str, Any] = {"name": name}
        if arguments:
            params["arguments"] = {k: str(v)[:500] for k, v in list(arguments.items())[:20]}
        result = self._rpc("prompts/get", params)
        if not isinstance(result, dict):
            raise McpError("the prompt result is malformed")
        parts: list[str] = []
        for msg in result.get("messages") or []:
            if not isinstance(msg, dict):
                continue
            content = msg.get("content")
            if isinstance(content, str):
                parts.append(content)
            elif isinstance(content, dict) and content.get("type") == "text":
                parts.append(str(content.get("text") or ""))
        text = clean_text("\n".join(parts), MAX_PROMPT_TEXT + 1)
        truncated = len(text) > MAX_PROMPT_TEXT
        return PromptOutput(text[:MAX_PROMPT_TEXT], truncated)


def _find_response(resp: Any, rid: int | None) -> dict[str, Any] | None:
    ctype = (getattr(resp, "content_type", "") or "").lower()
    raw = resp.body.decode("utf-8", errors="replace")
    candidates: list[Any] = []
    if "text/event-stream" in ctype:
        for event in re.split(r"\r?\n\r?\n", raw):
            data = "\n".join(line[5:].lstrip() for line in event.splitlines() if line.startswith("data:"))
            if data:
                try:
                    candidates.append(json.loads(data))
                except ValueError:
                    continue
    else:
        try:
            candidates.append(json.loads(raw))
        except ValueError:
            return None
    for c in candidates:
        if isinstance(c, dict) and c.get("jsonrpc") == "2.0" and c.get("id") == rid and ("result" in c or "error" in c):
            return c
    return None


# --- the broker: allowlist, policy, auditing ---------------------------------------------------------------


@dataclass(frozen=True)
class CatalogTool:
    server: str
    tool: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class CatalogResource:
    server: str
    uri: str
    name: str
    description: str
    mime_type: str


@dataclass(frozen=True)
class CatalogPrompt:
    server: str
    name: str
    description: str
    arguments: list[dict[str, Any]]


@dataclass
class CallRecord:
    server: str
    tool: str
    args_sha256: str
    ok: bool
    error: str | None = None
    text: str = ""
    truncated: bool = False


@dataclass
class ResourceRecord:
    server: str
    uri: str
    ok: bool
    mime_type: str = "text/plain"
    error: str | None = None
    text: str = ""
    truncated: bool = False


@dataclass
class PromptRecord:
    server: str
    name: str
    ok: bool
    error: str | None = None
    text: str = ""
    truncated: bool = False


class ToolBroker:
    """Everything the Builder may do with MCP servers, for one organization at a time."""

    def __init__(self, sessions: sessionmaker[Session], transport: RequestTransport, *,
                 secrets: Callable[[str], str] = resolve_secret, timeout: float = 15.0) -> None:
        self.sessions, self.transport, self.secrets, self.timeout = sessions, transport, secrets, timeout

    def catalog(self, organization_id: uuid.UUID) -> list[CatalogTool]:
        """Tools that are registered, enabled by an admin at A0 and still advertised by their server."""
        out: list[CatalogTool] = []
        with self.sessions() as s:
            for srv in s.scalars(select(McpServer).where(McpServer.organization_id == organization_id, McpServer.status == "active")
                                 .order_by(McpServer.name)):
                for t in srv.tools or []:
                    pol = (srv.tool_policy or {}).get(t["name"])
                    if pol and pol.get("enabled") is True and pol.get("level") == "A0":
                        out.append(CatalogTool(srv.name, t["name"], t.get("description", ""), t.get("input_schema", {})))
        return out

    def run(self, organization_id: uuid.UUID, requested: list[tuple[str, str, dict[str, Any]]], max_calls: int = 3) -> list[CallRecord]:
        """Execute model-requested calls that survive re-checking. Disallowed ones are recorded as refused, not run."""
        allowed = {(c.server, c.tool): c for c in self.catalog(organization_id)}
        records: list[CallRecord] = []
        clients: dict[str, McpClient | McpError] = {}
        seen: set[tuple[str, str, str]] = set()
        executed = 0
        for server, tool, args in requested:
            digest = hashlib.sha256(json.dumps(args, sort_keys=True, default=str).encode()).hexdigest()
            rec = CallRecord(server, tool, digest, ok=False)
            records.append(rec)
            entry = allowed.get((server, tool))
            if entry is None:
                rec.error = "refused: not an enabled tool"
                continue
            if (server, tool, digest) in seen:
                rec.error = "refused: duplicate call"
                continue
            if executed >= max_calls:
                rec.error = "refused: call limit reached"
                continue
            problem = check_arguments(entry.input_schema, args)
            if problem:
                rec.error = f"refused: {problem}"
                continue
            seen.add((server, tool, digest))
            executed += 1
            if server not in clients:
                clients[server] = self._connect(organization_id, server)
            client = clients[server]
            if isinstance(client, McpError):
                rec.error = str(client)
                continue
            with telemetry.span("mcp.tool_call", kind="client", **{"mcp.server": server, "mcp.tool": tool}) as sp:
                try:
                    out = client.call_tool(tool, args)
                except McpError as e:
                    rec.error = str(e)
                    sp.set_error("mcp error")
                    telemetry.count("orchivio.mcp.tool_calls", outcome="error")
                    continue
                if out.is_error:
                    rec.error = "the tool reported an error"
                    sp.set_error("tool error")
                    telemetry.count("orchivio.mcp.tool_calls", outcome="tool_error")
                    continue
                rec.ok, rec.text, rec.truncated = True, out.text, out.truncated
                sp.set_attribute("mcp.output_chars", len(out.text))
            telemetry.count("orchivio.mcp.tool_calls", outcome="ok")
        return records

    def resource_catalog(self, organization_id: uuid.UUID) -> list[CatalogResource]:
        """Resources that are registered, enabled by an admin, and still advertised by their server."""
        out: list[CatalogResource] = []
        with self.sessions() as s:
            for srv in s.scalars(select(McpServer).where(McpServer.organization_id == organization_id, McpServer.status == "active")
                                 .order_by(McpServer.name)):
                for r in srv.resources or []:
                    pol = (srv.resource_policy or {}).get(r["uri"])
                    if pol and pol.get("enabled") is True:
                        out.append(CatalogResource(srv.name, r["uri"], r.get("name", r["uri"]),
                                                   r.get("description", ""), r.get("mimeType", "text/plain")))
        return out

    def read_resources(self, organization_id: uuid.UUID,
                       requested: list[tuple[str, str]], max_reads: int = 5) -> list[ResourceRecord]:
        """Fetch model-requested resources that survive the allowlist check."""
        allowed = {(c.server, c.uri): c for c in self.resource_catalog(organization_id)}
        records: list[ResourceRecord] = []
        clients: dict[str, McpClient | McpError] = {}
        seen: set[tuple[str, str]] = set()
        executed = 0
        for server, uri in requested:
            rec = ResourceRecord(server, uri, ok=False)
            records.append(rec)
            if (server, uri) not in allowed:
                rec.error = "refused: not an enabled resource"
                continue
            if (server, uri) in seen:
                rec.error = "refused: duplicate read"
                continue
            if executed >= max_reads:
                rec.error = "refused: read limit reached"
                continue
            seen.add((server, uri))
            executed += 1
            if server not in clients:
                clients[server] = self._connect(organization_id, server)
            client = clients[server]
            if isinstance(client, McpError):
                rec.error = str(client)
                continue
            with telemetry.span("mcp.resource_read", kind="client", **{"mcp.server": server, "mcp.resource": uri[:200]}) as sp:
                try:
                    out = client.read_resource(uri)
                except McpError as e:
                    rec.error = str(e)
                    sp.set_error("mcp error")
                    telemetry.count("orchivio.mcp.resource_reads", outcome="error")
                    continue
                rec.ok, rec.text, rec.mime_type, rec.truncated = True, out.text, out.mime_type, out.truncated
                sp.set_attribute("mcp.output_chars", len(out.text))
            telemetry.count("orchivio.mcp.resource_reads", outcome="ok")
        return records

    def prompt_catalog(self, organization_id: uuid.UUID) -> list[CatalogPrompt]:
        """Prompts that are registered and enabled by an admin."""
        out: list[CatalogPrompt] = []
        with self.sessions() as s:
            for srv in s.scalars(select(McpServer).where(McpServer.organization_id == organization_id, McpServer.status == "active")
                                 .order_by(McpServer.name)):
                for p in srv.prompts or []:
                    pol = (srv.prompt_policy or {}).get(p["name"])
                    if pol and pol.get("enabled") is True:
                        out.append(CatalogPrompt(srv.name, p["name"], p.get("description", ""),
                                                 p.get("arguments", [])))
        return out

    def get_prompts(self, organization_id: uuid.UUID,
                    requested: list[tuple[str, str, dict[str, str] | None]], max_gets: int = 3) -> list[PromptRecord]:
        """Fetch model-requested prompts that survive the allowlist check."""
        allowed = {(c.server, c.name): c for c in self.prompt_catalog(organization_id)}
        records: list[PromptRecord] = []
        clients: dict[str, McpClient | McpError] = {}
        seen: set[tuple[str, str]] = set()
        executed = 0
        for server, name, args in requested:
            rec = PromptRecord(server, name, ok=False)
            records.append(rec)
            if (server, name) not in allowed:
                rec.error = "refused: not an enabled prompt"
                continue
            if (server, name) in seen:
                rec.error = "refused: duplicate get"
                continue
            if executed >= max_gets:
                rec.error = "refused: get limit reached"
                continue
            seen.add((server, name))
            executed += 1
            if server not in clients:
                clients[server] = self._connect(organization_id, server)
            client = clients[server]
            if isinstance(client, McpError):
                rec.error = str(client)
                continue
            with telemetry.span("mcp.prompt_get", kind="client", **{"mcp.server": server, "mcp.prompt": name}) as sp:
                try:
                    out = client.get_prompt(name, args)
                except McpError as e:
                    rec.error = str(e)
                    sp.set_error("mcp error")
                    telemetry.count("orchivio.mcp.prompt_gets", outcome="error")
                    continue
                rec.ok, rec.text, rec.truncated = True, out.text, out.truncated
                sp.set_attribute("mcp.output_chars", len(out.text))
            telemetry.count("orchivio.mcp.prompt_gets", outcome="ok")
        return records

    def _connect(self, organization_id: uuid.UUID, name: str) -> McpClient | McpError:
        with self.sessions() as s:
            srv = s.scalar(select(McpServer).where(McpServer.organization_id == organization_id, McpServer.name == name,
                                                   McpServer.status == "active"))
            if srv is None:
                return McpError("the server is not available")
            url, ref = srv.url, srv.secret_ref
        secret = None
        if ref:
            try:
                secret = self.secrets(ref)
            except SecretUnavailable:
                return McpError("the server's credential is not available")
        client = McpClient(url, self.transport, secret=secret, timeout=self.timeout)
        try:
            client.initialize()
        except McpError as e:
            return e
        return client
