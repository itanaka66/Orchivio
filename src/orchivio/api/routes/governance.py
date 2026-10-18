"""Governance console for an organization (FR-124): policy packs, compliance, app inventory, AI usage, audit export.

Everything here is organization-wide and needs the admin-level ``governance.manage``. Policy packs are enforced where models
are planned (``RevisionRepository.plan``), so every way of changing an application (revisions, proposals, AI, imports,
merges, rollbacks) meets the same rules; this module manages the packs and reports on the state of the organization.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import uuid
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from orchivio.db import to_model
from orchivio.db.models import (
    AIJob,
    Application,
    AppOidcProvider,
    AppRevision,
    AppUser,
    AuditEvent,
    Environment,
    InboundWebhook,
    PackageDeployment,
    PolicyPack,
    Project,
    Proposal,
    WebhookSubscription,
)
from orchivio.model.policies import RULES, evaluate, parse_rules

from ..deps import (
    Principal,
    api_error,
    audit,
    authorize,
    current_principal,
    get_db,
    not_found,
    utcnow,
)

router = APIRouter(tags=["governance"])

MAX_EXPORT_ROWS = 50_000
MAX_AI_JOBS = 100_000


class PackIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,99}$")
    description: str | None = Field(default=None, max_length=500)
    rules: list[dict[str, Any]] = Field(max_length=50)
    status: str = Field(default="active", pattern="^(active|disabled)$")


class PackPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    description: str | None = Field(default=None, max_length=500)
    rules: list[dict[str, Any]] | None = Field(default=None, max_length=50)
    status: str | None = Field(default=None, pattern="^(active|disabled)$")


def pack_out(p: PolicyPack) -> dict[str, Any]:
    return {"id": str(p.id), "name": p.name, "description": p.description, "status": p.status, "rules": p.rules}


def _rules(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    try:
        return [r.model_dump(mode="json") for r in parse_rules(raw)]
    except ValueError as e:
        raise api_error(422, "invalid_rules", str(e), {"catalogue": {k: v[1] for k, v in RULES.items()}}) from e


def _pack(db: Session, principal: Principal, org_id: uuid.UUID, pack_id: uuid.UUID) -> PolicyPack:
    authorize(db, principal, org_id, "governance.manage")
    p = db.get(PolicyPack, pack_id)
    if p is None or p.organization_id != org_id:
        raise not_found("policy pack")
    return p


# --- policy packs ------------------------------------------------------------------------------


@router.get("/governance/policy-rules")
def catalogue(principal: Principal = Depends(current_principal)) -> dict[str, Any]:
    """The rules a pack can use, with what their parameters are."""
    return {"rules": [{"rule": k, "description": v[1]} for k, v in RULES.items()]}


@router.post("/organizations/{org_id}/policy-packs", status_code=201)
def create_pack(org_id: uuid.UUID, body: PackIn, request: Request, principal: Principal = Depends(current_principal),
                db: Session = Depends(get_db)) -> dict[str, Any]:
    authorize(db, principal, org_id, "governance.manage")
    p = PolicyPack(organization_id=org_id, name=body.name, description=body.description, rules=_rules(body.rules), status=body.status,
                   created_by=principal.user_id)
    db.add(p)
    try:
        db.flush()
    except IntegrityError as e:
        db.rollback()
        raise api_error(409, "name_taken", "a policy pack with this name already exists") from e
    audit(db, request, "policy_pack.created", actor_id=principal.user_id, org_id=org_id, target_type="policy_pack", target_id=p.id,
          meta={"name": p.name, "rules": [r["rule"] for r in p.rules], "status": p.status})
    db.commit()
    return pack_out(p)


@router.get("/organizations/{org_id}/policy-packs")
def list_packs(org_id: uuid.UUID, principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict[str, Any]:
    authorize(db, principal, org_id, "governance.manage")
    rows = db.scalars(select(PolicyPack).where(PolicyPack.organization_id == org_id).order_by(PolicyPack.name))
    return {"items": [pack_out(p) for p in rows]}


@router.get("/organizations/{org_id}/policy-packs/{pack_id}")
def get_pack(org_id: uuid.UUID, pack_id: uuid.UUID, principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict[str, Any]:
    return pack_out(_pack(db, principal, org_id, pack_id))


@router.patch("/organizations/{org_id}/policy-packs/{pack_id}")
def update_pack(org_id: uuid.UUID, pack_id: uuid.UUID, body: PackPatch, request: Request, principal: Principal = Depends(current_principal),
                db: Session = Depends(get_db)) -> dict[str, Any]:
    p = _pack(db, principal, org_id, pack_id)
    changed = []
    if body.description is not None:
        p.description = body.description
        changed.append("description")
    if body.rules is not None:
        p.rules = _rules(body.rules)
        changed.append("rules")
    if body.status is not None:
        p.status = body.status
        changed.append("status")
    audit(db, request, "policy_pack.updated", actor_id=principal.user_id, org_id=org_id, target_type="policy_pack", target_id=p.id,
          meta={"name": p.name, "changed": changed, "status": p.status})
    db.commit()
    return pack_out(p)


@router.delete("/organizations/{org_id}/policy-packs/{pack_id}", status_code=204)
def delete_pack(org_id: uuid.UUID, pack_id: uuid.UUID, request: Request, principal: Principal = Depends(current_principal),
                db: Session = Depends(get_db)) -> Response:
    p = _pack(db, principal, org_id, pack_id)
    audit(db, request, "policy_pack.deleted", actor_id=principal.user_id, org_id=org_id, target_type="policy_pack", target_id=p.id, meta={"name": p.name})
    db.delete(p)
    db.commit()
    return Response(status_code=204)


# --- compliance and inventory ---------------------------------------------------------------------


def _apps(db: Session, org_id: uuid.UUID) -> list[tuple[Application, Project]]:
    return list(db.execute(select(Application, Project).join(Project, Project.id == Application.project_id)
                           .where(Project.organization_id == org_id).order_by(Project.name, Application.key)).all())


def _model(db: Session, app: Application):
    return to_model(db.get(AppRevision, app.current_revision_id)) if app.current_revision_id else None


@router.get("/organizations/{org_id}/governance/compliance")
def compliance(org_id: uuid.UUID, principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict[str, Any]:
    """How every application's *current* model measures up against the active packs. Existing apps are not changed by a
    new pack; this shows what the next change to them has to fix."""
    authorize(db, principal, org_id, "governance.manage")
    packs = list(db.scalars(select(PolicyPack).where(PolicyPack.organization_id == org_id, PolicyPack.status == "active").order_by(PolicyPack.name)))
    items, totals = [], Counter()
    for app, project in _apps(db, org_id):
        model = _model(db, app)
        violations = []
        if model is not None:
            for pack in packs:
                violations += [{**v.as_dict(), "pack": pack.name} for v in evaluate(model, pack.rules)]
        blocking = sum(1 for v in violations if v["severity"] == "error")
        status = "no_model" if model is None else "violations" if blocking else "warnings" if violations else "compliant"
        totals[status] += 1
        items.append({"application_id": str(app.id), "project": project.slug, "application": app.key, "status": status, "violations": violations})
    return {"packs": [p.name for p in packs], "summary": dict(totals), "applications": items}


def _inventory_row(db: Session, app: Application, project: Project, counts: dict[str, dict[uuid.UUID, int]]) -> dict[str, Any]:
    model = _model(db, app)
    row: dict[str, Any] = {"application_id": str(app.id), "project": project.slug, "application": app.key, "name": app.name,
                           "revision_no": None, "modules": 0, "entities": 0, "flows": 0, "processes": 0, "integrations": 0, "pages": 0, "risks": []}
    rev = db.get(AppRevision, app.current_revision_id) if app.current_revision_id else None
    if rev is not None and model is not None:
        row["revision_no"] = rev.revision_no
        row["last_change_at"] = rev.created_at.isoformat() if rev.created_at else None
        row["modules"] = len(model.modules)
        row["entities"] = sum(1 for m in model.modules for e in m.entities if not e.transient)
        row["flows"] = sum(len(m.flows) for m in model.modules)
        row["processes"] = sum(len(m.processes or []) for m in model.modules)
        row["integrations"] = sum(len(m.integrations or []) for m in model.modules)
        row["pages"] = sum(len(m.pages) for m in model.modules)
        risks = row["risks"]
        if model.security.anonymous:
            risks.append("anonymous-access")
        if any("export" in r.operations for r in model.security.access):
            risks.append("export-granted")
        if row["integrations"]:
            risks.append("external-integrations")
        if any(f.run_roles for m in model.modules for f in m.flows):
            risks.append("flows-runnable-by-users")
        if any(f.schedule is not None and f.schedule.enabled for m in model.modules for f in m.flows):
            risks.append("scheduled-flows")
    if (project.settings or {}).get("ai_allow_external") is True:
        row["risks"].append("external-ai-allowed")
    for key in ("app_users", "webhooks", "inbound_webhooks", "open_proposals", "environments"):
        row[key] = counts[key].get(app.id, 0)
    if counts["oidc_auto"].get(app.id):
        row["risks"].append("oidc-auto-provisioning")
    if row["webhooks"]:
        row["risks"].append("outbound-webhooks")
    return row


@router.get("/organizations/{org_id}/governance/inventory")
def inventory(org_id: uuid.UUID, principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Every application of the organization with what it contains and the settings that deserve a second look."""
    authorize(db, principal, org_id, "governance.manage")
    apps = _apps(db, org_id)
    ids = [a.id for a, _ in apps]

    def grouped(model_col, app_col, *where) -> dict[uuid.UUID, int]:
        if not ids:
            return {}
        return {k: v for k, v in db.execute(select(app_col, func.count()).where(app_col.in_(ids), *where).group_by(app_col)).all()}

    deployed = defaultdict(int)
    if ids:
        for env_app, n in db.execute(select(Environment.application_id, func.count(func.distinct(Environment.id)))
                                     .join(PackageDeployment, PackageDeployment.environment_id == Environment.id)
                                     .where(Environment.application_id.in_(ids), PackageDeployment.status == "deployed")
                                     .group_by(Environment.application_id)).all():
            deployed[env_app] = n
    counts = {"app_users": grouped(None, AppUser.application_id), "webhooks": grouped(None, WebhookSubscription.application_id, WebhookSubscription.status == "active"),
              "inbound_webhooks": grouped(None, InboundWebhook.application_id, InboundWebhook.status == "active"),
              "open_proposals": grouped(None, Proposal.application_id, Proposal.status == "pending"), "environments": dict(deployed),
              "oidc_auto": grouped(None, AppOidcProvider.application_id, AppOidcProvider.auto_provision.is_(True), AppOidcProvider.status == "active")}
    items = [_inventory_row(db, a, p, counts) for a, p in apps]
    risks = Counter(r for i in items for r in i["risks"])
    return {"summary": {"applications": len(items), "with_risks": sum(1 for i in items if i["risks"]), "risks": dict(risks)}, "applications": items}


