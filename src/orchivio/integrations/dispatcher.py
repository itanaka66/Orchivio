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

from orchivio import telemetry
from orchivio.db.models import (
    Application,
    AuditEvent,
    Project,
    WebhookDelivery,
    WebhookSubscription,
)
from orchivio.runtime import outbox

from .ssrf import DestinationError
from .transport import Transport, sign

log = logging.getLogger("orchivio.webhooks")

PATTERN = re.compile(r"^[A-Za-z0-9_.*-]{1,200}$")
SECRET_REF = re.compile(r"^(env|vault|file|aws-sm|gcp-sm|azure-kv):[A-Za-z0-9_./:@#-]{1,250}$")
RETRYABLE_STATUS = {408, 425, 429}
MAX_ERROR = 500


class SecretUnavailable(Exception):
    pass


def valid_pattern(p: str) -> bool:
    return bool(PATTERN.match(p))


def matches(patterns: list[str], event_type: str) -> bool:
    return any(fnmatch.fnmatchcase(event_type, p) for p in patterns)


def _resolve_vault(path: str) -> str:
    """HashiCorp Vault KV backend.  Needs VAULT_ADDR and VAULT_TOKEN env vars.

    Path format: ``secret/data/myapp`` (KV v2) or ``secret/myapp`` (KV v1).
    Append ``#field`` to extract a specific key from the JSON payload; without
    it the whole secret string is returned (only works for single-string KV v1
    secrets).
    """
    import json as _json
    import urllib.error as _uerr
    import urllib.request as _ureq

    path, _, field = path.partition("#")
    addr = os.environ.get("VAULT_ADDR", "http://127.0.0.1:8200").rstrip("/")
    token = os.environ.get("VAULT_TOKEN", "")
    if not token:
        raise SecretUnavailable("VAULT_TOKEN is not set")
    url = f"{addr}/v1/{path}"
    req = _ureq.Request(url, headers={"X-Vault-Token": token})
    try:
        with _ureq.urlopen(req, timeout=5) as resp:  # noqa: S310
            data = _json.loads(resp.read())
    except _uerr.HTTPError as exc:
        raise SecretUnavailable(f"vault returned HTTP {exc.code} for {path}") from exc
    except OSError as exc:
        raise SecretUnavailable(f"vault unreachable: {exc}") from exc
    # KV v2 wraps the payload under data.data; KV v1 uses data directly
    payload = data.get("data", {})
    if "data" in payload:
        payload = payload["data"]
    if field:
        val = payload.get(field)
        if not val:
            raise SecretUnavailable(f"vault secret {path} has no field {field!r}")
        return str(val)
    # KV v1: data is {"value": "..."} or a bare string (rare)
    if isinstance(payload, str):
        return payload
    raise SecretUnavailable(f"vault secret {path}: specify a field with #fieldname")


def _resolve_aws_sm(name: str) -> str:
    """AWS Secrets Manager backend.  Relies on the standard boto3 credential chain."""
    try:
        import boto3  # type: ignore[import]
    except ImportError as exc:
        raise SecretUnavailable("boto3 is not installed; cannot resolve aws-sm: secret refs") from exc
    import json as _json
    client = boto3.client("secretsmanager")
    try:
        resp = client.get_secret_value(SecretId=name)
    except Exception as exc:  # noqa: BLE001
        raise SecretUnavailable(f"aws-sm: could not retrieve {name}: {exc}") from exc
    secret = resp.get("SecretString") or ""
    if not secret:
        raise SecretUnavailable(f"aws-sm: {name} has no string value")
    # If the secret is JSON, try to return it as-is (callers can use #field)
    return secret


def _resolve_gcp_sm(resource: str) -> str:
    """GCP Secret Manager backend.  Relies on Application Default Credentials.

    Resource format: ``projects/PROJECT/secrets/NAME/versions/VERSION``.
    """
    try:
        from google.cloud import secretmanager as _sm  # type: ignore[import]
    except ImportError as exc:
        raise SecretUnavailable(
            "google-cloud-secret-manager is not installed; cannot resolve gcp-sm: secret refs"
        ) from exc
    client = _sm.SecretManagerServiceClient()
    try:
        response = client.access_secret_version(name=resource)
    except Exception as exc:  # noqa: BLE001
        raise SecretUnavailable(f"gcp-sm: could not access {resource}: {exc}") from exc
    return response.payload.data.decode("utf-8").strip()


