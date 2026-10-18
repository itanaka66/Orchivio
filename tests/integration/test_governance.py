"""Governance console: policy packs and their enforcement, compliance, inventory, AI usage and audit export."""

import copy
import csv
import hashlib
import io
import json
from datetime import UTC, datetime, timedelta

import pytest
from test_tenancy import add_member, make_org

from orchivio.db.models import AIJob, AuditEvent


def anonymous(raw):
    m = copy.deepcopy(raw)
    m["security"]["roles"].append("anonymous")
    m["security"]["access"].append({"entity": "core.Product", "role": "anonymous", "operations": ["read"]})
    m["security"]["anonymous"] = True
    return m


@pytest.fixture
def world(new_api, raw_model, app):
    owner = new_api("owner@example.com")
    org = make_org(owner)
    admin = new_api("admin@example.com")
    add_member(owner, org, "admin@example.com", "admin")
    editor = new_api("editor@example.com")
    add_member(owner, org, "editor@example.com", "editor")
    outsider = new_api("outsider@example.com")
    proj = owner.post(f"/organizations/{org['id']}/projects", json={"name": "Inv", "slug": "inventory"}).json()
    a = owner.post(f"/projects/{proj['id']}/applications", json={"key": "inventory", "name": "Inventory", "model": raw_model}).json()
    return org, proj, a, {"owner": owner, "admin": admin, "editor": editor, "outsider": outsider}, raw_model


def packs_url(org):
    return f"/organizations/{org['id']}/policy-packs"


def make_pack(owner, org, rules=None, **kw):
    body = {"name": "baseline", "rules": rules or [{"rule": "no-anonymous-access"}], **kw}
    r = owner.post(packs_url(org), json=body)
    assert r.status_code == 201, r.text
    return r.json()


# --- packs -------------------------------------------------------------------------------------


def test_pack_management_is_admin_only_and_validated(world):
    org, proj, a, p, _ = world
    o = p["owner"]
    pack = make_pack(o, org, description="Our baseline")
    assert pack["status"] == "active" and pack["rules"] == [{"rule": "no-anonymous-access", "params": {}, "severity": "error"}]
    assert o.post(packs_url(org), json={"name": "baseline", "rules": [{"rule": "no-anonymous-access"}]}).json()["error"]["code"] == "name_taken"
    for rules in ([{"rule": "bogus"}], [], [{"rule": "max-roles", "params": {"max": 0}}], [{"rule": "no-anonymous-access", "surprise": 1}]):
        r = o.post(packs_url(org), json={"name": "x", "rules": rules})
        assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_rules" and "no-anonymous-access" in r.json()["error"]["details"]["catalogue"], rules
    assert o.post(packs_url(org), json={"name": "bad name!", "rules": [{"rule": "no-anonymous-access"}]}).status_code == 422
    assert p["admin"].get(packs_url(org)).status_code == 200  # organization admins manage governance
    assert p["editor"].get(packs_url(org)).status_code == 403 and p["editor"].post(packs_url(org), json={}).status_code in (403, 422)
    assert p["outsider"].get(packs_url(org)).status_code == 404
    patched = o.patch(f"{packs_url(org)}/{pack['id']}", json={"rules": [{"rule": "max-roles", "params": {"max": 5}}], "status": "disabled"}).json()
    assert patched["status"] == "disabled" and patched["rules"][0]["rule"] == "max-roles"
    assert o.patch(f"{packs_url(org)}/{pack['id']}", json={"rules": [{"rule": "nope"}]}).status_code == 422
    assert o.get("/governance/policy-rules").json()["rules"][0]["rule"] == "no-anonymous-access"
    assert o.delete(f"{packs_url(org)}/{pack['id']}").status_code == 204 and o.get(f"{packs_url(org)}/{pack['id']}").status_code == 404


def test_packs_do_not_cross_organizations(world, new_api):
    org, proj, a, p, _ = world
    pack = make_pack(p["owner"], org)
    other = new_api("other@example.com")
    org2 = make_org(other, slug="other-org")
    assert other.get(f"{packs_url(org2)}/{pack['id']}").status_code == 404
    assert other.get(f"/organizations/{org['id']}/governance/inventory").status_code == 404
    assert other.get(packs_url(org2)).json()["items"] == []


