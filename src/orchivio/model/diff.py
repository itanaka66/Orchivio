"""Element-level model diff with risk classification (FR-026, DESIGN.md section 8.4/12.3).

Elements are matched by ``element_id`` (not by name), so a rename is a property change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from orchivio.policy import ApprovalLevel

from .ids import iter_elements
from .schema import AppModel

ChangeKind = Literal["added", "removed", "modified"]
_CHILD_KEYS = {
    "module": {"enumerations", "entities", "pages", "flows", "integrations", "processes"},
    "entity": {"fields"},
}


class MissingElementId(ValueError):
    """Run ``assign_element_ids`` before diffing."""


@dataclass(frozen=True)
class Change:
    kind: ChangeKind
    element_type: str
    element_id: str
    name: str
    properties: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()
    level: ApprovalLevel = ApprovalLevel.A2

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "element_type": self.element_type,
            "element_id": self.element_id,
            "name": self.name,
            "properties": list(self.properties),
            "risks": list(self.risks),
            "level": self.level.name,
        }


@dataclass(frozen=True)
class ModelDiff:
    changes: list[Change] = field(default_factory=list)
    security_changed: bool = False

    @property
    def is_empty(self) -> bool:
        return not self.changes

    @property
    def level(self) -> ApprovalLevel:
        """Highest approval level any change needs; A2 (apply) when anything changed."""
        return max((c.level for c in self.changes), default=ApprovalLevel.A0)

    @property
    def risky(self) -> list[Change]:
        return [c for c in self.changes if c.risks]


def _flatten(model: AppModel) -> dict[str, tuple[str, str, dict[str, Any]]]:
    out: dict[str, tuple[str, str, dict[str, Any]]] = {}
    for key, el in iter_elements(model):
        if not el.element_id:
            raise MissingElementId(f"{key} has no element_id")
        etype = key[0]
        skip = _CHILD_KEYS.get(etype, set()) | {"element_id"}
        props = el.model_dump(mode="json", exclude=skip)
        if etype in ("flow", "process"):  # node positions are layout, not behaviour
            for step in props.get("steps", []):
                step.pop("position", None)
        name = ".".join(key[1:])
        out[el.element_id] = (etype, name, props)
    return out


def _widened(old: dict[str, Any], new: dict[str, Any]) -> bool:
    """Did an access rule grant more than before?"""
    if set(new["operations"]) - set(old["operations"]):
        return True
    if old.get("row_constraint") is not None and new.get("row_constraint") is None:
        return True
    for kind in ("read", "write"):
        before, after = (old.get("fields") or {}).get(kind), (new.get("fields") or {}).get(kind)
        if before is not None and (after is None or set(after) - set(before)):
            return True
    return False


def _risks(etype: str, old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    risks: list[str] = []
    if etype == "field":
        if old["type"] != new["type"]:
            risks.append("type-change")
        if not old["required"] and new["required"]:
            risks.append("required-added")
        if old["name"] != new["name"]:
            risks.append("rename")
    elif etype == "enum":
        if set(old["values"]) - set(new["values"]):
            risks.append("enum-value-removed")
    elif etype == "access":
        if _widened(old, new):
            risks.append("permission-expansion")
    elif etype == "process":
        before, after = set(old.get("start_roles", [])), set(new.get("start_roles", []))
        if after - before or (old.get("run_as_role") != new.get("run_as_role") and new.get("run_as_role")):
            risks.append("permission-expansion")  # who may start it, or what its system tasks may do, grew
    elif etype == "integration":
        risks.append("external-integration")  # what may leave the platform, and where, changed
    elif etype == "page":
        def with_flow(page: dict[str, Any]) -> set[tuple[str, str]]:
            return {(a["key"], a["flow"]) for a in page.get("actions", []) if isinstance(a, dict) and a.get("flow")}

        if with_flow(new) - with_flow(old):
            risks.append("permission-expansion")  # everyone who may open the page can now run another flow
    elif etype == "flow":
        if set(new.get("run_roles") or []) - set(old.get("run_roles") or []) or (old.get("run_roles") is None and new.get("run_roles")):
            risks.append("permission-expansion")
        before = {(s["config"].get("integration"), s["config"].get("operation")) for s in old.get("steps", []) if s["type"] == "call_integration"}
        after = {(s["config"].get("integration"), s["config"].get("operation")) for s in new.get("steps", []) if s["type"] == "call_integration"}
        if after - before:
            risks.append("external-call")
    elif etype in ("entity", "page", "module") and old.get("name", old.get("key")) != new.get(
        "name", new.get("key")
    ):
        risks.append("rename")
    return risks


def diff_models(base: AppModel, new: AppModel) -> ModelDiff:
    """Compare two identified models. Changes are ordered deterministically."""
    a, b = _flatten(base), _flatten(new)
    changes: list[Change] = []

    for eid, (etype, name, props) in b.items():
        if eid not in a:
            risks = ["permission-grant"] if etype == "access" else ["external-integration"] if etype == "integration" else []
            if etype == "flow" and any(s["type"] == "call_integration" for s in props.get("steps", [])):
                risks = ["external-call"]
            if (etype == "flow" and props.get("run_roles")) or (
                    etype == "page" and any(isinstance(x, dict) and x.get("flow") for x in props.get("actions", []))):
                risks = [*risks, "permission-expansion"]  # new ways for users to run a flow
            lvl = ApprovalLevel.A3 if risks else ApprovalLevel.A2
            changes.append(Change("added", etype, eid, name, (), tuple(risks), lvl))
            continue
        _, _, old = a[eid]
        changed = tuple(sorted(k for k in props.keys() | old.keys() if props.get(k) != old.get(k)))
        if changed:
            risks = _risks(etype, old, props)
            lvl = ApprovalLevel.A2
            if {"permission-expansion", "external-integration", "external-call"} & set(risks):
                lvl = ApprovalLevel.A3
            if {"type-change", "enum-value-removed"} & set(risks):
                lvl = max(lvl, ApprovalLevel.A3)
            changes.append(Change("modified", etype, eid, name, changed, tuple(risks), lvl))

    for eid, (etype, name, _) in a.items():
        if eid not in b:
            risks = ["destructive"] if etype in ("entity", "field", "enum", "module") else []
            if etype == "access":
                risks = []  # revoking access narrows permissions
            lvl = ApprovalLevel.A3 if risks else ApprovalLevel.A2
            changes.append(Change("removed", etype, eid, name, (), tuple(risks), lvl))

    sec_a, sec_b = base.security, new.security
    sec_changed = (
        sec_a.roles != sec_b.roles or sec_a.pages != sec_b.pages or sec_a.anonymous != sec_b.anonymous
    )
    sec_id = "security"
    if sec_changed:
        risks = ["anonymous-access"] if sec_b.anonymous and not sec_a.anonymous else []
        changes.append(
            Change("modified", "security", sec_id, "security", ("roles/pages/anonymous",), tuple(risks), ApprovalLevel.A3)
        )

    order = {"removed": 0, "added": 1, "modified": 2}
    changes.sort(key=lambda c: (order[c.kind], c.element_type, c.name))
    return ModelDiff(changes, sec_changed or any(c.element_type == "access" for c in changes))


__all__ = ["Change", "MissingElementId", "ModelDiff", "diff_models"]
