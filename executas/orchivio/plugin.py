"""Orchivio Executa plugin — wraps the Orchivio REST API as Anna tools.

Configuration (environment variables):
  ORCHIVIO_BASE_URL             Base URL of the Orchivio instance (e.g. http://localhost:8000)
  ORCHIVIO_API_TOKEN            Personal API token (Authorization: Bearer <token>)
  ORCHIVIO_ANNA_SAMPLING_PORT   Port for the Anna sampling relay (default: 0 = OS-assigned)

The plugin starts a local HTTP relay on 127.0.0.1 that the Orchivio backend
calls when it needs an LLM completion. The relay forwards the request to the
Anna host via the sampling.complete reverse-RPC protocol and returns the result.
The actual relay URL is printed to stderr at startup so it can be passed to the
backend as ORCHIVIO_ANNA_SAMPLING_URL.
"""

from __future__ import annotations

import http.server
import json
import os
import queue
import sys
import threading
import uuid
from typing import Any

import httpx

BASE_URL = os.environ.get("ORCHIVIO_BASE_URL", "http://localhost:8000").rstrip("/")
API_TOKEN = os.environ.get("ORCHIVIO_API_TOKEN", "")
SAMPLING_PORT = int(os.environ.get("ORCHIVIO_ANNA_SAMPLING_PORT", "0"))

MANIFEST = {
    "name": "orchivio",
    "display_name": "Orchivio",
    "version": "0.1.0",
    "description": "Manage Orchivio projects and applications: list resources, propose AI-generated model changes, review diffs, and approve or reject proposals.",
    "author": "Orchivio",
    "capabilities": {"sampling": {}},
    "tools": [
        {
            "name": "list_organizations",
            "description": "Return the organizations the authenticated user belongs to, including their role in each.",
            "parameters": [],
        },
        {
            "name": "list_projects",
            "description": "List projects inside an organization.",
            "parameters": [
                {"name": "org_id", "type": "string", "description": "Organization UUID", "required": True},
            ],
        },
        {
            "name": "create_project",
            "description": "Create a new project inside an organization.",
            "parameters": [
                {"name": "org_id", "type": "string", "description": "Organization UUID", "required": True},
                {"name": "name", "type": "string", "description": "Human-readable project name", "required": True},
                {"name": "slug", "type": "string", "description": "URL-safe slug (lowercase letters, digits, hyphens)", "required": True},
            ],
        },
        {
            "name": "list_applications",
            "description": "List applications inside a project.",
            "parameters": [
                {"name": "project_id", "type": "string", "description": "Project UUID", "required": True},
            ],
        },
        {
            "name": "create_application",
            "description": "Create a new application inside a project. The application starts empty; use ai_propose_change to add its first model.",
            "parameters": [
                {"name": "project_id", "type": "string", "description": "Project UUID", "required": True},
                {"name": "key", "type": "string", "description": "Identifier key (lowercase letters, digits, hyphens, underscores)", "required": True},
                {"name": "name", "type": "string", "description": "Human-readable application name", "required": True},
            ],
        },
        {
            "name": "get_application",
            "description": "Get an application's metadata including its current revision number.",
            "parameters": [
                {"name": "app_id", "type": "string", "description": "Application UUID", "required": True},
            ],
        },
        {
            "name": "ai_propose_change",
            "description": (
                "Ask Orchivio's built-in AI builder to propose a model change for an application based on "
                "natural-language requirements. Returns a proposal with a diff and lint results. "
                "Always call get_proposal afterwards to show the user the diff before asking them to approve."
            ),
            "parameters": [
                {"name": "app_id", "type": "string", "description": "Application UUID", "required": True},
                {"name": "requirements", "type": "string", "description": "Plain-English description of the desired change", "required": True},
                {"name": "title", "type": "string", "description": "Short title for the proposal (optional)", "required": False},
            ],
        },
        {
            "name": "list_proposals",
            "description": "List proposals for an application, newest first.",
            "parameters": [
                {"name": "app_id", "type": "string", "description": "Application UUID", "required": True},
            ],
        },
        {
            "name": "get_proposal",
            "description": "Get full proposal details: status, diff, lint warnings, and approval level.",
            "parameters": [
                {"name": "proposal_id", "type": "string", "description": "Proposal UUID", "required": True},
            ],
        },
        {
            "name": "decide_proposal",
            "description": "Approve or reject a pending proposal.",
            "parameters": [
                {"name": "proposal_id", "type": "string", "description": "Proposal UUID", "required": True},
                {"name": "decision", "type": "string", "description": "'approve' or 'reject'", "required": True},
                {"name": "comment", "type": "string", "description": "Optional reviewer comment (max 1000 chars)", "required": False},
            ],
        },
    ],
}