# --- enforcement -------------------------------------------------------------------------------


def test_error_rules_block_every_way_of_changing_a_model(world):
    org, proj, a, p, raw = world
    o = p["owner"]
    make_pack(o, org)
    bad = anonymous(raw)
    r = o.post(f"/applications/{a['id']}/revisions", json={"model": bad, "acknowledge_risks": True})
    assert r.status_code == 422 and r.json()["error"]["code"] == "model_invalid"
    issue = next(i for i in r.json()["error"]["details"] if i["code"] == "policy:no-anonymous-access")
    assert issue["path"] == "/security/anonymous" and "not allowed in this organization" in issue["message"]
    prop = o.post(f"/applications/{a['id']}/proposals", json={"model": bad, "title": "open it up"})
    assert prop.status_code == 422 and any(i["code"] == "policy:no-anonymous-access" for i in prop.json()["error"]["details"])
    assert o.get(f"/applications/{a['id']}").json()["current_revision_no"] == 1  # nothing slipped through
    assert o.post(f"/applications/{a['id']}/revisions", json={"model": raw}).json()["error"]["code"] == "no_changes"


def test_warning_rules_inform_but_do_not_block_and_disabled_packs_do_nothing(world):
    org, proj, a, p, raw = world
    o = p["owner"]
    pack = make_pack(o, org, [{"rule": "no-anonymous-access", "severity": "warning"}])
    ok = o.post(f"/applications/{a['id']}/revisions", json={"model": anonymous(raw), "acknowledge_risks": True})
    assert ok.status_code == 201, ok.text
    o.patch(f"{packs_url(org)}/{pack['id']}", json={"status": "active", "rules": [{"rule": "no-anonymous-access"}]})
    again = o.post(f"/applications/{a['id']}/revisions", json={"model": raw, "acknowledge_risks": True})  # removing a role is an A3 security change
    assert again.status_code == 201  # going back to compliant is fine
    o.patch(f"{packs_url(org)}/{pack['id']}", json={"status": "disabled"})
    assert o.post(f"/applications/{a['id']}/revisions", json={"model": anonymous(raw), "acknowledge_risks": True}).status_code == 201


def test_only_the_organizations_own_packs_apply(world, new_api):
    org, proj, a, p, raw = world
    other = new_api("other@example.com")
    org2 = make_org(other, slug="other-org")
    make_pack(other, org2)  # a strict pack elsewhere
    assert p["owner"].post(f"/applications/{a['id']}/revisions", json={"model": anonymous(raw), "acknowledge_risks": True}).status_code == 201


def test_branch_merges_cannot_smuggle_in_what_a_policy_forbids(world):
    org, proj, a, p, raw = world
    o = p["owner"]
    branch = o.post(f"/applications/{a['id']}/branches", json={"name": "open-up"}).json()
    assert o.post(f"/applications/{a['id']}/revisions", json={"model": anonymous(raw), "branch": "open-up"}).status_code == 201
    make_pack(o, org)  # the rule arrives while the branch is open
    r = o.post(f"/branches/{branch['id']}/merge", json={"acknowledge_risks": True})
    assert r.status_code == 422 and r.json()["error"]["code"] == "merge_invalid"
    assert any(i["code"] == "policy:no-anonymous-access" for i in r.json()["error"]["details"])


def test_other_rules_enforce_too(world):
    org, proj, a, p, raw = world
    o = p["owner"]
    make_pack(o, org, [{"rule": "forbid-operations", "params": {"operations": ["export"]}}, {"rule": "max-roles", "params": {"max": 3}}], name="data")
    m = copy.deepcopy(raw)
    next(r for r in m["security"]["access"] if r["role"] == "viewer")["operations"].append("export")
    r = o.post(f"/applications/{a['id']}/revisions", json={"model": m, "acknowledge_risks": True})
    assert r.status_code == 422 and any(i["code"] == "policy:forbid-operations" for i in r.json()["error"]["details"])
    m2 = copy.deepcopy(raw)
    m2["security"]["roles"].append("extra")
    m2["security"]["access"].append({"entity": "core.Product", "role": "extra", "operations": ["read"]})
    r2 = o.post(f"/applications/{a['id']}/revisions", json={"model": m2, "acknowledge_risks": True})
    assert r2.status_code == 422 and any(i["code"] == "policy:max-roles" for i in r2.json()["error"]["details"])