# --- AI usage -------------------------------------------------------------------------------------


def _window(start: str | None, end: str | None) -> tuple[datetime, datetime]:
    def parse(v: str | None, default: datetime) -> datetime:
        if v is None:
            return default
        try:
            d = datetime.fromisoformat(v)
        except ValueError as e:
            raise api_error(422, "invalid_date", "use ISO 8601 dates, e.g. 2026-03-01 or 2026-03-01T00:00:00Z") from e
        return d if d.tzinfo else d.replace(tzinfo=UTC)

    now = utcnow()
    lo, hi = parse(start, now - timedelta(days=30)), parse(end, now + timedelta(seconds=1))
    if lo >= hi:
        raise api_error(422, "invalid_date", "'from' must be before 'to'")
    return lo, hi


def _aware(d: datetime) -> datetime:
    return d if d.tzinfo else d.replace(tzinfo=UTC)


@router.get("/organizations/{org_id}/governance/ai-usage")
def ai_usage(org_id: uuid.UUID, start: str | None = Query(None, alias="from"), end: str | None = Query(None, alias="to"),
             principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict[str, Any]:
    """AI Builder jobs of the organization in a window (default: 30 days): counts, tokens and cost by project, job type and
    provider/model, plus how often external MCP tools were consulted. Cost comes from what providers reported."""
    authorize(db, principal, org_id, "governance.manage")
    lo, hi = _window(start, end)
    rows = db.execute(select(AIJob, Project.slug).join(Project, Project.id == AIJob.project_id)
                      .where(Project.organization_id == org_id, AIJob.created_at >= lo, AIJob.created_at < hi)
                      .order_by(AIJob.created_at).limit(MAX_AI_JOBS + 1)).all()
    if len(rows) > MAX_AI_JOBS:
        raise api_error(422, "window_too_large", f"more than {MAX_AI_JOBS} AI jobs in this window; narrow it")
    tot: Counter = Counter()
    by: dict[str, dict[str, Counter]] = {"project": defaultdict(Counter), "type": defaultdict(Counter), "model": defaultdict(Counter)}
    for job, slug in rows:
        u = job.usage or {}
        cells = {"jobs": 1, f"status_{job.status}": 1, "input_tokens": int(u.get("input_tokens", 0) or 0), "output_tokens": int(u.get("output_tokens", 0) or 0),
                 "tool_calls": len(u.get("tool_calls", []) or [])}
        cost = float(u.get("cost_usd", 0) or 0)
        for key, bucket in (("project", slug), ("type", job.type), ("model", f"{job.provider or '-'}/{job.model or '-'}")):
            by[key][bucket].update(cells)
            by[key][bucket]["cost_usd"] += cost
        tot.update(cells)
        tot["cost_usd"] += cost
    mcp = db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.organization_id == org_id, AuditEvent.action == "mcp.client_call",
                                                                       AuditEvent.created_at >= lo, AuditEvent.created_at < hi)) or 0

    def shape(c: Counter) -> dict[str, Any]:
        d = dict(c)
        d["cost_usd"] = round(float(d.get("cost_usd", 0)), 6)
        return d

    return {"from": lo.isoformat(), "to": hi.isoformat(), "totals": shape(tot), "mcp_tool_calls": mcp,
            **{f"by_{k}": {name: shape(c) for name, c in sorted(v.items())} for k, v in by.items()}}


