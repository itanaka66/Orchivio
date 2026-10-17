"""Studio MCP server: protocol, tools, governance (scopes, proposal-only writes, audit, rate limit)."""

import copy
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_revisions_api import create_app
from test_tenancy import make_org

from orchivio.api.routes.mcp import TOOLS
from orchivio.db.models import AuditEvent, Proposal


@pytest.fixture
def world(new_api, raw_model, app):
    owner = new_api("owner@example.com")
    org = make_org(owner)
    proj = owner.post(f"/organizations/{org['id']}/projects", json={"name": "Inv", "slug": "inventory"}).json()
    application = create_app(owner, proj, raw_model).json()

    def token(scopes):
        r = owner.post("/tokens", json={"name": "agent", "organization_id": org["id"], "scopes": scopes})
        assert r.status_code == 201, r.text
        return r.json()["token"]

    return owner, org, proj, application, token


def rpc(token_value, app, method, params=None, id_=1, notify=False):
    client = TestClient(app, headers={"Authorization": f"Bearer {token_value}"}, raise_server_exceptions=False)
    msg = {"jsonrpc": "2.0", "method": method, "params": params or {}}
    if not notify:
        msg["id"] = id_
    return client.post("/api/v1/mcp", json=msg)


def call(token_value, app, tool, **args):
    r = rpc(token_value, app, "tools/call", {"name": tool, "arguments": args})
    assert r.status_code == 200, r.text
    res = r.json()["result"]
    return res["isError"], json.loads(res["content"][0]["text"])


def test_initialize_ping_and_tool_list(world, app):
    *_, token = world
    t = token(["*"])
    init = rpc(t, app, "initialize", {"protocolVersion": "2025-03-26", "capabilities": {}}).json()["result"]
    assert init["protocolVersion"] == "2025-03-26" and init["serverInfo"]["name"] == "orchivio-studio"
    assert rpc(t, app, "initialize", {"protocolVersion": "1999-01-01"}).json()["result"]["protocolVersion"] == "2025-06-18"
    assert rpc(t, app, "ping").json()["result"] == {}
    tools = rpc(t, app, "tools/list").json()["result"]["tools"]
    assert {x["name"] for x in tools} == set(TOOLS)
    # no tool applies changes
    assert not any("apply" in x["name"] or "revision" in x["name"] for x in tools)
    assert all(x["inputSchema"]["additionalProperties"] is False for x in tools)
    assert rpc(t, app, "notifications/initialized", notify=True).status_code == 202


def test_protocol_errors(world, app):
    *_, token = world
    t = token(["*"])
    assert rpc(t, app, "nope").json()["error"]["code"] == -32601
    client = TestClient(app, headers={"Authorization": f"Bearer {t}"})
    assert client.post("/api/v1/mcp", content=b"{oops").json()["error"]["code"] == -32700
    assert client.post("/api/v1/mcp", json=[{"jsonrpc": "2.0", "id": 1, "method": "ping"}]).json()["error"]["code"] == -32600
    assert client.get("/api/v1/mcp").status_code == 405
    assert client.post("/api/v1/mcp", content=b"x" * 1_100_000).status_code == 413


def test_requires_a_bearer_token_not_a_session(world, app):
    owner, *_ = world
    assert TestClient(app).post("/api/v1/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}).status_code == 401
    assert owner.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}).status_code == 401  # cookie session


def test_read_search_usages_lint_validate(world, app):
    owner, org, proj, application, token = world
    t = token(["*"])
    err, listed = call(t, app, "application.list", project_id=proj["id"])
    assert not err and listed["total"] == 1
    err, read = call(t, app, "model.read", application_id=application["id"])
    assert not err and read["revision_no"] == 1 and read["model"]["modules"]
    err, found = call(t, app, "model.search", application_id=application["id"], query="product")
    assert not err and any(h["name"] == "Product" and h["kind"] == "entity" for h in found["items"])
    eid = next(h["element_id"] for h in found["items"] if h["kind"] == "entity" and h["name"] == "Product")
    err, one = call(t, app, "model.read", application_id=application["id"], element_id=eid)
    assert not err and one["kind"] == "entity"
    err, used = call(t, app, "model.usages", application_id=application["id"], element_id=eid)
    assert not err and used["used_by"]
    err, lint = call(t, app, "model.lint", application_id=application["id"])
    assert not err and "items" in lint
    err, val = call(t, app, "model.validate", model=read["model"])
    assert not err and not [i for i in val["issues"] if i["severity"] == "error"]
    # both selectors, or neither, is an error the agent can read
    err, bad = call(t, app, "model.read")
    assert err and bad["error"]["code"] == "invalid_arguments"