# --- compliance and inventory ----------------------------------------------------------------------


def test_compliance_shows_existing_apps_a_new_pack_would_reject(world):
    org, proj, a, p, raw = world
    o = p["owner"]
    o.post(f"/applications/{a['id']}/revisions", json={"model": anonymous(raw), "acknowledge_risks": True})
    o.post(f"/projects/{proj['id']}/applications", json={"key": "empty", "name": "No model yet"})
    make_pack(o, org)
    make_pack(o, org, [{"rule": "max-roles", "params": {"max": 1}, "severity": "warning"}], name="advice")
    rep = o.get(f"/organizations/{org['id']}/governance/compliance").json()
    assert rep["packs"] == ["advice", "baseline"] and rep["summary"] == {"violations": 1, "no_model": 1}
    app = next(x for x in rep["applications"] if x["application"] == "inventory")
    assert {(v["pack"], v["rule"], v["severity"]) for v in app["violations"]} == {("baseline", "no-anonymous-access", "error"), ("advice", "max-roles", "warning")}
    assert o.post(f"/applications/{a['id']}/revisions", json={"model": raw, "acknowledge_risks": True}).status_code == 201
    again = o.get(f"/organizations/{org['id']}/governance/compliance").json()
    assert next(x for x in again["applications"] if x["application"] == "inventory")["status"] == "warnings"
    assert p["editor"].get(f"/organizations/{org['id']}/governance/compliance").status_code == 403


def test_inventory_lists_apps_with_counts_and_risk_flags(world, monkeypatch):
    org, proj, a, p, raw = world
    o = p["owner"]
    monkeypatch.setenv("ORCHIVIO_TEST_HOOK_SECRET", "s3cret-value")
    m = anonymous(raw)
    next(r for r in m["security"]["access"] if r["role"] == "viewer")["operations"].append("export")
    o.post(f"/applications/{a['id']}/revisions", json={"model": m, "acknowledge_risks": True})
    o.post(f"/applications/{a['id']}/app-users", json={"username": "alice", "password": "temporary pass phrase 1", "roles": ["viewer"]})
    o.post(f"/applications/{a['id']}/webhooks", json={"name": "main", "url": "https://hooks.example.com/x", "secret_ref": "env:ORCHIVIO_TEST_HOOK_SECRET",
                                                      "events": ["*"]})
    o.post(f"/projects/{proj['id']}/applications", json={"key": "second", "name": "Second", "model": raw})
    inv = o.get(f"/organizations/{org['id']}/governance/inventory").json()
    assert inv["summary"]["applications"] == 2
    first = next(x for x in inv["applications"] if x["application"] == "inventory")
    assert first["revision_no"] == 2 and first["entities"] == 1 and first["flows"] == 1 and first["pages"] == 2
    assert first["app_users"] == 1 and first["webhooks"] == 1 and first["open_proposals"] == 0
    assert {"anonymous-access", "export-granted", "flows-runnable-by-users", "outbound-webhooks"} <= set(first["risks"])
    second = next(x for x in inv["applications"] if x["application"] == "second")
    assert second["risks"] == ["flows-runnable-by-users"] and second["app_users"] == 0
    assert inv["summary"]["with_risks"] == 2 and inv["summary"]["risks"]["anonymous-access"] == 1
    assert "s3cret-value" not in json.dumps(inv) and "hooks.example.com" not in json.dumps(inv)  # inventory is a summary, not a data dump


