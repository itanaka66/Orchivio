"""Outbox relay and webhook delivery (FR-082, FR-083).

Pipeline: runtime outbox (same transaction as the data change) -> relay -> ``webhook_deliveries`` (platform
database, one row per matching subscription) -> send. Every hop is at-least-once and idempotent: the relay may
repeat after a crash (unique ``(subscription, event)`` makes that harmless) and a delivery may be sent more than
once (receivers deduplicate on the event id, ``Orchivio-Event-Id``). Ordering is not guaranteed.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os
import re
import uuid
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from orchivio.db.models import Application, AuditEvent, Project, WebhookDelivery, WebhookSubscription
from orchivio.runtime import outbox

from .ssrf import DestinationError
from .transport import Transport, sign

log = logging.getLogger("orchivio.webhooks")

PATTERN = re.compile(r"^[A-Za-z0-9_.*-]{1,200}$")
SECRET_REF = re.compile(r"^(env|file):[A-Za-z0-9_./:\\-]{1,250}$")
RETRYABLE_STATUS = {408, 425, 429}
MAX_ERROR = 500


class SecretUnavailable(Exception):
    pass


def valid_pattern(p: str) -> bool:
    return bool(PATTERN.match(p))


def matches(patterns: list[str], event_type: str) -> bool:
    return any(fnmatch.fnmatchcase(event_type, p) for p in patterns)


def resolve_secret(ref: str) -> str:
    """Look a signing secret up at send time. Supports ``env:NAME`` and ``file:/path`` (one line)."""
    scheme, _, value = ref.partition(":")
    if scheme == "env":
        secret = os.environ.get(value)
    elif scheme == "file":
        try:
            secret = Path(value).read_text(encoding="utf-8").strip()
        except OSError:
            secret = None
    else:
        raise SecretUnavailable(f"unsupported secret scheme {scheme!r}")
    if not secret:
        raise SecretUnavailable(f"secret {ref} is not available on this server")
    return secret


def build_event(row: dict[str, Any], app_id: str, fields: list[str] | None) -> dict[str, Any]:
    """CloudEvents 1.0 structured JSON. Without ``fields`` the receiver learns only that something happened."""
    payload = row["payload"]
    data: dict[str, Any] = {"entity": payload["entity"], "id": payload["id"]}
    if fields is not None:
        record = payload.get("record", {})
        keep = record if "*" in fields else {k: v for k, v in record.items() if k in fields or k == "id"}
        data["record"] = keep
        if "changed" in payload:
            data["changed"] = [c for c in payload["changed"] if "*" in fields or c in fields]
    occurred = row["occurred_at"]
    occurred = occurred if occurred.tzinfo else occurred.replace(tzinfo=UTC)
    return {"specversion": "1.0", "id": row["event_id"], "source": f"orchivio:app:{app_id}", "type": row["type"],
            "time": occurred.isoformat(), "subject": row["subject"], "datacontenttype": "application/json", "data": data}


def _insert_delivery(session: Session, values: dict[str, Any]) -> bool:
    name = session.get_bind().dialect.name
    if name == "postgresql":
        stmt = postgresql.insert(WebhookDelivery).values(**values).on_conflict_do_nothing()
    else:
        stmt = sqlite.insert(WebhookDelivery).values(**values).on_conflict_do_nothing()
    return session.execute(stmt).rowcount == 1


def _aware(d: datetime | None) -> datetime | None:
    return d if d is None or d.tzinfo else d.replace(tzinfo=UTC)


def ping_delivery(session: Session, sub: WebhookSubscription, now: datetime) -> uuid.UUID | None:
    """A synthetic ``orchivio.ping`` event for one subscription (not stored in the outbox); one attempt only."""
    event_id = str(uuid.uuid4())
    payload = {"specversion": "1.0", "id": event_id, "source": f"orchivio:app:{sub.application_id}", "type": "orchivio.ping",
               "time": now.isoformat(), "subject": "ping", "datacontenttype": "application/json", "data": {"ping": True}}
    did = uuid.uuid4()
    ok = _insert_delivery(session, {"id": did, "subscription_id": sub.id, "event_id": event_id, "event_type": "orchivio.ping",
                                    "payload": payload, "status": "queued", "attempt": 0, "max_attempts": 1,
                                    "available_at": now, "created_at": now})
    return did if ok else None


@dataclass
class DispatchResult:
    relayed: int = 0
    created: int = 0
    delivered: int = 0
    retried: int = 0
    dead: int = 0


class Dispatcher:
    def __init__(self, sessions: sessionmaker[Session], preview_engine: Engine, transport: Transport, *,
                 secrets: Callable[[str], str] = resolve_secret, worker_id: str | None = None,
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC), lease_seconds: int = 120, batch: int = 20,
                 max_attempts: int = 6, backoff_seconds: int = 30, pause_after_failures: int = 10,
                 relay_batch: int = 200, retention_days: int = 7) -> None:
        self.sessions, self.preview, self.transport, self.secrets = sessions, preview_engine, transport, secrets
        self.id = worker_id or f"dispatcher-{uuid.uuid4().hex[:8]}"
        self.clock, self.lease, self.batch = clock, lease_seconds, batch
        self.max_attempts, self.backoff, self.pause_after = max_attempts, backoff_seconds, pause_after_failures
        self.relay_batch, self.retention = relay_batch, timedelta(days=retention_days)

    def tick(self) -> DispatchResult:
        out = DispatchResult()
        out.relayed, out.created = self.relay()
        self.deliver(out)
        return out

    # -- relay ----------------------------------------------------------------------------------

    def relay(self) -> tuple[int, int]:
        rows = outbox.pending(self.preview, self.relay_batch)
        if not rows:
            return 0, 0
        now, created = self.clock(), 0
        with self.sessions() as s:
            apps = {uuid.UUID(r["application_id"]) for r in rows}
            subs: dict[str, list[WebhookSubscription]] = defaultdict(list)
            for sub in s.scalars(select(WebhookSubscription).where(WebhookSubscription.application_id.in_(apps),
                                                                   WebhookSubscription.status == "active")):
                subs[str(sub.application_id)].append(sub)
            for r in rows:
                for sub in subs.get(r["application_id"], []):
                    if matches(sub.events, r["type"]):
                        created += _insert_delivery(s, {
                            "id": uuid.uuid4(), "subscription_id": sub.id, "event_id": r["event_id"], "event_type": r["type"],
                            "payload": build_event(r, r["application_id"], sub.fields), "status": "queued", "attempt": 0,
                            "max_attempts": self.max_attempts, "available_at": now, "created_at": now})
            s.commit()
        outbox.mark_relayed(self.preview, [r["id"] for r in rows])  # after the commit: a crash repeats the relay, harmlessly
        outbox.purge(self.preview, relayed_before=now - self.retention)
        return len(rows), created

    def enqueue_test(self, sub: WebhookSubscription, session: Session) -> uuid.UUID | None:
        return ping_delivery(session, sub, self.clock())

    # -- delivery -------------------------------------------------------------------------------

    def _claim(self, s: Session, now: datetime) -> list[uuid.UUID]:
        for stale in s.scalars(select(WebhookDelivery).where(WebhookDelivery.status == "sending", WebhookDelivery.locked_until < now)):
            self._finish(s, stale, ok=False, status=None, error="delivery lease expired before it finished", now=now, worker=None)
        due = s.scalars(select(WebhookDelivery).join(WebhookSubscription, WebhookSubscription.id == WebhookDelivery.subscription_id)
                        .where(WebhookDelivery.status == "queued", WebhookDelivery.available_at <= now,
                               WebhookSubscription.status == "active")
                        .order_by(WebhookDelivery.available_at, WebhookDelivery.id).limit(self.batch)).all()
        taken = []
        for d in due:
            won = s.execute(update(WebhookDelivery).where(WebhookDelivery.id == d.id, WebhookDelivery.status == "queued").values(
                status="sending", locked_by=self.id, locked_until=now + timedelta(seconds=self.lease),
                attempt=WebhookDelivery.attempt + 1, last_attempt_at=now)).rowcount
            if won:
                taken.append(d.id)
        s.commit()
        return taken

    def deliver(self, out: DispatchResult) -> None:
        now = self.clock()
        with self.sessions() as s:
            ids = self._claim(s, now)
        for did in ids:
            with self.sessions() as s:
                d = s.get(WebhookDelivery, did)
                sub = s.get(WebhookSubscription, d.subscription_id)
                ok, status, error, permanent = self._send(sub, d)
                result = self._finish(s, d, ok=ok, status=status, error=error, now=self.clock(), worker=self.id, permanent=permanent)
                s.commit()
            out.delivered += result == "delivered"
            out.retried += result == "retry"
            out.dead += result == "dead"

    def _send(self, sub: WebhookSubscription, d: WebhookDelivery) -> tuple[bool, int | None, str | None, bool]:
        try:
            secret = self.secrets(sub.secret_ref)
        except SecretUnavailable as e:
            return False, None, str(e), False
        body = json.dumps(d.payload, separators=(",", ":"), ensure_ascii=False, sort_keys=True).encode("utf-8")
        headers = {"Content-Type": "application/cloudevents+json; charset=utf-8", "User-Agent": "Orchivio-Webhooks/1",
                   "Orchivio-Signature": sign(secret, body), "Orchivio-Event-Id": d.event_id, "Orchivio-Event-Type": d.event_type,
                   "Orchivio-Delivery-Id": str(d.id), "Orchivio-Attempt": str(d.attempt)}
        try:
            resp = self.transport.post(sub.url, headers, body)
        except DestinationError as e:
            return False, None, f"destination refused: {e}", True
        except OSError as e:
            return False, None, f"{type(e).__name__}: {e}"[:MAX_ERROR], False
        if 200 <= resp.status < 300:
            return True, resp.status, None, False
        retryable = resp.status >= 500 or resp.status in RETRYABLE_STATUS
        return False, resp.status, f"receiver answered HTTP {resp.status}", not retryable

    def _finish(self, s: Session, d: WebhookDelivery, *, ok: bool, status: int | None, error: str | None, now: datetime,
                worker: str | None, permanent: bool = False) -> str:
        guard = [WebhookDelivery.id == d.id, WebhookDelivery.status == "sending"]
        if worker is not None:
            guard.append(WebhookDelivery.locked_by == worker)
        sub = s.get(WebhookSubscription, d.subscription_id)
        if ok:
            won = s.execute(update(WebhookDelivery).where(*guard).values(status="delivered", delivered_at=now, response_status=status,
                                                                          error=None, locked_by=None, locked_until=None)).rowcount
            if won and sub is not None:
                sub.consecutive_failures = 0
            s.expire(d)
            return "delivered" if won else "lost"
        if permanent or d.attempt >= d.max_attempts:
            vals: dict[str, Any] = {"status": "dead"}
            outcome = "dead"
        else:
            delay = min(self.backoff * 2 ** (d.attempt - 1), 6 * 3600)
            vals = {"status": "queued", "available_at": now + timedelta(seconds=delay)}
            outcome = "retry"
        won = s.execute(update(WebhookDelivery).where(*guard).values(response_status=status, error=(error or "")[:MAX_ERROR],
                                                                      locked_by=None, locked_until=None, **vals)).rowcount
        s.expire(d)
        if not won:
            return "lost"
        if sub is not None:
            sub.consecutive_failures += 1
            if sub.status == "active" and sub.consecutive_failures >= self.pause_after:
                sub.status, sub.paused_reason = "paused", f"{sub.consecutive_failures} consecutive failed deliveries"
                self._audit(s, sub, "webhook.paused", {"reason": sub.paused_reason})
        if outcome == "dead":
            self._audit(s, sub, "webhook.delivery_dead", {"delivery": str(d.id), "event_type": d.event_type, "error": (error or "")[:200]})
        return outcome

    @staticmethod
    def _audit(s: Session, sub: WebhookSubscription | None, action: str, meta: dict[str, Any]) -> None:
        if sub is None:
            return
        app = s.get(Application, sub.application_id)
        project = s.get(Project, app.project_id) if app else None
        s.add(AuditEvent(organization_id=project.organization_id if project else None, actor_id=None, plane="platform",
                         channel="api", action=action, target_type="webhook", target_id=str(sub.id),
                         meta={"name": sub.name, **meta}))
