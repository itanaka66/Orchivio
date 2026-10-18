"""Idempotency keys for creating things (docs/OFFLINE.md).

A client that queues a request while offline (the Studio's outbox) cannot know whether a request that died on the wire was processed. With an
``Idempotency-Key`` header on a POST to a route that uses ``IdempotentRoute``, retrying the same request is safe:

* the first request runs normally; if it succeeds (2xx) its status and body are stored for 24 hours, per signed-in user and key;
* the same key with the same request returns the stored answer (``Idempotency-Replayed: true``) without doing anything again;
* the same key with a different method, path or body is refused (``422 idempotency_key_reuse``): a key names one request;
* the same key while the first request is still running is refused (``409 request_in_progress``, retry in a second);
* failures (4xx/5xx) are not stored, so a corrected request with the same key may be sent.

The key is scoped to the authenticated user (session or API token, with the CSRF check) and stored with a hash of the request, never the request itself.
Requests without the header are untouched. Only POST routes of routers declared with ``route_class=IdempotentRoute`` take part.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from fastapi import HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.routing import APIRoute
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from orchivio.db.models import IdempotencyKey

from .deps import api_error, current_principal, utcnow

KEY = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
TTL = timedelta(hours=24)
MAX_STORED = 256 * 1024


def _error(status: int, code: str, message: str, headers: dict[str, str] | None = None) -> Response:
    e = api_error(status, code, message, headers=headers)
    from fastapi.responses import JSONResponse

    return JSONResponse(e.detail, status_code=status, headers=headers)


class IdempotentRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Any]:
        original = super().get_route_handler()

        async def handler(request: Request) -> Response:
            key = request.headers.get("idempotency-key")
            if key is None or request.method != "POST":
                return await original(request)
            if not KEY.match(key):
                return _error(422, "invalid_idempotency_key", "an Idempotency-Key is 8-64 letters, digits, - or _")
            body = await request.body()
            digest = hashlib.sha256(f"{request.method}\n{request.url.path}\n".encode() + body).hexdigest()

            def begin() -> tuple[str, Response | None]:
                sf = request.app.state.session_factory
                with sf() as db:
                    try:
                        user_id = current_principal(request, db).user_id
                    except HTTPException:
                        return "pass", None  # not signed in: the route itself answers 401/403
                    db.execute(delete(IdempotencyKey).where(IdempotencyKey.user_id == user_id, IdempotencyKey.created_at < utcnow() - TTL))
                    row = db.scalar(select(IdempotencyKey).where(IdempotencyKey.user_id == user_id, IdempotencyKey.key == key))
                    if row is not None:
                        db.commit()
                        if row.request_hash != digest:
                            return "answer", _error(422, "idempotency_key_reuse", "this Idempotency-Key was used for a different request")
                        if row.state != "done":
                            return "answer", _error(409, "request_in_progress", "the first request with this key is still running; retry in a moment", {"Retry-After": "1"})
                        return "answer", Response(row.response_body or "", status_code=row.status_code or 200, media_type=row.content_type or "application/json",
                                                  headers={"Idempotency-Replayed": "true"})
                    db.add(IdempotencyKey(user_id=user_id, key=key, request_hash=digest, state="running"))
                    try:
                        db.commit()
                    except IntegrityError:  # a concurrent request with the same key won the insert
                        db.rollback()
                        return "answer", _error(409, "request_in_progress", "the first request with this key is still running; retry in a moment", {"Retry-After": "1"})
                    return f"run:{user_id}", None

            action, early = await run_in_threadpool(begin)
            if early is not None:
                return early
            if action == "pass":
                return await original(request)
            user_id = action.split(":", 1)[1]

            def finish(response: Response | None) -> None:
                import uuid

                with request.app.state.session_factory() as db:
                    row = db.scalar(select(IdempotencyKey).where(IdempotencyKey.user_id == uuid.UUID(user_id), IdempotencyKey.key == key))
                    if row is None:
                        return
                    stored = response is not None and 200 <= response.status_code < 300 and len(getattr(response, "body", b"") or b"") <= MAX_STORED
                    if stored:
                        row.state, row.status_code = "done", response.status_code  # type: ignore[union-attr]
                        row.response_body, row.content_type = bytes(response.body).decode("utf-8"), response.media_type or "application/json"  # type: ignore[union-attr]
                    else:
                        db.delete(row)  # not replayable: let the client send a corrected request with the same key
                    db.commit()

            try:
                response = await original(request)
            except BaseException:
                await run_in_threadpool(finish, None)
                raise
            await run_in_threadpool(finish, response)
            return response

        return handler


__all__ = ["IdempotentRoute"]
