"""Security consistency checker (FR-066)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from orchivio.expr import CheckEnv, ExprError, check
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

    for ai, rule in enumerate(sec.access):
        out += _field_findings(index, rule, f"/security/access/{ai}")

    writers: dict[str, set[str]] = {}
    for rule in sec.access:
        if set(rule.operations) & {"create", "update", "delete"}:
            writers.setdefault(rule.entity, set()).add(rule.role)

    for q, (_, ent) in sorted(index.entities.items()):
        if not ent.transient and not writers.get(q):
            out.append(Finding("warning", "entity-without-writers", "/security/access",
                               f"no role can write {q} (create, update or delete)"))

    for ai, rule in enumerate(sec.access):
        if "export" in rule.operations and "read" not in rule.operations:
            out.append(Finding("warning", "export-without-read", f"/security/access/{ai}",
                               f"role '{rule.role}' may export {rule.entity} but cannot read it; "
                               "export implies read access to all rows"))

    if sec.anonymous:
        out.append(Finding("warning", "anonymous-access", "/security/anonymous", "anonymous access is enabled; it requires A3 approval and exposes pages without authentication"))

    # Form page writability: a form page whose roles can't perform the required operation always fails
    for mod in model.modules:
        for pg in mod.pages:
            if pg.type != "form" or pg.mode is None:
                continue
            entity = index.resolve_entity(mod.key, pg.entity) or qualify(mod.key, pg.entity)
            allowed_roles = set(sec.pages.get(pg.key, []))
            if not allowed_roles:
                continue  # unreachable-page already fires
            required_op = "create" if pg.mode == "create" else "update"
            entity_writers = {r.role for r in sec.access if r.entity == entity and required_op in r.operations}
            if not (allowed_roles & entity_writers):
                out.append(Finding("warning", f"form-page-no-{required_op}",
                                   f"/security/pages/{pg.key}",
                                   f"form page '{pg.key}' (mode={pg.mode}) is accessible to "
                                   f"{sorted(allowed_roles)} but none of them can {required_op} {entity}"))

    return out


def _field_findings(index: ModelIndex, rule, path: str) -> list[Finding]:
    """Field-level traps (FR-064): a rule can be consistent on its own and still leak or block."""
    found = index.entities.get(rule.entity)
    if found is None or rule.fields is None:
        return []
    mod, ent = found
    out: list[Finding] = []
    read, write = rule.fields.read, rule.fields.write
    if read is not None:
        env = CheckEnv(index.types_for(mod.key), rule.entity)
        for f in ent.fields:
            if f.computed is None or f.name not in read:
                continue
            try:
                deps = check(f.computed.expr, env).fields
            except ExprError:
                continue
            hidden = sorted(d for d in deps if d not in read and d != f.name)
            if hidden:
                out.append(Finding("warning", "computed-field-leak", f"{path}/fields/read",
                                   f"computed field '{f.name}' is readable by '{rule.role}' but is derived from "
                                   f"{', '.join(hidden)}, which that role cannot read; its value reveals them"))
    if write is not None and "create" in rule.operations:
        for f in ent.fields:
            if (f.required and not f.generated and f.computed is None and f.default is None and f.default_expr is None
                    and f.name not in write):
                out.append(Finding("warning", "required-field-not-writable", f"{path}/fields/write",
                                   f"'{rule.role}' may create {rule.entity} but cannot write required field '{f.name}' "
                                   "(no default), so creating always fails"))
    return out
