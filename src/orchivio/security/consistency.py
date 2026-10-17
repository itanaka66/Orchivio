"""Security consistency checker (FR-066)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from orchivio.model import AppModel, ModelIndex
from orchivio.model.schema import qualify


@dataclass(frozen=True)
class Finding:
    severity: Literal["error", "warning"]
    code: str
    path: str
    message: str


def check_consistency(model: AppModel) -> list[Finding]:
    out: list[Finding] = []
    sec = model.security
    index = ModelIndex(model)

    readers: dict[str, set[str]] = {}
    for rule in sec.access:
        if "read" in rule.operations:
            readers.setdefault(rule.entity, set()).add(rule.role)

    page_entity = {}
    for mod in model.modules:
        for pg in mod.pages:
            page_entity[pg.key] = (index.resolve_entity(mod.key, pg.entity) or qualify(mod.key, pg.entity))

    for key, entity in sorted(page_entity.items()):
        allowed = sec.pages.get(key, [])
        if not allowed:
            out.append(Finding("warning", "unreachable-page", f"/security/pages/{key}", f"page '{key}' lists no roles and is unreachable (deny by default)"))
        for role in allowed:
            if role not in readers.get(entity, set()):
                out.append(Finding("error", "page-exposes-unreadable-entity", f"/security/pages/{key}", f"role '{role}' can open page '{key}' but cannot read {entity}"))

    used = {r.role for r in sec.access} | {r for rs in sec.pages.values() for r in rs}
    for role in sec.roles:
        if role not in used:
            out.append(Finding("warning", "role-without-access", "/security/roles", f"role '{role}' has no access rules and no pages"))

    for q, (_, ent) in sorted(index.entities.items()):
        if not ent.transient and not readers.get(q):
            out.append(Finding("warning", "entity-without-readers", "/security/access", f"no role can read {q}"))

    if sec.anonymous:
        out.append(Finding("warning", "anonymous-access", "/security/anonymous", "anonymous access is enabled; it requires A3 approval and exposes pages without authentication"))
    return out
