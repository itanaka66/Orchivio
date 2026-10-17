"""Inbound webhooks: signed requests from other systems that start a server flow (docs/INTEGRATIONS.md).

The public endpoint has no session or token. A request is accepted only if its signature verifies against the
webhook's secret (same scheme as outbound deliveries: HMAC-SHA256 over ``"<t>.<body>"``, replay window), and every
failure before that point looks alike so the endpoint reveals nothing. Accepted requests are deduplicated on the
event id and then queued as a job, so the flow runs with retries, as the webhook's fixed application role.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, desc, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from orchivio.db import to_model
from orchivio.db.models import AppRevision, Application, AuditEvent, InboundDelivery, InboundWebhook, Project
from orchivio.integrations import SecretUnavailable, resolve_secret, verify_signature
from orchivio.integrations.dispatcher import SECRET_REF
from orchivio.jobs import queue
from orchivio.model.validate import literal_fits

from ..deps import Principal, api_error, audit, current_principal, get_db, load_application, not_found, utcnow

log = logging.getLogger("orchivio.inbound")
router = APIRouter(tags=["inbound webhooks"])

KEY = r"^[a-z0-9][a-z0-9-]{0,62}$"
EVENT_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
RETENTION_DAYS = 7
_MISSING = object()


class InboundIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(pattern=KEY)
    flow: str = Field(min_length=1, max_length=130)
    run_as_role: str = Field(min_length=1, max_length=64)
    secret_ref: str = Field(max_length=300)
    arg_map: dict[str, str] = Field(default_factory=dict, max_length=30, description="flow parameter -> JSON pointer into the payload")
    defaults: dict[str, str | int | float | bool | None] = Field(default_factory=dict, max_length=30)


class InboundPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_as_role: str | None = Field(default=None, max_length=64)
    secret_ref: str | None = Field(default=None, max_length=300)
    arg_map: dict[str, str] | None = Field(default=None, max_length=30)
    defaults: dict[str, str | int | float | bool | None] | None = Field(default=None, max_length=30)
    status: str | None = Field(default=None, pattern="^(active|paused)$")


def pointer_get(doc: Any, pointer: str) -> Any:
    """RFC 6901 lookup; ``_MISSING`` when absent."""
    if pointer == "":
        return doc
    cur = doc
    for raw in pointer[1:].split("/"):
        part = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return _MISSING
    return cur


def _valid_pointer(p: str) -> bool:
    return p == "" or (p.startswith("/") and re.fullmatch(r"(?:[^~]|~[01])*", p) is not None)


def _flow_params(db: Session, app: Application, qualified: str):
    if app.current_revision_id is None:
        raise api_error(409, "no_revision", "the application has no model yet")
    model = to_model(db.get(AppRevision, app.current_revision_id))
    q = qualified if "." in qualified else f"{model.modules[0].key}.{qualified}"
    flow = next((f for m in model.modules for f in m.flows if f"{m.key}.{f.key}" == q), None)
    if flow is None or flow.kind != "server":
        raise api_error(422, "unknown_flow", "no such server flow")
    if any(q == f"{m.key}.{h}" for m in model.modules for e in m.entities for h in e.hooks.values()):
        raise api_error(422, "not_runnable", "an entity hook flow runs on its event and cannot be a webhook target")
    return model, q, flow


def _check_mapping(model, flow, role: str, secret_ref: str, arg_map: dict[str, str], defaults: dict[str, Any]) -> None:
    if role not in model.security.roles:
        raise api_error(422, "unknown_role", "the model does not define this role", {"defined": model.security.roles})
    if not SECRET_REF.match(secret_ref):
        raise api_error(422, "invalid_secret_ref", "use env:NAME or file:/path")
    try:
        resolve_secret(secret_ref)
    except SecretUnavailable as e:
        raise api_error(422, "secret_unavailable", str(e)) from e
    params = {p.name: p for p in flow.params}
    problems = []
    for name, ptr in arg_map.items():
        if name not in params:
            problems.append({"param": name, "message": "the flow has no such parameter"})
        elif not _valid_pointer(ptr):
            problems.append({"param": name, "message": "not a JSON pointer (RFC 6901), e.g. /data/id"})
    for name, value in defaults.items():
        if name not in params:
            problems.append({"param": name, "message": "the flow has no such parameter"})
        elif not literal_fits(params[name].type, value):
            problems.append({"param": name, "message": f"default does not fit type '{params[name].type}'"})
    for name in params:
        if name not in arg_map and name not in defaults:
            problems.append({"param": name, "message": "needs a payload pointer or a default"})
    if problems:
        raise api_error(422, "invalid_mapping", "the parameter mapping is incomplete or invalid", problems)


def hook_out(w: InboundWebhook) -> dict[str, Any]:
    try:
        resolve_secret(w.secret_ref)
        available = True
    except SecretUnavailable:
        available = False
    return {"id": str(w.id), "application_id": str(w.application_id), "key": w.key, "url_path": f"/api/v1/hooks/{w.application_id}/{w.key}",
            "flow": w.flow, "run_as_role": w.run_as_role, "secret_ref": w.secret_ref, "secret_available": available,
            "arg_map": w.arg_map, "defaults": w.defaults, "status": w.status}


def _load(db: Session, principal: Principal, webhook_id: uuid.UUID):
    w = db.get(InboundWebhook, webhook_id)
    if w is None:
        raise not_found("inbound webhook")
    app, project = load_application(db, principal, w.application_id, "integration.manage")
    return w, app, project


# --- management (admin) -----------------------------------------------------------------------


@router.post("/applications/{app_id}/inbound-webhooks", status_code=201)
def create(app_id: uuid.UUID, body: InboundIn, request: Request, principal: Principal = Depends(current_principal),
           db: Session = Depends(get_db)) -> dict[str, Any]:
    app, project = load_application(db, principal, app_id, "integration.manage")
    model, q, flow = _flow_params(db, app, body.flow)
    _check_mapping(model, flow, body.run_as_role, body.secret_ref, body.arg_map, body.defaults)
    w = InboundWebhook(application_id=app.id, key=body.key, flow=q, run_as_role=body.run_as_role, secret_ref=body.secret_ref,
                       arg_map=body.arg_map, defaults=body.defaults, created_by=principal.user_id)
    db.add(w)
    try:
        db.flush()
    except IntegrityError as e:
        db.rollback()
        raise api_error(409, "key_taken", "an inbound webhook with this key already exists") from e
    audit(db, request, "inbound_webhook.created", actor_id=principal.user_id, org_id=project.organization_id, target_type="inbound_webhook",
          target_id=w.id, meta={"key": w.key, "flow": q, "role": w.run_as_role})
    db.commit()
    return hook_out(w)


@router.get("/applications/{app_id}/inbound-webhooks")
def list_hooks(app_id: uuid.UUID, principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict[str, Any]:
    app, _ = load_application(db, principal, app_id, "integration.manage")
    rows = db.scalars(select(InboundWebhook).where(InboundWebhook.application_id == app.id).order_by(InboundWebhook.key))
    return {"items": [hook_out(w) for w in rows]}


@router.get("/inbound-webhooks/{webhook_id}")
def get_hook(webhook_id: uuid.UUID, principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict[str, Any]:
    w, _, _ = _load(db, principal, webhook_id)
    return hook_out(w)


@router.patch("/inbound-webhooks/{webhook_id}")
def update_hook(webhook_id: uuid.UUID, body: InboundPatch, request: Request, principal: Principal = Depends(current_principal),
                db: Session = Depends(get_db)) -> dict[str, Any]:
    w, app, project = _load(db, principal, webhook_id)
    model, _, flow = _flow_params(db, app, w.flow)
    role = body.run_as_role or w.run_as_role
    ref = body.secret_ref or w.secret_ref
    arg_map = body.arg_map if body.arg_map is not None else w.arg_map
    defaults = body.defaults if body.defaults is not None else w.defaults
    _check_mapping(model, flow, role, ref, arg_map, defaults)
    w.run_as_role, w.secret_ref, w.arg_map, w.defaults = role, ref, arg_map, defaults
    if body.status is not None:
        w.status = body.status
    audit(db, request, "inbound_webhook.updated", actor_id=principal.user_id, org_id=project.organization_id,
          target_type="inbound_webhook", target_id=w.id, meta={"key": w.key, "status": w.status, "role": role})
    db.commit()
    return hook_out(w)


@router.delete("/inbound-webhooks/{webhook_id}", status_code=204)
def delete_hook(webhook_id: uuid.UUID, request: Request, principal: Principal = Depends(current_principal),
                db: Session = Depends(get_db)) -> Response:
    w, _, project = _load(db, principal, webhook_id)
    audit(db, request, "inbound_webhook.deleted", actor_id=principal.user_id, org_id=project.organization_id,
          target_type="inbound_webhook", target_id=w.id, meta={"key": w.key})
    db.delete(w)
    db.commit()
    return Response(status_code=204)


@router.get("/inbound-webhooks/{webhook_id}/events")
def events(webhook_id: uuid.UUID, limit: int = Query(50, ge=1, le=200), principal: Principal = Depends(current_principal),
           db: Session = Depends(get_db)) -> dict[str, Any]:
    """Recent accepted requests and the job each one started."""
    w, _, _ = _load(db, principal, webhook_id)
    rows = db.scalars(select(InboundDelivery).where(InboundDelivery.webhook_id == w.id).order_by(desc(InboundDelivery.received_at)).limit(limit))
    return {"items": [{"event_id": d.event_id, "job_id": str(d.job_id) if d.job_id else None, "received_at": d.received_at.isoformat()}
                      for d in rows]}


# --- the public endpoint ------------------------------------------------------------------------


def _unauthorized() -> Exception:
    return api_error(401, "invalid_signature", "the request signature is missing, wrong or too old")


@router.post("/hooks/{app_id}/{key}", status_code=202)
async def receive(app_id: uuid.UUID, key: str, request: Request, db: Session = Depends(get_db)) -> dict[str, Any]:
    w = db.scalar(select(InboundWebhook).where(InboundWebhook.application_id == app_id, InboundWebhook.key == key))
    if w is None or w.status != "active":
        raise not_found("inbound webhook")
    settings = request.app.state.settings
    if not request.app.state.inbound_limiter.allow(w.id):
        raise api_error(429, "rate_limited", "too many requests", headers={"Retry-After": "60"})
    if int(request.headers.get("content-length") or 0) > settings.inbound_max_body_bytes:
        raise api_error(413, "payload_too_large", "the request body is too large")
    body = await request.body()
    if len(body) > settings.inbound_max_body_bytes:
        raise api_error(413, "payload_too_large", "the request body is too large")

    header = request.headers.get("orchivio-signature", "")
    try:
        secret = resolve_secret(w.secret_ref)
    except SecretUnavailable:
        log.error("inbound webhook %s: signing secret %s is not available", w.id, w.secret_ref)
        raise _unauthorized() from None  # indistinguishable from a bad signature
    if not verify_signature(secret, header, body):
        raise _unauthorized()

    event_id = request.headers.get("webhook-id") or hashlib.sha256(header.encode()).hexdigest()[:64]
    if not EVENT_ID.match(event_id):
        raise api_error(400, "invalid_event_id", "Webhook-Id must be 1-128 characters of letters, digits and . _ : -")
    try:
        payload = json.loads(body)
    except ValueError:
        raise api_error(400, "invalid_payload", "the body must be JSON") from None

    app = db.get(Application, w.application_id)
    model, q, flow = _flow_params(db, app, w.flow)
    params = {p.name: p for p in flow.params}
    args: dict[str, Any] = {}
    problems = []
    for name, p in params.items():
        value = pointer_get(payload, w.arg_map[name]) if name in w.arg_map else _MISSING
        if value is _MISSING:
            if name in w.defaults:
                value = w.defaults[name]
            else:
                problems.append({"param": name, "message": f"payload has nothing at {w.arg_map.get(name)}"})
                continue
        if not literal_fits(p.type, value):
            problems.append({"param": name, "message": f"does not fit type '{p.type}'"})
            continue
        args[name] = value
    if problems:
        raise api_error(422, "invalid_payload", "the payload does not fit the flow's parameters", problems)

    now = utcnow()
    db.execute(delete(InboundDelivery).where(InboundDelivery.received_at < now - timedelta(days=RETENTION_DAYS)))
    receipt = InboundDelivery(webhook_id=w.id, event_id=event_id, received_at=now)
    db.add(receipt)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        return {"accepted": True, "duplicate": True, "job_id": None}
    job_id = queue.enqueue(db, application_id=w.application_id, flow=q, args=args, run_as_role=w.run_as_role, trigger="webhook",
                           max_attempts=3, backoff_seconds=30, source_ref=f"inbound:{w.id}:{event_id}"[:200], at=now)
    receipt.job_id = job_id
    project = db.get(Project, app.project_id)
    db.add(AuditEvent(organization_id=project.organization_id if project else None, actor_id=None, plane="application", channel="api",
                      action="inbound_webhook.accepted", target_type="inbound_webhook", target_id=str(w.id),
                      meta={"key": w.key, "flow": q, "event_id": event_id, "job_id": str(job_id)}))
    db.commit()
    return {"accepted": True, "duplicate": False, "job_id": str(job_id)}
