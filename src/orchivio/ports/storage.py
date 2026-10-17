"""Artifact storage port (exports, logs, build artifacts, DESIGN.md section 6.3)."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-/]{0,255}$")


class InvalidKey(ValueError):
    pass


@dataclass(frozen=True)
class StoredObject:
    key: str
    size: int
    sha256: str


class ArtifactStore(Protocol):
    def put(self, key: str, data: bytes) -> StoredObject: ...

    def get(self, key: str) -> bytes: ...

    def exists(self, key: str) -> bool: ...

    def delete(self, key: str) -> None: ...


def validate_key(key: str) -> str:
    """Keys are relative, slash-separated and cannot escape the store root."""
    parts = key.split("/")
    if not _KEY_RE.match(key) or any(p in ("", ".", "..") for p in parts):
        raise InvalidKey(f"invalid artifact key {key!r}")
    return key


class LocalArtifactStore:
    """Filesystem adapter. S3-compatible adapters implement the same protocol."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).resolve()  # created on first write

    def _path(self, key: str) -> Path:
        path = (self.root / validate_key(key)).resolve()
        if not path.is_relative_to(self.root):  # defence in depth against symlinks
            raise InvalidKey(f"invalid artifact key {key!r}")
        return path

    def put(self, key: str, data: bytes) -> StoredObject:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)  # atomic on the same filesystem
        return StoredObject(key, len(data), hashlib.sha256(data).hexdigest())

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)