def test_invalid_arguments_and_unknown_tools_are_tool_errors(world, app):
    *_, application, token = world
    t = token(["*"])
    err, out = call(t, app, "model.search", application_id=application["id"], query="x", surprise=1)
    assert err and out["error"]["code"] == "invalid_arguments"
    err, out = call(t, app, "model.search", application_id="not-a-uuid", query="x")
    assert err and out["error"]["details"]
    err, out = call(t, app, "proposal.apply", proposal_id=application["id"])
    assert err and out["error"]["code"] == "unknown_tool"


def test_proposal_create_is_pending_attributed_and_never_applied(world, app):
    owner, org, proj, application, token = world
    t = token(["*"])
    _, read = call(t, app, "model.read", application_id=application["id"])
    m = copy.deepcopy(read["model"])
    ent = next(e for mod in m["modules"] for e in mod["entities"] if e["name"] == "Product")
    ent["display_name"] = "Catalogue item"
    err, p = call(t, app, "proposal.create", application_id=application["id"], model=m, title="Rename Product")
    assert not err and p["status"] == "pending" and p["source"] == "mcp" and "cannot apply" in p["note"]
    assert owner.get(f"/applications/{application['id']}").json()["current_revision_no"] == 1  # unchanged
    err, got = call(t, app, "proposal.get", proposal_id=p["id"])
    assert not err and got["status"] == "pending"
    err, lst = call(t, app, "proposal.list", application_id=application["id"], status="pending")
    assert not err and lst["total"] == 1
    # a model identical to the current revision is rejected
    err, nochange = call(t, app, "proposal.create", application_id=application["id"], model=read["model"], title="same")
    assert err and nochange["error"]["code"] == "no_changes"
    # invalid models are refused with their issues
    bad = copy.deepcopy(m)
    bad["security"]["access"] = []
    err, inv = call(t, app, "proposal.create", application_id=application["id"], model=bad, title="bad")
    assert err and inv["error"]["code"] == "model_invalid" and inv["error"]["details"]
    # a human approves it in the Studio; only then does the model change
    r = owner.post(f"/proposals/{p['id']}/approval", json={"decision": "approve"})
    assert r.status_code in (200, 201), r.text
    assert owner.get(f"/applications/{application['id']}").json()["current_revision_no"] == 2


def test_token_scopes_limit_tools(world, app):
    owner, org, proj, application, token = world
    reader = token(["model.read", "project.read"])
    _, read = call(reader, app, "model.read", application_id=application["id"])
    assert read["revision_no"] == 1
    err, out = call(reader, app, "proposal.create", application_id=application["id"], model=read["model"], title="x")
    assert err and out["error"]["status"] == 403
    err, out = call(reader, app, "tests.run", application_id=application["id"])
    assert err and out["error"]["status"] == 403


def test_other_organizations_look_nonexistent(world, app, new_api):
    owner, org, proj, application, token = world
    other = new_api("other@example.com")
    org2 = make_org(other, slug="other-org")
    t2 = other.post("/tokens", json={"name": "a", "organization_id": org2["id"], "scopes": ["*"]}).json()["token"]
    err, out = call(t2, app, "model.read", application_id=application["id"])
    assert err and out["error"]["status"] == 404


def test_tests_run_tool(world, app):
    *_, application, token = world
    t = token(["*"])
    err, run = call(t, app, "tests.run", application_id=application["id"], seed=1, rows=5)
    assert not err and run["status"] in ("succeeded", "failed")


def test_every_call_is_audited_without_arguments(world, app):
    owner, org, proj, application, token = world
    t = token(["*"])
    call(t, app, "model.search", application_id=application["id"], query="secret-term-123")
    call(t, app, "model.search", application_id=application["id"], query="x", surprise=1)
    with app.state.session_factory() as s:
        events = s.scalars(select(AuditEvent).where(AuditEvent.action == "mcp.tool_call").order_by(AuditEvent.created_at)).all()
    assert len(events) == 2
    assert events[0].meta["tool"] == "model.search" and events[0].meta["ok"] is True
    assert len(events[0].meta["args_sha256"]) == 64 and "secret-term-123" not in json.dumps(events[0].meta)
    assert events[1].meta["ok"] is False and events[1].meta["error"] == "invalid_arguments"
    with app.state.session_factory() as s:
        assert s.scalars(select(Proposal)).first() is None


def test_rate_limit_per_token(world, app):
    *_, token = world
    t = token(["*"])
    app.state.mcp_limiter.per_minute = 3
    codes = [rpc(t, app, "tools/call", {"name": "model.search", "arguments": {"application_id": None}}, id_=i).status_code
             for i in range(5)]
    assert codes == [200, 200, 200, 429, 429]
    assert rpc(t, app, "ping").status_code == 200  # only tool calls are limited
    other = token(["*"])
    assert rpc(other, app, "tools/call", {"name": "ping-nothing"}).status_code == 200
