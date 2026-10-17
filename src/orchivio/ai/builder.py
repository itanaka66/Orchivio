"""AI Builder: requirements -> validated, linted model proposals (DESIGN.md sections 7.2, 11.2).

The builder never applies anything. It returns a candidate model that already passed schema
validation, static validation and blocking lint rules (or raises ``BuilderError`` with the
unresolved problems). Persisting it as a proposal and approving it is the caller's job.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace

from pydantic import BaseModel, Field

from orchivio.llm import (
    LLMProvider,
    LLMRequest,
    Message,
    StructuredOutputError,
    Usage,
    generate_validated,
)
from orchivio.model import (
    AppModel,
    Finding,
    Issue,
    assign_element_ids,
    blocking,
    build_refs,
    has_errors,
    iter_elements,
    lint,
    validate_model,
)

from . import prompts


class BuilderError(Exception):
    """The provider could not produce an acceptable result. ``issues`` explains why."""

    def __init__(self, message: str, *, issues: list[Issue] | None = None, usage: Usage = Usage(),
                 attempts: int = 0, template: str = "") -> None:
        super().__init__(message)
        self.issues = issues or []
        self.usage = usage
        self.attempts = attempts
        self.template = template


@dataclass(frozen=True)
class BuildResult:
    model: AppModel
    issues: list[Issue]
    findings: list[Finding]
    usage: Usage
    attempts: int
    provider: str
    model_name: str
    template: str


class AnalysisEntity(BaseModel):
    name: str = Field(max_length=100)
    purpose: str = Field(max_length=300)
    key_fields: list[str] = Field(default_factory=list, max_length=30)


class Analysis(BaseModel):
    summary: str = Field(max_length=2000)
    roles: list[str] = Field(default_factory=list, max_length=20)
    entities: list[AnalysisEntity] = Field(default_factory=list, max_length=50)
    open_questions: list[str] = Field(default_factory=list, max_length=5)
    assumptions: list[str] = Field(default_factory=list, max_length=20)


class Explanation(BaseModel):
    explanation: str = Field(max_length=3000)
    cited_element_ids: list[str] = Field(min_length=1, max_length=30)


@dataclass(frozen=True)
class AnalysisResult:
    analysis: Analysis
    usage: Usage
    attempts: int
    provider: str
    model_name: str
    template: str


@dataclass(frozen=True)
class ExplanationResult:
    explanation: str
    cited_element_ids: list[str]
    usage: Usage
    attempts: int
    provider: str
    model_name: str
    template: str
    dropped_citations: list[str] = field(default_factory=list)


def _issue_lines(issues: list[Issue], limit: int = 30) -> str:
    return "\n".join(f"- [{i.code}] {i.path}: {i.message}" for i in issues[:limit])


class AIBuilder:
    def __init__(self, provider: LLMProvider, *, max_validation_attempts: int = 3, max_repairs: int = 2,
                 timeout_s: float = 90.0, max_output_tokens: int = 8000) -> None:
        self.provider = provider
        self.max_validation_attempts = max_validation_attempts
        self.max_repairs = max_repairs
        self.timeout_s = timeout_s
        self.max_output_tokens = max_output_tokens

    def _request(self, template: prompts.Template, user: str) -> LLMRequest:
        return LLMRequest(
            messages=(Message("system", template.system), Message("user", user)),
            temperature=0.0, max_output_tokens=self.max_output_tokens, timeout_s=self.timeout_s,
            prompt_template=template.ref,
        )

    # -- requirements analysis ---------------------------------------------------------------

    def analyze(self, requirements: str) -> AnalysisResult:
        req = self._request(prompts.ANALYZE, prompts.block("requirements", requirements))
        try:
            v = generate_validated(self.provider, req, Analysis, max_attempts=self.max_validation_attempts)
        except StructuredOutputError as e:
            raise self._failed(e, prompts.ANALYZE) from e
        return AnalysisResult(v.value, v.usage, v.attempts, v.last_response.provider, v.last_response.model,
                              prompts.ANALYZE.ref)

    # -- model proposal ----------------------------------------------------------------------

    def propose_model(self, requirements: str, current: AppModel | None = None) -> BuildResult:
        tpl = prompts.PROPOSE_MODEL
        user = prompts.block("requirements", requirements)
        if current is not None:
            user += "\n\n" + prompts.model_block(current.model_dump(mode="json", exclude_none=True))
            user += "\n\nReturn the complete updated model."
        req = self._request(tpl, user)
        base_messages = req.messages
        total, attempts = Usage(), 0
        last_issues: list[Issue] = []
        for _ in range(self.max_repairs + 1):
            try:
                v = generate_validated(self.provider, req, AppModel, max_attempts=self.max_validation_attempts)
            except StructuredOutputError as e:
                raise self._failed(e, tpl, total, attempts) from e
            total, attempts = total + v.usage, attempts + v.attempts
            candidate = assign_element_ids(v.value, previous=current)
            issues = validate_model(candidate)
            findings = lint(candidate) if not has_errors(issues) else []
            blocked = [i for i in issues if i.severity == "error"] + [
                Issue("error", f"lint:{f.rule_id}", f.path, f.message, f.element_id) for f in blocking(findings)
            ]
            if not blocked:
                r = v.last_response
                return BuildResult(candidate, issues, findings, total, attempts, r.provider, r.model, tpl.ref)
            last_issues = blocked
            req = replace(req, messages=(
                *base_messages,
                Message("assistant", v.last_response.text[:8000]),
                Message("user", "That model has problems. Return the complete corrected model.\n"
                                + _issue_lines(blocked)),
            ))
        raise BuilderError("the model still has errors after repair attempts", issues=last_issues,
                           usage=total, attempts=attempts, template=tpl.ref)

    # -- explain -----------------------------------------------------------------------------

    def explain(self, model: AppModel, element_id: str, question: str | None = None) -> ExplanationResult:
        tpl = prompts.EXPLAIN
        ctx = element_context(model, element_id)
        if ctx is None:
            raise KeyError(element_id)
        user = prompts.block("element_context", json.dumps(ctx, separators=(",", ":"), ensure_ascii=False))
        user += "\n\n" + prompts.block("requirements", question or "Explain what this element does and why it matters.")
        known = {e["element_id"] for e in ctx["elements"]}
        req = self._request(tpl, user)
        total, attempts = Usage(), 0
        for round_ in range(2):
            try:
                v = generate_validated(self.provider, req, Explanation, max_attempts=self.max_validation_attempts)
            except StructuredOutputError as e:
                raise self._failed(e, tpl, total, attempts) from e
            total, attempts = total + v.usage, attempts + v.attempts
            valid = [c for c in v.value.cited_element_ids if c in known]
            dropped = [c for c in v.value.cited_element_ids if c not in known]
            if valid:
                r = v.last_response
                return ExplanationResult(v.value.explanation, valid, total, attempts, r.provider, r.model,
                                         tpl.ref, dropped)
            req = replace(req, messages=(*req.messages, Message("user",
                "Cite at least one element_id that appears in the element context.")))
        raise BuilderError("the explanation did not cite any known element", usage=total, attempts=attempts,
                           template=tpl.ref)

    @staticmethod
    def _failed(e: StructuredOutputError, tpl: prompts.Template, usage: Usage = Usage(), attempts: int = 0) -> BuilderError:
        for a in e.attempts:
            usage = usage + a.usage
        return BuilderError(f"no valid output from the model: {e.errors[-1] if e.errors else 'unknown'}",
                            usage=usage, attempts=attempts + len(e.attempts), template=tpl.ref)


def element_context(model: AppModel, element_id: str) -> dict | None:
    """The target element plus its direct dependencies and dependents (minimal LLM context)."""
    elements = {el.element_id: (key, el) for key, el in iter_elements(model) if el.element_id}
    if element_id not in elements:
        return None
    refs = build_refs(model)
    related = {element_id}
    related |= {r.to_id for r in refs if r.from_id == element_id}
    related |= {r.from_id for r in refs if r.to_id == element_id}
    out = []
    for eid in sorted(related):
        key, el = elements[eid]
        dump = el.model_dump(mode="json", exclude_none=True)
        for child in ("fields", "enumerations", "entities", "pages"):
            dump.pop(child, None)  # children appear as their own elements when related
        out.append({"element_id": eid, "kind": key[0], "name": ".".join(key[1:]), "definition": dump})
    return {"target": element_id, "elements": out,
            "relations": [{"from": r.from_id, "to": r.to_id, "kind": r.kind}
                          for r in refs if r.from_id in related and r.to_id in related]}
