"""AI Builder: requirements -> validated, linted model proposals (DESIGN.md sections 7.2, 11.2).

The builder never applies anything. It returns a candidate model that already passed schema
validation, static validation and blocking lint rules (or raises ``BuilderError`` with the
unresolved problems). Persisting it as a proposal and approving it is the caller's job.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from orchivio import telemetry
from orchivio.llm import (
    NO_USAGE,
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


def traced(operation: str):
    """Wrap an AIBuilder method in a span and count it. Only the operation, provider, model, tokens, cost and attempts are
    recorded: never prompts, requirements, model text or element names."""

    def deco(fn):
        def run(self, *args, **kwargs):
            with telemetry.span(f"ai.{operation}", kind="client", operation=operation) as sp:
                try:
                    out = fn(self, *args, **kwargs)
                except BuilderError as e:
                    sp.set_attributes(_usage_attrs(e.usage, getattr(e, "attempts", 0)))
                    sp.set_error("builder error")
                    telemetry.count("orchivio.ai.calls", operation=operation, outcome="invalid_output")
                    raise
                except Exception:
                    telemetry.count("orchivio.ai.calls", operation=operation, outcome="error")
                    raise
                usage = getattr(out, "usage", NO_USAGE)
                sp.set_attributes({**_usage_attrs(usage, getattr(out, "attempts", 0)), "ai.provider": getattr(out, "provider", "") or "",
                                   "ai.model": getattr(out, "model_name", None) or getattr(out, "model", "") or ""})
                telemetry.count("orchivio.ai.calls", operation=operation, outcome="ok")
                telemetry.count("orchivio.ai.tokens", usage.input_tokens, operation=operation, direction="input")
                telemetry.count("orchivio.ai.tokens", usage.output_tokens, operation=operation, direction="output")
                return out

        run.__name__, run.__doc__ = fn.__name__, fn.__doc__
        return run

    return deco


def _usage_attrs(u, attempts: int) -> dict:
    return {"ai.input_tokens": u.input_tokens, "ai.output_tokens": u.output_tokens, "ai.cost_usd": u.cost_usd, "ai.attempts": attempts}


class BuilderError(Exception):
    """The provider could not produce an acceptable result. ``issues`` explains why."""

    def __init__(self, message: str, *, issues: list[Issue] | None = None, usage: Usage = NO_USAGE,
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


class LogFinding(BaseModel):
    title: str = Field(max_length=200)
    severity: str = Field(pattern="^(low|medium|high)$")
    explanation: str = Field(max_length=1500)
    suggestion: str = Field(default="", max_length=1000)
    evidence_ids: list[int] = Field(min_length=1, max_length=50)


class LogAnalysis(BaseModel):
    summary: str = Field(max_length=2000)
    findings: list[LogFinding] = Field(default_factory=list, max_length=8)


@dataclass(frozen=True)
class LogAnalysisResult:
    analysis: LogAnalysis
    usage: Usage
    attempts: int
    provider: str
    model_name: str
    template: str
    dropped_findings: int = 0


class ProposedStory(BaseModel):
    title: str = Field(max_length=300)
    body: str = Field(max_length=2000)
    acceptance: str = Field(default="", max_length=2000)
    feedback_numbers: list[int] = Field(min_length=1, max_length=100)
    element_ids: list[str] = Field(default_factory=list, max_length=20)


class StoryProposals(BaseModel):
    stories: list[ProposedStory] = Field(default_factory=list, max_length=10)


@dataclass(frozen=True)
class ClusterResult:
    stories: list[ProposedStory]
    usage: Usage
    attempts: int
    provider: str
    model_name: str
    template: str
    dropped: int = 0


class ExtractedRequirement(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    body: str = Field(default="", max_length=2000)


class RequirementExtraction(BaseModel):
    requirements: list[ExtractedRequirement] = Field(default_factory=list, max_length=20)


@dataclass(frozen=True)
class ExtractResult:
    requirements: list[ExtractedRequirement]
    usage: Usage
    attempts: int
    provider: str
    model_name: str
    template: str
    skipped_duplicates: int = 0


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


class PlannedCall(BaseModel):
    model_config = ConfigDict(extra="forbid")
    server: str = Field(min_length=1, max_length=100)
    tool: str = Field(min_length=1, max_length=100)
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    calls: list[PlannedCall] = Field(default_factory=list, max_length=10)
    reason: str | None = Field(default=None, max_length=500)


@dataclass(frozen=True)
class ToolInfo:
    server: str
    tool: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class ToolContext:
    """Output of one external tool call, to be shown to the model as untrusted data."""

    server: str
    tool: str
    text: str


@dataclass
class PlanResult:
    calls: list[PlannedCall]
    usage: Usage
    attempts: int
    template: str


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

    @traced("analyze")
    def analyze(self, requirements: str) -> AnalysisResult:
        req = self._request(prompts.ANALYZE, prompts.block("requirements", requirements))
        try:
            v = generate_validated(self.provider, req, Analysis, max_attempts=self.max_validation_attempts)
        except StructuredOutputError as e:
            raise self._failed(e, prompts.ANALYZE) from e
        return AnalysisResult(v.value, v.usage, v.attempts, v.last_response.provider, v.last_response.model,
                              prompts.ANALYZE.ref)

    @traced("analyze_logs")
    def analyze_logs(self, entries: list[dict[str, Any]], question: str | None = None) -> LogAnalysisResult:
        """Explain a window of application log lines. Findings must cite ids that are in ``entries``; a finding whose citations
        are all unknown is dropped, unknown ids are removed from the rest."""
        tpl = prompts.ANALYZE_LOGS
        known = {int(e["id"]) for e in entries}
        user = prompts.block("logs", "\n".join(json.dumps(e, separators=(",", ":"), ensure_ascii=False) for e in entries))
        user += "\n\n" + prompts.block("requirements", question or "What went wrong, and what should the operator look at first?")
        try:
            v = generate_validated(self.provider, self._request(tpl, user), LogAnalysis, max_attempts=self.max_validation_attempts)
        except StructuredOutputError as e:
            raise self._failed(e, tpl) from e
        kept, dropped = [], 0
        for f in v.value.findings:
            ids = [i for i in f.evidence_ids if i in known]
            if ids:
                kept.append(f.model_copy(update={"evidence_ids": ids}))
            else:
                dropped += 1
        r = v.last_response
        return LogAnalysisResult(LogAnalysis(summary=v.value.summary, findings=kept), v.usage, v.attempts, r.provider, r.model, tpl.ref, dropped)

    @traced("extract_requirements")
    def extract_requirements(self, text: str, existing_titles: list[str]) -> ExtractResult:
        """Requirements stated in ``text``. Titles that repeat an existing one (or an earlier one in the answer) are dropped."""
        tpl = prompts.EXTRACT_REQUIREMENTS
        user = prompts.block("document", text) + "\n\n" + prompts.block("existing_titles", json.dumps(existing_titles[:200], ensure_ascii=False))
        try:
            v = generate_validated(self.provider, self._request(tpl, user), RequirementExtraction, max_attempts=self.max_validation_attempts)
        except StructuredOutputError as e:
            raise self._failed(e, tpl) from e
        seen = {t.strip().casefold() for t in existing_titles}
        kept, skipped = [], 0
        for r in v.value.requirements:
            key = r.title.strip().casefold()
            if not key or key in seen:
                skipped += 1
                continue
            seen.add(key)
            kept.append(r)
        r_ = v.last_response
        return ExtractResult(kept, v.usage, v.attempts, r_.provider, r_.model, tpl.ref, skipped)

    @traced("cluster_feedback")
    def cluster_feedback(self, items: list[dict[str, Any]], elements: dict[str, str]) -> ClusterResult:
        """Group preview feedback into proposed stories. ``items`` carry a ``number`` the model must cite; stories that cite no
        known item are dropped, an item is assigned to the first story that cites it, unknown element ids are removed."""
        tpl = prompts.CLUSTER_FEEDBACK
        known = {int(i["number"]) for i in items}
        user = prompts.block("feedback", "\n".join(json.dumps(i, separators=(",", ":"), ensure_ascii=False) for i in items))
        user += "\n\n" + prompts.block("elements", json.dumps(elements, separators=(",", ":"), ensure_ascii=False))
        try:
            v = generate_validated(self.provider, self._request(tpl, user), StoryProposals, max_attempts=self.max_validation_attempts)
        except StructuredOutputError as e:
            raise self._failed(e, tpl) from e
        taken: set[int] = set()
        kept, dropped = [], 0
        for s in v.value.stories:
            nums = [n for n in dict.fromkeys(s.feedback_numbers) if n in known and n not in taken]
            if not nums:
                dropped += 1
                continue
            taken.update(nums)
            kept.append(s.model_copy(update={"feedback_numbers": nums, "element_ids": [e for e in s.element_ids if e in elements]}))
        r = v.last_response
        return ClusterResult(kept, v.usage, v.attempts, r.provider, r.model, tpl.ref, dropped)

    # -- model proposal ----------------------------------------------------------------------

    @traced("plan_tool_calls")
    def plan_tool_calls(self, requirements: str, tools: list[ToolInfo], max_calls: int = 3) -> PlanResult:
        """Ask which of the allowed tools to call for background. The answer is only a *request*: the caller must
        check every call against the allowlist and the tool's schema before running anything."""
        tpl = prompts.PLAN_TOOLS
        catalogue = [{"server": t.server, "tool": t.tool, "description": t.description, "input_schema": t.input_schema} for t in tools]
        user = (prompts.block("requirements", requirements) + "\n\n"
                + prompts.block("tools", json.dumps(catalogue, separators=(",", ":"), ensure_ascii=False))
                + f"\n\nChoose at most {max_calls} calls.")
        req = self._request(tpl, user)
        try:
            v = generate_validated(self.provider, req, ToolPlan, max_attempts=self.max_validation_attempts)
        except StructuredOutputError as e:
            raise self._failed(e, tpl) from e
        return PlanResult(v.value.calls[:max_calls], v.usage, v.attempts, tpl.ref)

    @traced("propose_model")
    def propose_model(self, requirements: str, current: AppModel | None = None,
                      context: list[ToolContext] | None = None) -> BuildResult:
        tpl = prompts.PROPOSE_MODEL_WITH_CONTEXT if context else prompts.PROPOSE_MODEL
        user = prompts.block("requirements", requirements)
        for c in context or []:
            user += "\n\n" + prompts.block("external_context", f"source: {c.server}/{c.tool}\n{c.text}")
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

    @traced("explain")
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
        for _round in range(2):
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
    def _failed(e: StructuredOutputError, tpl: prompts.Template, usage: Usage = NO_USAGE, attempts: int = 0) -> BuilderError:
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
