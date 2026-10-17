"""Derive test cases from a model (FR-040, DESIGN.md Phase 5).

Case ids are built from element keys, not counters, so they stay stable across revisions and a
result can be compared with the previous run.
"""

from __future__ import annotations

from dataclasses import dataclass

from orchivio.model import AppModel, ModelIndex
from orchivio.model.schema import OPERATIONS, parse_field_type
from orchivio.security import resolve_access


@dataclass(frozen=True)
class Case:
    id: str
    kind: str  # access | row-constraint | crud | validation
    title: str
    entity: str
    role: str | None = None
    op: str | None = None
    expect: str = "pass"  # allow | deny | pass
    field: str | None = None
    rule: str | None = None  # validation rule name, or access rule element id


def generate_cases(model: AppModel) -> list[Case]:
    index = ModelIndex(model)
    persistent = {q: ent for q, (_m, ent) in index.entities.items() if not ent.transient}
    cases: list[Case] = []

    # 1. access matrix: every (entity, role, operation) must behave as the model says
    for q in sorted(persistent):
        for role in model.security.roles:
            for op in OPERATIONS:
                allowed = resolve_access(model, [role], q, op).allowed
                cases.append(Case(f"access:{q}:{role}:{op}", "access",
                                  f"{role} {'may' if allowed else 'may not'} {op} {q}", q, role, op,
                                  "allow" if allowed else "deny"))

    # 2. row constraints: SQL filtering must equal the interpreter on real rows
    for r in model.security.access:
        if r.row_constraint and r.entity in persistent:
            for op in ("read", "create", "update", "delete"):
                if op in r.operations:
                    cases.append(Case(f"row:{r.entity}:{r.role}:{op}:{(r.element_id or '')[:8]}", "row-constraint",
                                      f"{r.role} {op} is limited by: {r.row_constraint[:80]}", r.entity, r.role, op,
                                      "pass", rule=r.element_id))

    # 3. CRUD smoke and 4. validation per entity
    for q, ent in sorted(persistent.items()):
        cases.append(Case(f"crud:{q}", "crud", f"create, read, update, delete one {ent.name}", q))
        for f in ent.fields:
            if f.generated or f.computed:
                continue
            kind, target = parse_field_type(f.type) or ("", None)
            fid = f"validation:{q}:{f.name}"
            if f.required and not f.default_expr and f.default is None:
                cases.append(Case(f"{fid}:required", "validation", f"{ent.name}.{f.name} is required", q, field=f.name, rule="required"))
            if f.max_length and target in ("string", "text"):
                cases.append(Case(f"{fid}:max_length", "validation", f"{ent.name}.{f.name} rejects > {f.max_length} characters", q, field=f.name, rule="max_length"))
            if f.minimum is not None and target in ("integer", "decimal"):
                cases.append(Case(f"{fid}:minimum", "validation", f"{ent.name}.{f.name} rejects < {f.minimum}", q, field=f.name, rule="minimum"))
            if f.maximum is not None and target in ("integer", "decimal"):
                cases.append(Case(f"{fid}:maximum", "validation", f"{ent.name}.{f.name} rejects > {f.maximum}", q, field=f.name, rule="maximum"))
            if f.unique and not f.primary_key:
                cases.append(Case(f"{fid}:unique", "validation", f"{ent.name}.{f.name} must be unique", q, field=f.name, rule="unique"))
            if kind == "enum":
                cases.append(Case(f"{fid}:enum", "validation", f"{ent.name}.{f.name} rejects values outside {f.type}", q, field=f.name, rule="enum"))
        cases.append(Case(f"validation:{q}:*:unknown_field", "validation", f"{ent.name} rejects unknown fields", q, rule="unknown_field"))
    return cases