def test_inventory_flags_external_ai_and_oidc_provisioning(world):
    org, proj, a, p, raw = world
    o = p["owner"]
    o.patch(f"/projects/{proj['id']}", json={"settings": {"ai_allow_external": True}})
    inv = o.get(f"/organizations/{org['id']}/governance/inventory").json()
    assert "external-ai-allowed" in inv["applications"][0]["risks"]


# --- AI usage --------------------------------------------------------------------------------------


def add_job(app, project_id, *, type_="propose_model", status="succeeded", provider="anthropic", model="m1", usage=None, when=None):
    with app.state.session_factory() as s:
        j = AIJob(project_id=project_id, type=type_, status=status, provider=provider, model=model, usage=usage or {})
        if when is not None:
            j.created_at = when
        s.add(j)
        s.commit()


def test_ai_usage_aggregates_by_project_type_and_model(world, app):
    org, proj, a, p, raw = world
    o = p["owner"]
    pid = proj["id"]
    import uuid
    pid = uuid.UUID(pid)
    add_job(app, pid, usage={"input_tokens": 1000, "output_tokens": 200, "cost_usd": 0.05, "tool_calls": [{"tool": "x"}, {"tool": "y"}]})
    add_job(app, pid, usage={"input_tokens": 500, "output_tokens": 100, "cost_usd": 0.02})
    add_job(app, pid, type_="analyze_requirements", status="failed", provider="mock", model=None, usage={"input_tokens": 10})
    add_job(app, pid, usage={"input_tokens": 99999, "cost_usd": 99}, when=datetime.now(UTC) - timedelta(days=90))  # outside the default window
    with app.state.session_factory() as s:
        s.add(AuditEvent(organization_id=uuid.UUID(org["id"]), plane="platform", channel="api", action="mcp.client_call", meta={}))
        s.add(AuditEvent(organization_id=uuid.uuid4(), plane="platform", channel="api", action="mcp.client_call", meta={}))  # another organization
        s.commit()
    u = o.get(f"/organizations/{org['id']}/governance/ai-usage").json()
    assert u["totals"]["jobs"] == 3 and u["totals"]["input_tokens"] == 1510 and u["totals"]["output_tokens"] == 300
    assert u["totals"]["cost_usd"] == 0.07 and u["totals"]["tool_calls"] == 2 and u["totals"]["status_failed"] == 1 and u["mcp_tool_calls"] == 1
    assert u["by_project"]["inventory"]["jobs"] == 3
    assert u["by_type"]["propose_model"]["jobs"] == 2 and u["by_type"]["analyze_requirements"]["status_failed"] == 1
    assert u["by_model"]["anthropic/m1"]["input_tokens"] == 1500 and u["by_model"]["mock/-"]["jobs"] == 1
    wide = o.get(f"/organizations/{org['id']}/governance/ai-usage?from=2000-01-01").json()
    assert wide["totals"]["jobs"] == 4 and wide["totals"]["cost_usd"] == 99.07
    for q in ("from=garbage", "from=2026-05-02&to=2026-05-01"):
        assert o.get(f"/organizations/{org['id']}/governance/ai-usage?{q}").json()["error"]["code"] == "invalid_date"
    assert p["editor"].get(f"/organizations/{org['id']}/governance/ai-usage").status_code == 403


# --- audit export ----------------------------------------------------------------------------------


def export(c, org, **q):
    qs = "&".join(f"{k}={v}" for k, v in q.items())
    return c.get(f"/organizations/{org['id']}/governance/audit-export" + (f"?{qs}" if qs else ""))


def test_audit_export_jsonl_is_complete_ordered_and_hashed(world):
    org, proj, a, p, raw = world
    o = p["owner"]
    r = export(o, org)
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/x-ndjson")
    rows = [json.loads(line) for line in r.text.splitlines()]
    assert [x["id"] for x in rows] == sorted(x["id"] for x in rows) and len(rows) == int(r.headers["X-Row-Count"])
    assert {"project.created", "revision.created"} <= {x["action"] for x in rows}
    assert hashlib.sha256(r.content).hexdigest() == r.headers["X-Content-SHA256"] and r.headers["X-More"] == "false"
    assert all(x["organization_id"] == org["id"] for x in rows)
    again = [json.loads(line) for line in export(o, org).text.splitlines()]
    assert "audit.exported" in {x["action"] for x in again}  # the export of the audit log is itself on record
    me = next(x for x in again if x["action"] == "audit.exported")
    assert me["metadata"]["sha256"] == r.headers["X-Content-SHA256"] and me["metadata"]["rows"] == len(rows)