# ---------------------------------------------------------------------------
# Anna sampling relay
# ---------------------------------------------------------------------------
# The Anna host communicates via stdin/stdout JSON-RPC. A background stdin
# reader thread routes incoming messages: sampling responses go to their
# waiting queue; everything else goes to the main inbox.
# The relay HTTP server (on a loopback port) receives sampling requests from
# the Orchivio backend, forwards them to Anna via the sampling.complete
# reverse-RPC method, and returns the result.
# ---------------------------------------------------------------------------

_sampling: dict[str, queue.Queue] = {}  # sampling request id -> result queue
_stdout_lock = threading.Lock()
_inbox: queue.Queue = queue.Queue()  # regular JSON-RPC requests


def _write(msg: dict) -> None:
    with _stdout_lock:
        sys.stdout.write(json.dumps(msg) + "\n")
        sys.stdout.flush()


def _sample(
    system: str,
    messages: list[dict[str, str]],
    max_tokens: int,
    temperature: float = 0.0,
    timeout: float = 120.0,
) -> dict:
    """Send a sampling.complete request to Anna and block until the response arrives."""
    rid = str(uuid.uuid4())
    result_q: queue.Queue = queue.Queue()
    _sampling[rid] = result_q
    try:
        _write({
            "jsonrpc": "2.0",
            "id": rid,
            "method": "sampling.complete",
            "params": {
                "system": system,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
            },
        })
        try:
            msg = result_q.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError("Anna sampling did not respond within the timeout")
    finally:
        _sampling.pop(rid, None)
    if "error" in msg:
        err = msg["error"]
        raise RuntimeError(f"Anna sampling error: {err.get('message', err) if isinstance(err, dict) else err}")
    return msg.get("result") or {}


def _stdin_reader() -> None:
    """Background thread: continuously reads stdin and routes messages."""
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            continue
        rid = msg.get("id", "")
        if rid and rid in _sampling:
            _sampling[rid].put(msg)
        else:
            _inbox.put(msg)


class _SamplingHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args, **kwargs) -> None:
        pass  # suppress default request logging to avoid polluting stderr

    def do_POST(self) -> None:
        if self.path != "/complete":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            body: dict[str, Any] = json.loads(self.rfile.read(length))
            result = _sample(
                system=body.get("system", ""),
                messages=body.get("messages", []),
                max_tokens=int(body.get("max_tokens", 4096)),
                temperature=float(body.get("temperature", 0.0)),
            )
            # Anna's sampling response has a `content` field (string or list).
            content = result.get("content", "")
            if isinstance(content, list):
                content = "".join(
                    block.get("text", "") for block in content if isinstance(block, dict)
                )
            usage = result.get("usage") or {}
            response_body = json.dumps({
                "text": content,
                "model": result.get("model", "anna"),
                "input_tokens": int(usage.get("input_tokens", 0)),
                "output_tokens": int(usage.get("output_tokens", 0)),
                "stop_reason": result.get("stop_reason", "end_turn"),
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response_body)))
            self.end_headers()
            self.wfile.write(response_body)
        except Exception as e:
            error_body = json.dumps({"error": str(e)}).encode()
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(error_body)))
            self.end_headers()
            self.wfile.write(error_body)


