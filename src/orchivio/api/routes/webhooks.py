"""Outbound webhook subscriptions and their deliveries (docs/INTEGRATIONS.md, FR-082)."""

from __future__ import annotations

import re
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import desc, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from orchivio.db.models import WebhookDelivery, WebhookSubscription
from orchivio.integrations import DestinationError, SecretUnavailable, check_url, destination_policy, resolve_secret, valid_pattern
from orchivio.integrations.dispatcher import SECRET_REF, ping_delivery

from ..deps import Principal, api_error, audit, current_principal, get_db, load_application, not_found, utcnow
from ..schemas import Page

router = APIRouter(tags=["webhooks"])

FIELD = re.compile(r"^(\*|[a-z][a-z0-9_]{0,62})$")


class WebhookIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,99}$")
    url: str = Field(max_length=2000)
    secret_ref: str = Field(max_length=300, description="env:NAME or file:/path holding the signing secret on the server")
    events: list[str] = Field(min_length=1, max_length=20, description="glob patterns such as core.Product.created, core.*.updated, *")
    fields: list[str] | None = Field(default=None, max_length=100, description="record fields to include; null = ids only; ['*'] = all")


class WebhookPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str | None = Field(default=None, max_length=2000)
    secret_ref: str | None = Field(default=None, max_length=300)
    events: list[str] | None = Field(default=None, min_length=1, max_length=20)
    fields: list[str] | None = Field(default=None, max_length=100)
    clear_fields: bool = False
    status: str | None = Field(default=None, pattern="^(active|paused)$")


def sub_out(w: WebhookSubscription) -> dict[str, Any]:
    return {"id": str(w.id), "application_id": str(w.application_id), "name": w.name, "url": w.url, "secret_ref": w.secret_ref,
            "events": w.events, "fields": w.fields, "status": w.status, "paused_reason": w.paused_reason,
            "consecutive_failures": w.consecutive_failures, "created_at": w.created_at.isoformat() if w.created_at else None}


def delivery_out(d: WebhookDelivery, payload: bool = False) -> dict[str, Any]:
    out = {"id": str(d.id), "subscription_id": str(d.subscription_id), "event_id": d.event_id, "event_type": d.event_type,
           "status": d.status, "attempt": d.attempt, "max_attempts": d.max_attempts, "response_status": d.response_status,
           "error": d.error, "available_at": d.available_at.isoformat(),
           "last_attempt_at": d.last_attempt_at.isoformat() if d.last_attempt_at else None,
           "delivered_at": d.delivered_at.isoformat() if d.delivered_at else None}
    if payload:
        out["payload"] = d.payload
    return out


def _validate(request: Request, *, url: str | None, secret_ref: str | None, events: list[str] | None, fields: list[str] | None) -> None:
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
    for p in events or []:
        if not valid_pattern(p):
            raise api_error(422, "invalid_event_pattern", f"{p!r}: use letters, digits, . _ - and * only")
    for f in fields or []:
        if not FIELD.match(f):
            raise api_error(422, "invalid_field", f"{f!r} is not a field name or '*'")


def _load(db: Session, principal: Principal, webhook_id: uuid.UUID):
    w = db.get(WebhookSubscription, webhook_id)
    if w is None:
        raise not_found("webhook")
    app, project = load_application(db, principal, w.application_id, "integration.manage")
    return w, app, project


@router.post("/applications/{app_id}/webhooks", status_code=201)
def create_webhook(app_id: uuid.UUID, body: WebhookIn, request: Request, principal: Principal = Depends(current_principal),
                   db: Session = Depends(get_db)) -> dict[str, Any]:
    app, project = load_application(db, principal, app_id, "integration.manage")
    _validate(request, url=body.url, secret_ref=body.secret_ref, events=body.events, fields=body.fields)
    w = WebhookSubscription(application_id=app.id, name=body.name, url=body.url, secret_ref=body.secret_ref,
                            events=sorted(set(body.events)), fields=sorted(set(body.fields)) if body.fields is not None else None,
                            created_by=principal.user_id)
    db.add(w)
    try:
        db.flush()
    except IntegrityError as e:
        db.rollback()
        raise api_error(409, "name_taken", "a webhook with this name already exists") from e
    audit(db, request, "webhook.created", actor_id=principal.user_id, org_id=project.organization_id, target_type="webhook",
          target_id=w.id, meta={"name": w.name, "events": w.events, "fields": w.fields})  # the URL may contain tokens: not logged
    db.commit()
    return sub_out(w)


