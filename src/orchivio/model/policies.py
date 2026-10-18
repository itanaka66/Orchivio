"""Organization policy packs (FR-124): organization-wide rules a model must satisfy.

A pack is a list of rules from a fixed catalogue (no expressions, no code), each with parameters and a severity. Evaluation
is pure: model in, violations out. ``error`` violations block a new revision exactly like a validation error;
``warning`` violations are reported but do not block. Existing revisions are never changed by a new pack: the compliance
report shows what no longer complies, and the next change must.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .schema import ALL_OPERATIONS, AppModel


@dataclass(frozen=True)
class Violation:
    rule: str
    severity: str  # error | warning
    path: str
    message: str
    element_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"rule": self.rule, "severity": self.severity, "path": self.path, "message": self.message, "element_id": self.element_id}


Check = Callable[[AppModel, dict[str, Any]], list[tuple[str, str, str | None]]]  # (path, message, element id)


def _no_anonymous(model: AppModel, p: dict[str, Any]) -> list[tuple[str, str, str | None]]:
    return [("/security/anonymous", "anonymous access is not allowed in this organization", None)] if model.security.anonymous else []


def _integration_destinations(model: AppModel, p: dict[str, Any]) -> list[tuple[str, str, str | None]]:
    allowed = set(p.get("allowed", []))
    out = []
    for mi, mod in enumerate(model.modules):
        for ig in mod.integrations or []:
            if ig.destination not in allowed:
                shown = ", ".join(sorted(allowed)) or "none"
                out.append((f"/modules/{mi}/integrations/{ig.key}", f"integration '{ig.key}' uses destination '{ig.destination}'; allowed: {shown}", ig.element_id))
    return out


def _forbid_operations(model: AppModel, p: dict[str, Any]) -> list[tuple[str, str, str | None]]:
    roles, ops = p.get("roles", "*"), set(p["operations"])
    out = []
    for ai, rule in enumerate(model.security.access):
        bad = sorted(ops & set(rule.operations))
        if bad and (roles == "*" or rule.role in roles):
            out.append((f"/security/access/{ai}", f"role '{rule.role}' may not be granted {', '.join(bad)} on {rule.entity} in this organization", rule.element_id))
    return out


def _require_row_constraint(model: AppModel, p: dict[str, Any]) -> list[tuple[str, str, str | None]]:
    entities, ops, exempt = p.get("entities", "*"), set(p["operations"]), set(p.get("exempt_roles", []))
    out = []
    for ai, rule in enumerate(model.security.access):
        if rule.role in exempt or rule.row_constraint or not ops & set(rule.operations):
            continue
        if entities == "*" or rule.entity in entities:
            out.append((f"/security/access/{ai}", f"role '{rule.role}' needs a row constraint for {', '.join(sorted(ops & set(rule.operations)))} on {rule.entity}", rule.element_id))
    return out


def _max_roles(model: AppModel, p: dict[str, Any]) -> list[tuple[str, str, str | None]]:
    n = len(model.security.roles)
    return [("/security/roles", f"the model defines {n} roles; at most {p['max']} are allowed", None)] if n > p["max"] else []


def _forbid_automation_roles(model: AppModel, p: dict[str, Any]) -> list[tuple[str, str, str | None]]:
    """Scheduled flows, processes and flows exposed to roles must not run with these (powerful) roles."""
    banned = set(p["roles"])
    out = []
    for mi, mod in enumerate(model.modules):
        for fl in mod.flows:
            if fl.schedule is not None and fl.schedule.run_as_role in banned:
                out.append((f"/modules/{mi}/flows/{fl.key}/schedule", f"flow '{fl.key}' is scheduled as '{fl.schedule.run_as_role}'", fl.element_id))
        for pr in mod.processes or []:
            if pr.run_as_role in banned:
                out.append((f"/modules/{mi}/processes/{pr.key}", f"process '{pr.key}' runs its system tasks as '{pr.run_as_role}'", pr.element_id))
    return out


def _no_flow_exposure(model: AppModel, p: dict[str, Any]) -> list[tuple[str, str, str | None]]:
    """Which roles may be given direct flow runs (``run_roles``) and page-bound flow actions."""
    allowed = set(p.get("roles", []))
    out = []
    for mi, mod in enumerate(model.modules):
        for fl in mod.flows:
            extra = sorted(set(fl.run_roles or []) - allowed)
            if extra:
                out.append((f"/modules/{mi}/flows/{fl.key}/run_roles", f"flow '{fl.key}' may be run directly by {', '.join(extra)}", fl.element_id))
    return out


RULES: dict[str, tuple[Check, str]] = {
    "no-anonymous-access": (_no_anonymous, "Anonymous access must stay off."),
    "integration-destinations": (_integration_destinations, "Integrations may only use the listed destination names (params: allowed)."),
    "forbid-operations": (_forbid_operations, "Access rules may not grant these operations (params: operations, roles or '*')."),
    "require-row-constraint": (_require_row_constraint, "Access rules granting these operations need a row constraint (params: operations, entities or '*', exempt_roles)."),
    "max-roles": (_max_roles, "At most this many application roles (params: max)."),
    "forbid-automation-roles": (_forbid_automation_roles, "Schedules and processes may not run as these roles (params: roles)."),
    "limit-flow-run-roles": (_no_flow_exposure, "Only these roles may be listed in flows' run_roles (params: roles)."),
}


class PolicyRule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rule: Literal["no-anonymous-access", "integration-destinations", "forbid-operations", "require-row-constraint", "max-roles",
                  "forbid-automation-roles", "limit-flow-run-roles"]
    params: dict[str, Any] = Field(default_factory=dict)
    severity: Literal["error", "warning"] = "error"

    @field_validator("params")
    @classmethod
    def _small(cls, v: dict[str, Any]) -> dict[str, Any]:
        if len(str(v)) > 4000:
            raise ValueError("parameters are too large")
        return v


def _names(v: Any, what: str, allow_star: bool = False) -> list[str] | str:
    if allow_star and v == "*":
        return "*"
    if not isinstance(v, list) or not all(isinstance(x, str) and 0 < len(x) <= 100 for x in v) or len(v) > 100:
        raise ValueError(f"{what} must be a list of names" + (" or '*'" if allow_star else ""))
    return v


def normalise(rule: PolicyRule) -> PolicyRule:
    """Check and complete the parameters of one rule. Raises ``ValueError`` with a message for the author."""
    p, name = dict(rule.params), rule.rule
    known: dict[str, set[str]] = {"no-anonymous-access": set(), "integration-destinations": {"allowed"}, "forbid-operations": {"operations", "roles"},
                                  "require-row-constraint": {"operations", "entities", "exempt_roles"}, "max-roles": {"max"},
                                  "forbid-automation-roles": {"roles"}, "limit-flow-run-roles": {"roles"}}
    extra = set(p) - known[name]
    if extra:
        raise ValueError(f"{name}: unknown parameter(s) {', '.join(sorted(extra))}")
    if name == "integration-destinations":
        p["allowed"] = _names(p.get("allowed", []), "allowed")
    elif name in ("forbid-operations", "require-row-constraint"):
        ops = _names(p.get("operations"), "operations")
        bad = sorted(set(ops) - set(ALL_OPERATIONS))
        if not ops or bad:
            raise ValueError(f"{name}: operations must be some of {', '.join(ALL_OPERATIONS)}")
        p["operations"] = ops
        if name == "forbid-operations":
            p["roles"] = _names(p.get("roles", "*"), "roles", True)
        else:
            p["entities"] = _names(p.get("entities", "*"), "entities", True)
            p["exempt_roles"] = _names(p.get("exempt_roles", []), "exempt_roles")
    elif name == "max-roles":
        if not isinstance(p.get("max"), int) or isinstance(p.get("max"), bool) or not 1 <= p["max"] <= 1000:
            raise ValueError("max-roles: max must be an integer from 1 to 1000")
    elif name in ("forbid-automation-roles", "limit-flow-run-roles"):
        p["roles"] = _names(p.get("roles", []), "roles")
        if name == "forbid-automation-roles" and not p["roles"]:
            raise ValueError("forbid-automation-roles: give at least one role")
    return PolicyRule(rule=rule.rule, params=p, severity=rule.severity)


def parse_rules(raw: Any) -> list[PolicyRule]:
    """Validate the rules of a pack (a list of ``{rule, params, severity}``)."""
    if not isinstance(raw, list) or not 1 <= len(raw) <= 50:
        raise ValueError("a pack has 1-50 rules")
    try:
        return [normalise(PolicyRule.model_validate(r)) for r in raw]
    except ValidationError as e:
        first = e.errors()[0]
        raise ValueError(f"invalid rule: {'.'.join(str(x) for x in first['loc'])}: {first['msg']}") from e


def evaluate(model: AppModel, rules: list[dict[str, Any]] | list[PolicyRule]) -> list[Violation]:
    out: list[Violation] = []
    for r in rules:
        rule = r if isinstance(r, PolicyRule) else PolicyRule.model_validate(r)
        check = RULES[rule.rule][0]
        out += [Violation(rule.rule, rule.severity, path, msg, eid) for path, msg, eid in check(model, rule.params)]
    return out


__all__ = ["RULES", "PolicyRule", "Violation", "evaluate", "normalise", "parse_rules"]
