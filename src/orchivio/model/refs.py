"""Dependency graph between model elements (FR-122, find usages / impact analysis)."""

from __future__ import annotations

from dataclasses import dataclass

from orchivio.expr import CheckEnv, ExprError, check

from .schema import AppModel, parse_field_type, qualify
from .validate import ModelIndex


@dataclass(frozen=True)
class Ref:
    from_id: str
    to_id: str
    kind: str


def build_refs(model: AppModel) -> list[Ref]:
    """All references between identified elements. Requires ``element_id`` on every element.

    Expressions that fail to type-check contribute no edges (validation reports them).
    """
    index = ModelIndex(model)
    ent_id = {q: ent.element_id for q, (_, ent) in index.entities.items()}
    enum_id = {
        f"{m.key}.{en.name}": en.element_id for m in model.modules for en in m.enumerations
    }
    field_id = {
        (q, f.name): f.element_id for q, (_, ent) in index.entities.items() for f in ent.fields
    }
    refs: set[Ref] = set()

    def add(src: str | None, dst: str | None, kind: str) -> None:
        if src and dst:
            refs.add(Ref(src, dst, kind))

    def expr_refs(src: str | None, source: str, module_key: str, entity: str | None) -> None:
        try:
            res = check(source, CheckEnv(index.types_for(module_key), entity))
        except ExprError:
            return
        for name in res.fields:
            add(src, field_id.get((entity, name)), "expr-field")
        for t in res.types.values():
            if t.kind == "enumtype" and t.name:
                add(src, enum_id.get(qualify(module_key, t.name)), "expr-enum")

    for mod in model.modules:
        for ent in mod.entities:
            q = f"{mod.key}.{ent.name}"
            for f in ent.fields:
                parsed = parse_field_type(f.type)
                if parsed and parsed[0] == "enum":
                    add(f.element_id, enum_id.get(qualify(mod.key, parsed[1] or "")), "field-enum")
                if parsed and parsed[0] == "ref":
                    target = index.resolve_entity(mod.key, parsed[1] or "")
                    add(f.element_id, ent_id.get(target or ""), "field-entity")
                if f.computed:
                    expr_refs(f.element_id, f.computed.expr, mod.key, q)
                if f.default_expr:
                    expr_refs(f.element_id, f.default_expr, mod.key, None)
        for pg in mod.pages:
            target = index.resolve_entity(mod.key, pg.entity)
            add(pg.element_id, ent_id.get(target or ""), "page-entity")
            for col in pg.columns:
                add(pg.element_id, field_id.get((target, col)), "page-column")
            if target:
                for act in pg.actions:
                    if not isinstance(act, str):
                        for src in (act.visible_expr, act.editable_expr):
                            if src:
                                expr_refs(pg.element_id, src, target.split(".")[0], target)
    for rule in model.security.access:
        add(rule.element_id, ent_id.get(rule.entity), "access-entity")
        found = index.entities.get(rule.entity)
        if found and rule.row_constraint:
            expr_refs(rule.element_id, rule.row_constraint, found[0].key, rule.entity)
    return sorted(refs, key=lambda r: (r.from_id, r.to_id, r.kind))


def usages(refs: list[Ref], element_id: str) -> list[Ref]:
    """Inbound references: who depends on ``element_id``."""
    return [r for r in refs if r.to_id == element_id]
