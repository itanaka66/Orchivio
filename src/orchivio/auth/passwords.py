"""Password hashing with scrypt from the standard library (no extra dependency).

Stored format: ``scrypt$<n>$<r>$<p>$<salt_b64>$<hash_b64>``. Parameters are stored with the
hash so they can be raised later; ``needs_rehash`` tells callers when to upgrade on login.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

N, R, P = 2**14, 8, 1
_DKLEN = 32
_SALT_LEN = 16
MIN_LENGTH = 12
MAX_LENGTH = 128  # bounds hashing cost for oversized inputs


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def _derive(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=_DKLEN, maxmem=128 * n * r * 2)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(_SALT_LEN)
    return f"scrypt${N}${R}${P}${_b64(salt)}${_b64(_derive(password, salt, N, R, P))}"


def verify_password(password: str, stored: str | None) -> bool:
    """Constant-time check. Malformed or missing hashes verify as False."""
    if not stored or len(password) > MAX_LENGTH:
        return False
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest)
        actual = _derive(password, base64.b64decode(salt), int(n), int(r), int(p))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def needs_rehash(stored: str) -> bool:
    try:
        _, n, r, p, *_ = stored.split("$")
        return (int(n), int(r), int(p)) != (N, R, P)
    except ValueError:
        return True


# Verified against when the account does not exist, so response time doesn't reveal that.
DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


def password_problem(password: str) -> str | None:
    """Return why a new password is unacceptable, or ``None``."""
    if len(password) < MIN_LENGTH:
        return f"password must be at least {MIN_LENGTH} characters"
    if len(password) > MAX_LENGTH:
        return f"password must be at most {MAX_LENGTH} characters"
    if len(set(password)) < 4:
        return "password is too repetitive"
    return None
