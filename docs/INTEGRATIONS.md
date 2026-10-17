# Integrations: business events and outbound webhooks

Covers FR-083 (transactional outbox) and FR-082 (outbound and inbound webhooks). Not built: consumed REST / OpenAPI
import (FR-081), broker adapters.

## Business events

Every committed create, update and delete of a record in a generated application produces an event
`<module>.<Entity>.created|updated|deleted` (whoever caused it: the API, a flow, a hook, a scheduled job).

* **Transactional outbox.** The event row is written to `orchivio_outbox` in the application's runtime database *in
  the same transaction as the data change*. A rejected write, or a flow that rolls back, produces no event; a
  committed change cannot lose its event. Resetting the preview discards its events.
* **Relay.** The worker (`orchivio worker`, or `ORCHIVIO_WORKER_ENABLED=true`) copies new outbox rows into
  `webhook_deliveries` (platform database), one per matching subscription, then marks them relayed. A crash between the
  two repeats the relay; a unique `(subscription, event)` key makes that harmless. Relayed rows are purged after 7 days.
* **At-least-once, unordered.** A delivery can be sent more than once; receivers deduplicate on the event id.

## Subscriptions

Managed by admins (`integration.manage`): `POST /applications/{id}/webhooks` with

| Field | Meaning |
|---|---|
| `url` | HTTPS destination (see SSRF below). Treated as sensitive: never written to the audit log |
| `secret_ref` | `env:NAME` or `file:/path` on the server holding the signing secret. The secret itself is never stored |
| `events` | glob patterns over event types: `core.Product.created`, `core.Product.*`, `core.*.updated`, `*` |
| `fields` | `null` = ids only (the receiver learns that something happened); `["name","sku"]` = those record fields; `["*"]` = the whole record |

The default (`null`) is deliberately minimal: a webhook leaves the platform, so record content is opt-in per field.
Field-level security (docs/APP-SECURITY.md) governs the API; events are system-level, so choose `fields` as you would
for any export.

## What is sent

`POST` with a CloudEvents 1.0 structured JSON body (`application/cloudevents+json`):

```json
{"specversion":"1.0","id":"<event id>","source":"orchivio:app:<application id>","type":"core.Product.updated",
 "time":"2026-05-01T12:00:00+00:00","subject":"core.Product/<id>","datacontenttype":"application/json",
 "data":{"entity":"core.Product","id":"<id>","record":{...},"changed":["quantity"]}}
```

Headers: `Orchivio-Signature: t=<unix>,v1=<hex>`, `Orchivio-Event-Id`, `Orchivio-Event-Type`, `Orchivio-Delivery-Id`,
`Orchivio-Attempt`. The signature is HMAC-SHA256 over `"<t>.<raw body>"` with the secret; verify it on the exact
bytes received, compare in constant time and reject timestamps older than a few minutes
(`orchivio.integrations.verify_signature` does all three). Any 2xx counts as delivered; redirects are not followed.

## Retries, dead letters, circuit breaker

* HTTP 5xx, 408, 425, 429, timeouts and network errors retry with exponential backoff
  (`ORCHIVIO_WEBHOOK_BACKOFF_SECONDS`, default 30 s, doubling, at most 6 h) up to `ORCHIVIO_WEBHOOK_MAX_ATTEMPTS`
  (default 6). Other 4xx, and refused destinations, are permanent failures.
* A delivery out of attempts is **dead** (`webhook.delivery_dead` audit event); `POST /webhook-deliveries/{id}/retry`
  gives it a fresh set of attempts.
* After `ORCHIVIO_WEBHOOK_PAUSE_AFTER_FAILURES` (default 10) consecutive failed attempts the subscription is
  **paused** (`webhook.paused`) and receives nothing new until an admin sets `status: active`.
* `POST /webhooks/{id}/test` queues one `orchivio.ping` (a single attempt).

## SSRF protection

Checked when a subscription is saved (static) and again on every send (resolved):

* https only (plain http only with `ORCHIVIO_WEBHOOK_ALLOW_HTTP`, for development); no credentials in the URL; no
  numeric or non-canonical host forms (`0x7f.0.0.1`, `2130706433`); no `localhost`, `.local`, `.internal`, single-label
  names.
* At send time the name is resolved and **every** answer must be publicly routable (not private, loopback,
  link-local such as the cloud metadata address, CGNAT, multicast, reserved, IPv4-mapped or NAT64/6to4/Teredo forms of
  those). One bad answer rejects the name. The connection is then made to that resolved address with the original
  host name for TLS and `Host`, so DNS cannot change between check and use (rebinding).
* `ORCHIVIO_WEBHOOK_ALLOWED_HOSTS` (fnmatch patterns) restricts destinations to an allowlist; empty means any public
  host. `ORCHIVIO_WEBHOOK_ALLOW_PRIVATE_NETWORKS` disables the address check and exists for local development only.
* No redirects, 10 s timeout (`ORCHIVIO_WEBHOOK_TIMEOUT_SECONDS`), response bodies capped at 64 KiB and not stored.

## Inbound webhooks

An admin (`integration.manage`) registers an endpoint that starts a server flow:
`POST /applications/{id}/inbound-webhooks` with `key`, `flow`, `run_as_role`, `secret_ref` (`env:`/`file:`),
`arg_map` (flow parameter -> RFC 6901 JSON pointer into the payload, e.g. `/data/id`) and optional `defaults`
(parameter -> literal, used when the pointer finds nothing). Every flow parameter needs a pointer or a default; roles,
types and pointers are checked when the webhook is saved. The sender posts JSON to the returned
`url_path` (`/api/v1/hooks/{application id}/{key}`):

* **Authentication is the signature.** Same scheme as outbound deliveries: header
  `Orchivio-Signature: t=<unix>,v1=<hex HMAC-SHA256 of "<t>.<raw body>">` (`orchivio.integrations.sign` builds it),
  timestamp within 5 minutes. A missing, wrong or stale signature, and a server that cannot read the secret, all answer
  the same `401 invalid_signature`; unknown, paused or foreign endpoints answer `404`. There is no session, cookie or
  token on this route.
* **Replay protection.** The event id is the `Webhook-Id` header if present, else the signature value. A second
  request with the same id is acknowledged (`200`-class `202` with `duplicate: true`) and runs nothing. Receipts are
  kept 7 days (`GET /inbound-webhooks/{id}/events`).
* **Execution.** An accepted request is queued as a job (trigger `webhook`, up to 3 attempts), so the flow runs
  asynchronously with the usual retries and dead-letter handling, as the system identity `system:webhook:<flow>` holding
  only `run_as_role`. The response is `202 {accepted, duplicate, job_id}`; follow the job under
  `/applications/{id}/jobs`.
* **Payload checks happen after authentication** and never echo values: a pointer that finds nothing or a value of the
  wrong type is `422 invalid_payload` naming the parameter; non-JSON is `400`; bodies over 256 KiB are `413`
  (`ORCHIVIO_INBOUND_MAX_BODY_BYTES`); more than 60 requests a minute per webhook are `429`
  (`ORCHIVIO_INBOUND_RATE_PER_MINUTE`, per process). Rejected requests are not recorded, so a corrected resend works.
* Accepted requests are audited (`inbound_webhook.accepted`, application plane) without payload or secret.

## Not built

OpenAPI import and consumed integrations (FR-081), a
model-level `emit_event` step, broker adapters, per-environment allowlists (the allowlist is per installation), secret
resolution beyond `env:`/`file:`, and a Studio UI.