@router.get("/applications/{app_id}/webhooks")
def list_webhooks(app_id: uuid.UUID, principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict[str, Any]:
    app, _ = load_application(db, principal, app_id, "integration.manage")
    rows = db.scalars(select(WebhookSubscription).where(WebhookSubscription.application_id == app.id).order_by(WebhookSubscription.name))
    return {"items": [sub_out(w) for w in rows]}


@router.get("/webhooks/{webhook_id}")
def get_webhook(webhook_id: uuid.UUID, principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict[str, Any]:
    w, _, _ = _load(db, principal, webhook_id)
    return sub_out(w)


@router.patch("/webhooks/{webhook_id}")
def update_webhook(webhook_id: uuid.UUID, body: WebhookPatch, request: Request, principal: Principal = Depends(current_principal),
                   db: Session = Depends(get_db)) -> dict[str, Any]:
    w, _, project = _load(db, principal, webhook_id)
    _validate(request, url=body.url, secret_ref=body.secret_ref, events=body.events, fields=body.fields)
    changed = []
    for attr in ("url", "secret_ref"):
        if getattr(body, attr) is not None:
            setattr(w, attr, getattr(body, attr))
            changed.append(attr)
    if body.events is not None:
        w.events, _ = sorted(set(body.events)), changed.append("events")
    if body.fields is not None or body.clear_fields:
        w.fields, _ = (None if body.clear_fields else sorted(set(body.fields or []))), changed.append("fields")
    if body.status is not None:
        w.status = body.status
        if body.status == "active":
            w.consecutive_failures, w.paused_reason = 0, None
        else:
            w.paused_reason = "paused by an administrator"
        changed.append("status")
    audit(db, request, "webhook.updated", actor_id=principal.user_id, org_id=project.organization_id, target_type="webhook",
          target_id=w.id, meta={"name": w.name, "changed": changed})
    db.commit()
    return sub_out(w)


@router.delete("/webhooks/{webhook_id}", status_code=204)
def delete_webhook(webhook_id: uuid.UUID, request: Request, principal: Principal = Depends(current_principal),
                   db: Session = Depends(get_db)) -> Response:
    w, _, project = _load(db, principal, webhook_id)
    audit(db, request, "webhook.deleted", actor_id=principal.user_id, org_id=project.organization_id, target_type="webhook",
          target_id=w.id, meta={"name": w.name})
    db.delete(w)
    db.commit()
    return Response(status_code=204)


@router.post("/webhooks/{webhook_id}/test", status_code=202)
def test_webhook(webhook_id: uuid.UUID, principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Queue one ``orchivio.ping`` delivery (a single attempt); watch it under the deliveries."""
    w, _, _ = _load(db, principal, webhook_id)
    did = ping_delivery(db, w, utcnow())
    db.commit()
    return delivery_out(db.get(WebhookDelivery, did))


@router.get("/webhooks/{webhook_id}/deliveries", response_model=Page[dict])
def list_deliveries(webhook_id: uuid.UUID, status: str | None = Query(None, pattern="^(queued|sending|delivered|dead)$"),
                    limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                    principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> Page[Any]:
    w, _, _ = _load(db, principal, webhook_id)
    q = select(WebhookDelivery).where(WebhookDelivery.subscription_id == w.id)
    if status:
        q = q.where(WebhookDelivery.status == status)
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    rows = db.scalars(q.order_by(desc(WebhookDelivery.created_at), WebhookDelivery.id).limit(limit).offset(offset)).all()
    return Page(items=[delivery_out(d) for d in rows], limit=limit, offset=offset, total=total)


def _delivery(db: Session, principal: Principal, delivery_id: uuid.UUID):
    d = db.get(WebhookDelivery, delivery_id)
    if d is None:
        raise not_found("delivery")
    w, app, project = _load(db, principal, d.subscription_id)
    return d, w, project


@router.get("/webhook-deliveries/{delivery_id}")
def get_delivery(delivery_id: uuid.UUID, principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict[str, Any]:
    d, _, _ = _delivery(db, principal, delivery_id)
    return delivery_out(d, payload=True)


@router.post("/webhook-deliveries/{delivery_id}/retry")
def retry_delivery(delivery_id: uuid.UUID, request: Request, principal: Principal = Depends(current_principal),
                   db: Session = Depends(get_db)) -> dict[str, Any]:
    """Give a dead delivery a fresh set of attempts."""
    d, w, project = _delivery(db, principal, delivery_id)
    won = db.execute(update(WebhookDelivery).where(WebhookDelivery.id == d.id, WebhookDelivery.status == "dead")
                     .values(status="queued", attempt=0, available_at=utcnow(), error=None)).rowcount
    db.expire(d)
    if not won:
        raise api_error(409, "not_dead", "only dead deliveries can be retried")
    audit(db, request, "webhook.delivery_retried", actor_id=principal.user_id, org_id=project.organization_id,
          target_type="webhook", target_id=w.id, meta={"delivery": str(d.id), "event_type": d.event_type})
    db.commit()
    return delivery_out(d)
