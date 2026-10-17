"""Model schema migrations. Schema N-1 must load in runtime N (DESIGN.md section 5.1)."""

from __future__ import annotations

import copy
from typing import Any

from .schema import OPERATIONS, SCHEMA_VERSION


class UnsupportedSchemaVersion(ValueError):
    pass


def _migrate_1_0(raw: dict[str, Any]) -> dict[str, Any]:
    """1.0 (flat entities/pages with per-entity permissions) -> 1.1 (modules + security)."""
    data = copy.deepcopy(raw)
    entities = data.pop("entities", [])
    pages = data.pop("pages", [])
    roles: set[str] = set()
    rights: dict[tuple[str, str], list[str]] = {}
    entity_readers: dict[str, set[str]] = {}

    for ent in entities:
        perms = ent.pop("permissions", {}) or {}
        for op in OPERATIONS:
            for role in perms.get(op, []):
                roles.add(role)
                rights.setdefault((ent["name"], role), []).append(op)
                if op == "read":
                    entity_readers.setdefault(ent["name"], set()).add(role)

    access = [
        {"entity": f"core.{entity}", "role": role, "operations": ops}
        for (entity, role), ops in sorted(rights.items())
    ]
    # 1.0 had no page-level access: grant each page to the roles that can read its entity.
    page_roles = {
        p["key"]: sorted(entity_readers.get(p["entity"], set())) for p in pages if "key" in p
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "application": data["application"],
        "modules": [{"key": "core", "entities": entities, "pages": pages}],
        "security": {"roles": sorted(roles), "access": access, "pages": page_roles},
    }


def migrate_to_latest(raw: dict[str, Any]) -> dict[str, Any]:
    """Return a 1.1 model dict. The input is not modified."""
    version = str(raw.get("schema_version", ""))
    if version == SCHEMA_VERSION:
        return copy.deepcopy(raw)
    if version == "1.0":
        return _migrate_1_0(raw)
    raise UnsupportedSchemaVersion(f"unsupported schema_version {version!r}")
