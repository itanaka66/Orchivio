"""Orchivio Executa plugin — wraps the Orchivio REST API as Anna tools.

Configuration (environment variables):
  ORCHIVIO_BASE_URL   Base URL of your Orchivio instance (e.g. http://localhost:8000)
  ORCHIVIO_API_TOKEN  Personal API token created in Orchivio (Authorization: Bearer <token>)
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

import httpx

BASE_URL = os.environ.get("ORCHIVIO_BASE_URL", "http://localhost:8000").rstrip("/")
API_TOKEN = os.environ.get("ORCHIVIO_API_TOKEN", "")

MANIFEST = {
    "name": "orchivio",
    "display_name": "Orchivio",
    "version": "0.1.0",
    "description": "Manage Orchivio projects and applications: list resources, propose AI-generated model changes, review diffs, and approve or reject proposals.",
    "author": "Orchivio",
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
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            req = json.loads(raw)
        except json.JSONDecodeError as e:
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": None,
                                         "error": {"code": -32700, "message": f"parse error: {e}"}}) + "\n")
            sys.stdout.flush()
            continue
        resp = _handle(req)
        sys.stdout.write(json.dumps(resp) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
