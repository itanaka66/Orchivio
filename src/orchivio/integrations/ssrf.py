"""Destination checks for outbound requests (SSRF protection, DESIGN.md §14).

Two stages. ``check_url`` is static (scheme, credentials, literal addresses, allowlist) and runs when a
subscription is saved. ``resolve_public`` runs at send time: every address the name resolves to must be publicly
routable, and the connection is then made to *that address* (see ``transport``), so a DNS answer cannot change
between the check and the connection (rebinding).
"""

from __future__ import annotations

import fnmatch
import ipaddress
import re
import socket
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

Resolver = Callable[[str, int], list[str]]


class DestinationError(Exception):
    pass


@dataclass(frozen=True)
class Policy:
    allow_http: bool = False  # plain http, for development only
    allow_private: bool = False  # private / loopback / link-local targets, for development only
    allowed_hosts: tuple[str, ...] = ()  # fnmatch patterns; empty = any public host


@dataclass(frozen=True)
class Destination:
    scheme: str
    host: str
    port: int
    target: str  # path + query

    @property
    def literal_ip(self) -> str | None:
        try:
            return str(ipaddress.ip_address(self.host))
        except ValueError:
            return None


_BLOCKED_V6 = tuple(ipaddress.ip_network(n) for n in ("64:ff9b::/96", "64:ff9b:1::/48", "2002::/16", "2001::/32", "::ffff:0:0/96"))
_BLOCKED_NAMES = ("localhost", ".localhost", ".local", ".internal", ".localdomain", ".home.arpa")


def is_public(address: str) -> bool:
    """Publicly routable unicast address. IPv4-mapped/embedded forms are judged by the IPv4 inside."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return is_public(str(ip.ipv4_mapped))
        if any(ip in n for n in _BLOCKED_V6):  # NAT64, 6to4, Teredo can smuggle an IPv4 address
            return False
    return ip.is_global and not (ip.is_multicast or ip.is_unspecified or ip.is_reserved or ip.is_loopback or ip.is_link_local)


DEFAULT_POLICY = Policy()


def check_url(url: str, policy: Policy = DEFAULT_POLICY) -> Destination:
    if len(url) > 2000 or any(c in url for c in "\r\n\t "):
        raise DestinationError("the URL is malformed")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as e:
        raise DestinationError("the URL is malformed") from e
    scheme = parts.scheme.lower()
    if scheme not in ("https", "http") or (scheme == "http" and not policy.allow_http):
        raise DestinationError("only https URLs are allowed")
    if parts.username is not None or parts.password is not None or "@" in (parts.netloc or ""):
        raise DestinationError("credentials in the URL are not allowed; sign requests with the secret instead")
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        raise DestinationError("the URL has no host")
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError as e:
        raise DestinationError("the host name is invalid") from e
    port = port or (443 if scheme == "https" else 80)
    dest = Destination(scheme, host, port, (parts.path or "/") + (f"?{parts.query}" if parts.query else ""))
    if dest.literal_ip is None and all(re.fullmatch(r"0x[0-9a-f]+|[0-9]+", label) for label in host.split(".")):
        raise DestinationError("numeric host names (octal, hex or shortened addresses) are not allowed")
    if not policy.allow_private:
        if dest.literal_ip is not None:
            if not is_public(dest.literal_ip):
                raise DestinationError("private, loopback and link-local addresses are not allowed")
        elif host in ("localhost",) or host.endswith(_BLOCKED_NAMES) or "." not in host:
            raise DestinationError("internal host names are not allowed")
    if policy.allowed_hosts and not any(fnmatch.fnmatchcase(host, p.lower()) for p in policy.allowed_hosts):
        raise DestinationError("this host is not on the destination allowlist")
    return dest


def system_resolver(host: str, port: int) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise DestinationError(f"cannot resolve {host}") from e
    seen: list[str] = []
    for info in infos:
        addr = info[4][0]
        if addr not in seen:
            seen.append(addr)
    return seen


def resolve_public(dest: Destination, policy: Policy = DEFAULT_POLICY, resolver: Resolver = system_resolver) -> list[str]:
    """Addresses to connect to. One non-public answer rejects the whole name (no 'pick the good one')."""
    addresses = [dest.literal_ip] if dest.literal_ip else resolver(dest.host, dest.port)
    if not addresses:
        raise DestinationError(f"cannot resolve {dest.host}")
    if not policy.allow_private:
        bad = [a for a in addresses if not is_public(a)]
        if bad:
            raise DestinationError(f"{dest.host} resolves to a non-public address")
    return addresses
