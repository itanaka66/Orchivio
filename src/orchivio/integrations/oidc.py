"""OpenID Connect relying-party logic for end-user sign-in (authorization code flow with PKCE).

What is verified, and what deliberately is not supported:

* Discovery: the document's ``issuer`` must equal the configured issuer exactly, and every endpoint must pass the same
  SSRF checks as any outbound destination (all fetches go through the pinned transport).
* ID token: signature (RS256 or ES256 only; ``none`` and HMAC algorithms are refused), key by ``kid`` from the issuer's
  JWKS (refreshed at most once per five minutes when a key is unknown), ``iss``, ``aud`` (and ``azp`` when several
  audiences), ``exp``/``nbf``/``iat`` with a small leeway, and the ``nonce`` bound to this sign-in.
* Not used: the userinfo endpoint, access tokens, refresh tokens, implicit and hybrid flows, request objects.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

from .rest import RequestTransport
from .ssrf import DestinationError

LEEWAY_SECONDS = 60
CACHE_SECONDS = 3600
JWKS_REFRESH_SECONDS = 300
MAX_DOC_BYTES = 256 * 1024
ALGORITHMS = ("RS256", "ES256")


class OidcError(Exception):
    """A sign-in problem. ``code`` is stable and safe to show; the message never contains provider-supplied text."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


