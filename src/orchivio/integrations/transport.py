"""Signing and the pinned HTTP transport for outbound webhooks."""

from __future__ import annotations

import hashlib
import hmac
import http.client
import socket
import ssl
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Protocol

from .ssrf import (
    DEFAULT_POLICY,
    DestinationError,
    Policy,
    Resolver,
    check_url,
    resolve_public,
    system_resolver,
)

MAX_RESPONSE_BYTES = 64 * 1024
TOLERANCE_SECONDS = 300


def sign(secret: str, body: bytes, timestamp: int | None = None) -> str:
    """``t=<unix>,v1=<hex HMAC-SHA256 of "<t>.<body>">`` (the timestamp is signed, which limits replay)."""
    t = int(time.time()) if timestamp is None else timestamp
    mac = hmac.new(secret.encode("utf-8"), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={t},v1={mac}"


def verify_signature(secret: str, header: str, body: bytes, *, tolerance: int = TOLERANCE_SECONDS, now: int | None = None) -> bool:
    """For receivers: constant-time check of ``sign`` output, rejecting stale timestamps."""
    try:
        fields = dict(part.split("=", 1) for part in header.split(","))
        t = int(fields["t"])
        given = fields["v1"]
    except (ValueError, KeyError):
        return False
    if abs((int(time.time()) if now is None else now) - t) > tolerance:
        return False
    expected = hmac.new(secret.encode("utf-8"), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, given)


@dataclass(frozen=True)
class Response:
    status: int
    body: bytes = b""
    content_type: str = ""
    headers: Mapping[str, str] = field(default_factory=dict)  # lower-cased names


class Transport(Protocol):
    def post(self, url: str, headers: dict[str, str], body: bytes) -> Response:
        """Send; raises ``DestinationError`` when the destination is refused and ``OSError`` on network failure."""
        ...


class _Pinned(http.client.HTTPSConnection):
    """Connects to a fixed IP while keeping the real host name for SNI, certificate checks and Host."""

    def __init__(self, host: str, port: int, ip: str, timeout: float, context: ssl.SSLContext | None) -> None:
        super().__init__(host, port, timeout=timeout, context=context)
        self._ip = ip

    def connect(self) -> None:
        sock = socket.create_connection((self._ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)  # type: ignore[attr-defined]


class _PinnedHttp(http.client.HTTPConnection):
    def __init__(self, host: str, port: int, ip: str, timeout: float) -> None:
        super().__init__(host, port, timeout=timeout)
        self._ip = ip

    def connect(self) -> None:
        self.sock = socket.create_connection((self._ip, self.port), self.timeout)


class PinnedTransport:
    """POSTs after re-checking the destination at send time; follows no redirects."""

    def __init__(self, policy: Policy = DEFAULT_POLICY, *, timeout: float = 10.0, resolver: Resolver = system_resolver,
                 tls: Callable[[], ssl.SSLContext] = ssl.create_default_context) -> None:
        self.policy, self.timeout, self.resolver, self.tls = policy, timeout, resolver, tls

    def post(self, url: str, headers: dict[str, str], body: bytes) -> Response:
        return self.request("POST", url, headers, body)

    def request(self, method: str, url: str, headers: dict[str, str], body: bytes | None = None, *,
                timeout: float | None = None, max_bytes: int = MAX_RESPONSE_BYTES) -> Response:
        dest = check_url(url, self.policy)
        last: OSError | None = None
        for ip in resolve_public(dest, self.policy, self.resolver):
            wait = timeout or self.timeout
            conn: http.client.HTTPConnection = (
                _Pinned(dest.host, dest.port, ip, wait, self.tls()) if dest.scheme == "https"
                else _PinnedHttp(dest.host, dest.port, ip, wait))
            try:
                send = {**headers, **({"Content-Length": str(len(body))} if body is not None else {})}
                conn.request(method, dest.target, body=body, headers=send)
                resp = conn.getresponse()
                return Response(resp.status, resp.read(max_bytes), resp.getheader("Content-Type") or "",
                                {k.lower(): v for k, v in resp.getheaders()})
            except (OSError, http.client.HTTPException) as e:
                last = e if isinstance(e, OSError) else OSError(str(e))
            finally:
                conn.close()
        raise last or DestinationError("no address could be reached")