def _resolve_azure_kv(uri: str) -> str:
    """Azure Key Vault backend.  Relies on DefaultAzureCredential.

    URI format: ``https://VAULT.vault.azure.net/secrets/NAME`` (version optional as trailing path segment).
    """
    try:
        from azure.identity import DefaultAzureCredential  # type: ignore[import]
        from azure.keyvault.secrets import SecretClient  # type: ignore[import]
    except ImportError as exc:
        raise SecretUnavailable(
            "azure-keyvault-secrets and azure-identity are not installed; cannot resolve azure-kv: secret refs"
        ) from exc
    # Split vault URL from secret name (and optional version)
    # Expected: https://VAULT.vault.azure.net/secrets/NAME[/VERSION]
    parts = uri.split("/secrets/", 1)
    if len(parts) != 2:
        raise SecretUnavailable(f"azure-kv: invalid URI format: {uri!r}")
    vault_url, secret_path = parts[0], parts[1]
    secret_name = secret_path.split("/")[0]
    version = secret_path.split("/")[1] if "/" in secret_path else None
    credential = DefaultAzureCredential()
    client = SecretClient(vault_url=vault_url, credential=credential)
    try:
        secret = client.get_secret(secret_name, version=version)
    except Exception as exc:  # noqa: BLE001
        raise SecretUnavailable(f"azure-kv: could not retrieve {secret_name}: {exc}") from exc
    if not secret.value:
        raise SecretUnavailable(f"azure-kv: {secret_name} has no value")
    return secret.value


def resolve_secret(ref: str) -> str:
    """Resolve a secret reference.

    Supported schemes:
    - ``env:VAR_NAME`` — environment variable
    - ``file:/path/to/file`` — single-line text file
    - ``vault:secret/data/path[#field]`` — HashiCorp Vault (needs VAULT_ADDR + VAULT_TOKEN)
    - ``aws-sm:secret-name`` — AWS Secrets Manager (uses boto3 credential chain)
    - ``gcp-sm:projects/P/secrets/S/versions/V`` — GCP Secret Manager (uses ADC)
    - ``azure-kv:https://VAULT.vault.azure.net/secrets/NAME`` — Azure Key Vault (uses DefaultAzureCredential)
    """
    scheme, _, value = ref.partition(":")
    if scheme == "env":
        secret: str | None = os.environ.get(value)
    elif scheme == "file":
        try:
            secret = Path(value).read_text(encoding="utf-8").strip()
        except OSError:
            secret = None
    elif scheme == "vault":
        return _resolve_vault(value)
    elif scheme == "aws-sm":
        return _resolve_aws_sm(value)
    elif scheme == "gcp-sm":
        return _resolve_gcp_sm(value)
    elif scheme == "azure-kv":
        return _resolve_azure_kv(f"{scheme}:{value}")
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
                with telemetry.span("webhook.deliver", kind="client", **{"webhook.event_type": d.event_type, "webhook.attempt": d.attempt}) as sp:
                    ok, status, error, permanent = self._send(sub, d)
                    sp.set_attribute("http.response.status_code", status or 0)
                    if not ok:
                        sp.set_error("delivery failed")
                result = self._finish(s, d, ok=ok, status=status, error=error, now=self.clock(), worker=self.id, permanent=permanent)
                s.commit()
                telemetry.count("orchivio.webhook.deliveries", outcome=result)
                telemetry.applog(sub.application_id, logging.INFO if result == "delivered" else logging.ERROR if result == "dead" else logging.WARNING,
                                 "webhook", f"webhook delivery {result}", webhook=sub.name, event_type=d.event_type, outcome=result,
                                 status=status or 0, attempt=d.attempt)
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
            resp = self.transport.post(sub.url, telemetry.inject(headers), body)
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