@dataclass(frozen=True)
class Metadata:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(text: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (ValueError, TypeError) as e:
        raise OidcError("invalid_token", "the ID token is malformed") from e


def pkce_pair() -> tuple[str, str]:
    """A fresh code verifier and its S256 challenge."""
    verifier = b64url(secrets.token_bytes(48))
    return verifier, b64url(hashlib.sha256(verifier.encode("ascii")).digest())


def authorization_url(meta: Metadata, *, client_id: str, redirect_uri: str, scopes: str, state: str, nonce: str, challenge: str) -> str:
    q = urlencode({"response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri, "scope": scopes, "state": state,
                   "nonce": nonce, "code_challenge": challenge, "code_challenge_method": "S256"})
    return f"{meta.authorization_endpoint}{'&' if '?' in meta.authorization_endpoint else '?'}{q}"


class OidcClient:
    def __init__(self, transport: RequestTransport, *, check_endpoint: Callable[[str], None] | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.transport, self.clock = transport, clock
        self.check_endpoint = check_endpoint or (lambda url: None)
        self._meta: dict[str, tuple[float, Metadata]] = {}
        self._keys: dict[str, tuple[float, float, list[dict[str, Any]]]] = {}  # uri -> (fetched, last forced refresh, keys)

    # -- fetching -------------------------------------------------------------------------------

    def _get_json(self, url: str, what: str) -> dict[str, Any]:
        try:
            resp = self.transport.request("GET", url, {"Accept": "application/json", "User-Agent": "Orchivio-OIDC/1"}, None, max_bytes=MAX_DOC_BYTES)
        except DestinationError as e:
            raise OidcError("provider_unreachable", f"the identity provider's {what} was refused: {e}") from e
        except OSError as e:
            raise OidcError("provider_unreachable", f"the identity provider's {what} could not be reached ({type(e).__name__})") from e
        if resp.status != 200 or len(resp.body) >= MAX_DOC_BYTES:
            raise OidcError("provider_error", f"the identity provider's {what} answered HTTP {resp.status}")
        try:
            doc = json.loads(resp.body)
        except ValueError as e:
            raise OidcError("provider_error", f"the identity provider's {what} is not JSON") from e
        if not isinstance(doc, dict):
            raise OidcError("provider_error", f"the identity provider's {what} is not an object")
        return doc

    def discover(self, issuer: str) -> Metadata:
        cached = self._meta.get(issuer)
        if cached and self.clock() - cached[0] < CACHE_SECONDS:
            return cached[1]
        doc = self._get_json(issuer.rstrip("/") + "/.well-known/openid-configuration", "discovery document")
        if doc.get("issuer") != issuer:
            raise OidcError("issuer_mismatch", "the discovery document names a different issuer than the one configured")
        endpoints = {}
        for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            value = doc.get(key)
            if not isinstance(value, str):
                raise OidcError("provider_error", f"the discovery document has no {key}")
            try:
                self.check_endpoint(value)
            except DestinationError as e:
                raise OidcError("provider_error", f"the discovery document's {key} is not acceptable: {e}") from e
            endpoints[key] = value
        meta = Metadata(issuer, **endpoints)
        self._meta[issuer] = (self.clock(), meta)
        return meta

    def _jwks(self, uri: str, force: bool = False) -> list[dict[str, Any]]:
        now = self.clock()
        cached = self._keys.get(uri)
        if cached and now - cached[0] < CACHE_SECONDS and not force:
            return cached[2]
        if cached and force and now - cached[1] < JWKS_REFRESH_SECONDS:
            return cached[2]  # an unknown kid must not make us hammer the provider
        doc = self._get_json(uri, "signing keys")
        keys = [k for k in doc.get("keys", []) if isinstance(k, dict)] if isinstance(doc.get("keys"), list) else []
        self._keys[uri] = (now, now if force else (cached[1] if cached else 0.0), keys)
        return keys

    def exchange(self, meta: Metadata, *, code: str, redirect_uri: str, client_id: str, client_secret: str | None, verifier: str) -> str:
        """Trade the authorization code for the ID token (the only part of the answer that is used)."""
        form = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri, "client_id": client_id, "code_verifier": verifier}
        if client_secret:
            form["client_secret"] = client_secret
        try:
            resp = self.transport.request("POST", meta.token_endpoint, {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json",
                                                                          "User-Agent": "Orchivio-OIDC/1"}, urlencode(form).encode(), max_bytes=MAX_DOC_BYTES)
        except DestinationError as e:
            raise OidcError("provider_unreachable", f"the token endpoint was refused: {e}") from e
        except OSError as e:
            raise OidcError("provider_unreachable", f"the token endpoint could not be reached ({type(e).__name__})") from e
        if resp.status != 200:
            raise OidcError("token_exchange_failed", f"the token endpoint answered HTTP {resp.status}")
        try:
            token = json.loads(resp.body).get("id_token")
        except (ValueError, AttributeError) as e:
            raise OidcError("token_exchange_failed", "the token endpoint's answer is not valid") from e
        if not isinstance(token, str) or token.count(".") != 2 or len(token) > 16_000:
            raise OidcError("token_exchange_failed", "the token endpoint returned no usable ID token")
        return token

    # -- verification ---------------------------------------------------------------------------

    def verify_id_token(self, token: str, meta: Metadata, *, client_id: str, nonce: str) -> dict[str, Any]:
        header_b, payload_b, sig_b = token.split(".")
        try:
            header = json.loads(b64url_decode(header_b))
            claims = json.loads(b64url_decode(payload_b))
        except ValueError as e:
            raise OidcError("invalid_token", "the ID token is malformed") from e
        if not isinstance(header, dict) or not isinstance(claims, dict):
            raise OidcError("invalid_token", "the ID token is malformed")
        alg = header.get("alg")
        if alg not in ALGORITHMS:
            raise OidcError("invalid_token", "the ID token uses an unsupported signature algorithm")
        signing_input = f"{header_b}.{payload_b}".encode("ascii")
        key = self._select_key(meta.jwks_uri, header.get("kid"), alg)
        _verify_signature(key, alg, signing_input, b64url_decode(sig_b))
        self._check_claims(claims, meta.issuer, client_id, nonce)
        return claims

    def _select_key(self, uri: str, kid: Any, alg: str) -> dict[str, Any]:
        kty = "RSA" if alg == "RS256" else "EC"
        for force in (False, True):
            keys = [k for k in self._jwks(uri, force) if k.get("kty") == kty and k.get("use") in (None, "sig")
                    and k.get("alg") in (None, alg)]
            matches = [k for k in keys if k.get("kid") == kid] if kid is not None else (keys if len(keys) == 1 else [])
            if matches:
                return matches[0]
        raise OidcError("invalid_token", "no signing key matches the ID token")

    def _check_claims(self, c: dict[str, Any], issuer: str, client_id: str, nonce: str) -> None:
        now = self.clock()
        if c.get("iss") != issuer:
            raise OidcError("invalid_token", "the ID token was issued by a different issuer")
        aud = c.get("aud")
        auds = [aud] if isinstance(aud, str) else aud if isinstance(aud, list) else []
        if client_id not in auds or (len(auds) > 1 and c.get("azp") != client_id):
            raise OidcError("invalid_token", "the ID token is not meant for this application")
        for name in ("exp", "iat"):
            if not isinstance(c.get(name), (int, float)) or isinstance(c.get(name), bool):
                raise OidcError("invalid_token", f"the ID token has no valid {name}")
        if c["exp"] <= now - LEEWAY_SECONDS:
            raise OidcError("token_expired", "the ID token has expired")
        if c["iat"] > now + LEEWAY_SECONDS or (isinstance(c.get("nbf"), (int, float)) and c["nbf"] > now + LEEWAY_SECONDS):
            raise OidcError("invalid_token", "the ID token is not valid yet")
        if not isinstance(c.get("nonce"), str) or not secrets.compare_digest(c["nonce"], nonce):
            raise OidcError("invalid_token", "the ID token does not belong to this sign-in")
        sub = c.get("sub")
        if not isinstance(sub, str) or not 1 <= len(sub) <= 255:
            raise OidcError("invalid_token", "the ID token has no valid subject")


def _verify_signature(jwk: dict[str, Any], alg: str, data: bytes, sig: bytes) -> None:
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa, utils
    except ImportError as e:  # pragma: no cover
        raise OidcError("unavailable", "OIDC sign-in needs the 'cryptography' package (pip install orchivio[delivery])") from e
    try:
        if alg == "RS256":
            n, e_ = int.from_bytes(b64url_decode(jwk["n"]), "big"), int.from_bytes(b64url_decode(jwk["e"]), "big")
            if n.bit_length() < 2048:
                raise OidcError("invalid_token", "the provider's signing key is too small")
            rsa.RSAPublicNumbers(e_, n).public_key().verify(sig, data, padding.PKCS1v15(), hashes.SHA256())
        else:
            if jwk.get("crv") != "P-256" or len(sig) != 64:
                raise OidcError("invalid_token", "the ID token's signature does not fit ES256")
            x, y = int.from_bytes(b64url_decode(jwk["x"]), "big"), int.from_bytes(b64url_decode(jwk["y"]), "big")
            der = utils.encode_dss_signature(int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big"))
            ec.EllipticCurvePublicNumbers(x, y, ec.SECP256R1()).public_key().verify(der, data, ec.ECDSA(hashes.SHA256()))
    except InvalidSignature as e:
        raise OidcError("invalid_token", "the ID token's signature is not valid") from e
    except (KeyError, ValueError, TypeError) as e:
        raise OidcError("invalid_token", "the provider's signing key is not usable") from e