def test_audit_export_filters_and_pages(world):
    org, proj, a, p, raw = world
    o = p["owner"]
    for _ in range(5):
        make = o.post(f"/applications/{a['id']}/revisions", json={"model": raw})  # no_changes: still not audited; use real changes instead
    assert make.status_code == 409
    o.post(packs_url(org), json={"name": "p1", "rules": [{"rule": "no-anonymous-access"}]})
    o.post(packs_url(org), json={"name": "p2", "rules": [{"rule": "no-anonymous-access"}]})
    only = [json.loads(x) for x in export(o, org, action_prefix="policy_pack.").text.splitlines()]
    assert [x["action"] for x in only] == ["policy_pack.created", "policy_pack.created"]
    assert export(o, org, action_prefix="%").text == ""  # LIKE wildcards in the filter are literal
    assert export(o, org, action_prefix="_").text == ""
    assert all(json.loads(x)["plane"] == "platform" for x in export(o, org, plane="platform").text.splitlines())
    first = export(o, org, limit=2)
    assert first.headers["X-Row-Count"] == "2" and first.headers["X-More"] == "true"
    seen = [json.loads(x)["id"] for x in first.text.splitlines()]
    nxt = export(o, org, limit=2, after_id=first.headers["X-Last-Id"])
    seen2 = [json.loads(x)["id"] for x in nxt.text.splitlines()]
    assert seen2 and min(seen2) > max(seen)
    assert export(o, org, **{"from": "2000-01-01", "to": "2000-01-02"}).text == "" and export(o, org, format="xml").status_code == 422
    assert export(o, org, **{"from": "2999-01-01"}).json()["error"]["code"] == "invalid_date"  # the window would end before it starts
    assert export(o, org, limit=0).status_code == 422 and export(o, org, plane="other").status_code == 422


def test_audit_export_csv_is_formula_safe_and_organization_scoped(world, app):
    org, proj, a, p, raw = world
    import uuid
    with app.state.session_factory() as s:
        s.add(AuditEvent(organization_id=uuid.UUID(org["id"]), plane="application", channel="api", action="=HYPERLINK(\"http://evil\")", target_id="+1+1",
                         meta={"note": "@SUM(A1)"}))
        s.add(AuditEvent(organization_id=uuid.uuid4(), plane="platform", channel="api", action="other.org.secret", meta={}))
        s.commit()
    r = export(p["owner"], org, format="csv")
    assert r.status_code == 200 and r.content.startswith(b"\xef\xbb\xbf") and r.headers["content-disposition"].endswith('.csv"')
    rows = list(csv.reader(io.StringIO(r.content.decode("utf-8-sig"))))
    assert rows[0] == ["id", "created_at", "organization_id", "actor_id", "plane", "channel", "action", "target_type", "target_id", "metadata"]
    evil = next(x for x in rows[1:] if "HYPERLINK" in x[6])
    assert evil[6].startswith("'=") and evil[8] == "'+1+1"
    assert "other.org.secret" not in r.text
    assert not any(cell[:1] in ("=", "+", "@") for row in rows[1:] for cell in row if cell)


def test_audit_export_needs_governance_rights(world):
    org, proj, a, p, raw = world
    assert export(p["editor"], org).status_code == 403 and export(p["outsider"], org).status_code == 404
    assert export(p["admin"], org).status_code == 200
    with_filter = export(p["owner"], org, action_prefix="policy_pack.")
    assert with_filter.status_code == 200
    # failed attempts leave no export record
    rows = [json.loads(x) for x in export(p["owner"], org, action_prefix="audit.exported").text.splitlines()]
    assert len(rows) == 2  # the admin's and the owner's filtered export; the 403 and 404 attempts left no record