def _start_relay() -> int:
    """Start the sampling relay HTTP server in a daemon thread. Returns the bound port."""
    server = http.server.HTTPServer(("127.0.0.1", SAMPLING_PORT), _SamplingHandler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return port


# ---------------------------------------------------------------------------
# Orchivio REST API helpers
# ---------------------------------------------------------------------------

def _headers() -> dict[str, str]:
    h = {"Content-Type": "application/json", "Accept": "application/json"}
    if API_TOKEN:
        h["Authorization"] = f"Bearer {API_TOKEN}"
    return h


def _get(path: str, params: dict[str, Any] | None = None) -> Any:
    with httpx.Client(base_url=BASE_URL, headers=_headers(), timeout=30) as client:
        r = client.get(path, params=params)
        r.raise_for_status()
        return r.json()


def _post(path: str, body: dict[str, Any]) -> Any:
    with httpx.Client(base_url=BASE_URL, headers=_headers(), timeout=60) as client:
        r = client.post(path, json=body)
        r.raise_for_status()
        return r.json()


# ---------------------------------------------------------------------------
# Tool dispatch
# ---------------------------------------------------------------------------

def _invoke(tool: str, args: dict[str, Any]) -> Any:
    if tool == "list_organizations":
        me = _get("/auth/me")
        return {"organizations": me.get("organizations", [])}

    if tool == "list_projects":
        return _get(f"/organizations/{args['org_id']}/projects")

    if tool == "create_project":
        return _post(f"/organizations/{args['org_id']}/projects", {
            "name": args["name"],
            "slug": args["slug"],
        })

    if tool == "list_applications":
        return _get(f"/projects/{args['project_id']}/applications")

    if tool == "create_application":
        return _post(f"/projects/{args['project_id']}/applications", {
            "key": args["key"],
            "name": args["name"],
        })

    if tool == "get_application":
        return _get(f"/applications/{args['app_id']}")

    if tool == "ai_propose_change":
        body: dict[str, Any] = {"requirements": args["requirements"]}
        if args.get("title"):
            body["title"] = args["title"]
        return _post(f"/applications/{args['app_id']}/ai/propose", body)

    if tool == "list_proposals":
        return _get(f"/applications/{args['app_id']}/proposals")

    if tool == "get_proposal":
        return _get(f"/proposals/{args['proposal_id']}")

    if tool == "decide_proposal":
        body = {"decision": args["decision"]}
        if args.get("comment"):
            body["comment"] = args["comment"]
        return _post(f"/proposals/{args['proposal_id']}/decision", body)

    raise ValueError(f"unknown tool: {tool}")


def _handle(req: dict[str, Any]) -> dict[str, Any]:
    rid = req.get("id")
    method = req.get("method")
    params = req.get("params") or {}

    if method == "health":
        return {"jsonrpc": "2.0", "id": rid, "result": {"status": "ready"}}

    if method == "describe":
        return {"jsonrpc": "2.0", "id": rid, "result": MANIFEST}

    if method == "invoke":
        tool = params.get("tool") or params.get("name", "")
        arguments = params.get("arguments") or {}
        try:
            data = _invoke(tool, arguments)
            return {"jsonrpc": "2.0", "id": rid, "result": {"success": True, "data": data}}
        except httpx.HTTPStatusError as e:
            try:
                detail = e.response.json()
            except Exception:
                detail = e.response.text
            return {"jsonrpc": "2.0", "id": rid,
                    "result": {"success": False, "error": f"HTTP {e.response.status_code}", "detail": detail}}
        except Exception as e:
            return {"jsonrpc": "2.0", "id": rid,
                    "result": {"success": False, "error": str(e)}}

    return {"jsonrpc": "2.0", "id": rid,
            "error": {"code": -32601, "message": f"method not found: {method}"}}


def main() -> None:
    # Start the Anna sampling relay and announce its URL so the backend can
    # be configured with ORCHIVIO_ANNA_SAMPLING_URL.
    relay_port = _start_relay()
    relay_url = f"http://127.0.0.1:{relay_port}"
    print(f"ORCHIVIO_ANNA_SAMPLING_URL={relay_url}", file=sys.stderr, flush=True)

    # Read stdin in a background thread so sampling responses can be routed
    # back to waiting _sample() calls even while _handle() is in progress.
    reader = threading.Thread(target=_stdin_reader, daemon=True, name="orchivio-stdin")
    reader.start()

    while True:
        req = _inbox.get()
        resp = _handle(req)
        _write(resp)


if __name__ == "__main__":
    main()
