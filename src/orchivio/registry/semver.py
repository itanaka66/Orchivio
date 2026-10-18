"""Semantic versions and the few range forms the registry understands (semver.org 2.0.0, no build metadata).

``1.2.3``  exactly that version (pre-releases only this way)      ``^1.2.3``  >=1.2.3 <2.0.0 (``^0.2.3``: <0.3.0, ``^0.0.3``: exactly)
``~1.2.3`` >=1.2.3 <1.3.0                                         ``>=1.2.3`` that or newer       ``*``/``latest``  the newest release
Pre-releases (``1.0.0-beta.1``) are never picked by a range, only named explicitly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import total_ordering

_VERSION = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$")


class VersionError(ValueError):
    pass


@total_ordering
@dataclass(frozen=True)
class Version:
    major: int
    minor: int
    patch: int
    pre: tuple[str, ...] = ()

    @classmethod
    def parse(cls, text: str) -> Version:
        m = _VERSION.match(text.strip()) if isinstance(text, str) and len(text) <= 64 else None
        if not m:
            raise VersionError(f"{text!r} is not a semantic version such as 1.2.3 or 1.2.3-beta.1")
        pre = tuple(m.group(4).split(".")) if m.group(4) else ()
        if any(p.isdigit() and len(p) > 1 and p.startswith("0") for p in pre):
            raise VersionError("numeric pre-release identifiers must not have leading zeros")
        return cls(int(m.group(1)), int(m.group(2)), int(m.group(3)), pre)

    @property
    def is_prerelease(self) -> bool:
        return bool(self.pre)

    def _key(self) -> tuple:
        # a release sorts after its own pre-releases; numeric identifiers sort before alphanumeric ones
        ident = tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in self.pre)
        return (self.major, self.minor, self.patch, 1 if not self.pre else 0, ident)

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, Version):
            return NotImplemented
        return self._key() < other._key()

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}" + (f"-{'.'.join(self.pre)}" if self.pre else "")


def satisfies(version: Version, spec: str) -> bool:
    """Does ``version`` match ``spec``? Raises ``VersionError`` for a spec it does not understand."""
    spec = spec.strip()
    if spec in ("*", "latest", ""):
        return not version.is_prerelease
    if spec[0] in "^~" or spec.startswith(">="):
        if version.is_prerelease:
            return False
        base = Version.parse(spec[2:] if spec.startswith(">=") else spec[1:])
        if version < base:
            return False
        if spec.startswith(">="):
            return True
        if spec[0] == "~":
            return (version.major, version.minor) == (base.major, base.minor)
        if base.major > 0:
            return version.major == base.major
        if base.minor > 0:
            return (version.major, version.minor) == (0, base.minor)
        return version == base
    return version == Version.parse(spec)


def resolve(spec: str, available: list[str]) -> str | None:
    """The highest of ``available`` that satisfies ``spec``, or None."""
    best: Version | None = None
    for text in available:
        try:
            v = Version.parse(text)
        except VersionError:
            continue
        if satisfies(v, spec) and (best is None or v > best):
            best = v
    return str(best) if best else None
