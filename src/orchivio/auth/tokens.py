"""Opaque bearer secrets: session cookies, CSRF tokens and API tokens.

Only the SHA-256 digest is stored. The tokens are 256-bit random values, so a fast hash is
appropriate (unlike passwords, they cannot be guessed or dictionary-attacked).
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

API_TOKEN_PREFIX = "orv_"


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_token(prefix: str = "") -> tuple[str, str]:
    """Return ``(plaintext, digest)``. The plaintext is shown to the user exactly once."""
    plain = prefix + secrets.token_urlsafe(32)
    return plain, hash_token(plain)


def matches(token: str, digest: str) -> bool:
    return hmac.compare_digest(hash_token(token), digest)