# --- audit export ---------------------------------------------------------------------------------


def _cell(value: Any) -> str:
    s = "" if value is None else (json.dumps(value, ensure_ascii=False, sort_keys=True) if isinstance(value, (dict, list)) else str(value))
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s  # spreadsheets must not execute audit text


@router.get("/organizations/{org_id}/governance/audit-export")
def audit_export(org_id: uuid.UUID, request: Request, start: str | None = Query(None, alias="from"), end: str | None = Query(None, alias="to"),
                 plane: str | None = Query(None, pattern="^(platform|application)$"), action_prefix: str | None = Query(None, max_length=100),
                 after_id: int = Query(0, ge=0), limit: int = Query(10_000, ge=1, le=MAX_EXPORT_ROWS), format: str = Query("jsonl", pattern="^(jsonl|csv)$"),
                 principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> Response:
    """Download audit events of the organization (oldest first) as JSON lines or CSV, for archiving or a SIEM. Continue with
    ``after_id`` = the last id of the previous page (``X-Last-Id``). Every export is itself an audit event."""
    authorize(db, principal, org_id, "governance.manage")
    lo, hi = _window(start, end)
    q = select(AuditEvent).where(AuditEvent.organization_id == org_id, AuditEvent.id > after_id, AuditEvent.created_at >= lo, AuditEvent.created_at < hi)
    if plane:
        q = q.where(AuditEvent.plane == plane)
    if action_prefix:
        q = q.where(AuditEvent.action.like(action_prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%", escape="\\"))
    rows = list(db.scalars(q.order_by(AuditEvent.id).limit(limit + 1)))
    more = len(rows) > limit
    rows = rows[:limit]
    cols = ["id", "created_at", "organization_id", "actor_id", "plane", "channel", "action", "target_type", "target_id", "metadata"]

    def rec(e: AuditEvent) -> dict[str, Any]:
        return {"id": e.id, "created_at": _aware(e.created_at).isoformat(), "organization_id": str(e.organization_id) if e.organization_id else None,
                "actor_id": str(e.actor_id) if e.actor_id else None, "plane": e.plane, "channel": e.channel, "action": e.action,
                "target_type": e.target_type, "target_id": e.target_id, "metadata": e.meta}

    if format == "csv":
        out = io.StringIO(newline="")
        w = csv.writer(out, lineterminator="\r\n")
        w.writerow(cols)
        for e in rows:
            r = rec(e)
            w.writerow([_cell(r[c]) for c in cols])
        body, media = b"\xef\xbb\xbf" + out.getvalue().encode("utf-8"), "text/csv; charset=utf-8"
    else:
        body, media = "".join(json.dumps(rec(e), ensure_ascii=False, sort_keys=True) + "\n" for e in rows).encode("utf-8"), "application/x-ndjson"
    digest = hashlib.sha256(body).hexdigest()
    audit(db, request, "audit.exported", actor_id=principal.user_id, org_id=org_id, target_type="organization", target_id=org_id,
          meta={"rows": len(rows), "format": format, "plane": plane, "action_prefix": action_prefix, "from": lo.isoformat(), "to": hi.isoformat(),
                "sha256": digest})
    db.commit()
    headers = {"X-Row-Count": str(len(rows)), "X-Content-SHA256": digest, "X-More": "true" if more else "false",
               "Content-Disposition": f'attachment; filename="audit-{org_id}.{ "csv" if format == "csv" else "jsonl"}"'}
    if rows:
        headers["X-Last-Id"] = str(rows[-1].id)
    return Response(body, media_type=media, headers=headers)
