"""AI quality eval harness.

Run with:
    ORCHIVIO_LIVE_LLM=1 ORCHIVIO_ANTHROPIC_API_KEY=sk-... pytest tests/evals -v

Rubrics are in tests/evals/fixtures/rubrics.yaml. Each rubric specifies a prompt
and scoring criteria (required keywords, entity names, forbidden names). The JSON
schema contract is already enforced by generate_validated; these checks catch
higher-level quality regressions.
"""

import os
from pathlib import Path

import pytest
import yaml

pytestmark = [
    pytest.mark.eval,
    pytest.mark.skipif(
        os.environ.get("ORCHIVIO_LIVE_LLM") != "1" or not os.environ.get("ORCHIVIO_ANTHROPIC_API_KEY"),
        reason="set ORCHIVIO_LIVE_LLM=1 and ORCHIVIO_ANTHROPIC_API_KEY to run evals",
    ),
]

_RUBRICS = yaml.safe_load(
    (Path(__file__).parent / "fixtures" / "rubrics.yaml").read_text(encoding="utf-8")
)


@pytest.fixture(scope="module")
def builder():
    from orchivio.ai import AIBuilder
    from orchivio.llm import AnthropicProvider

    kwargs = {"model": os.environ["ORCHIVIO_LLM_MODEL"]} if os.environ.get("ORCHIVIO_LLM_MODEL") else {}
    return AIBuilder(AnthropicProvider(os.environ["ORCHIVIO_ANTHROPIC_API_KEY"], **kwargs))


def _score(result, rubric: dict) -> list[str]:
    """Return human-readable failure strings; empty list = pass."""
    failures: list[str] = []
    analysis = result.analysis

    if not analysis.summary.strip():
        failures.append("summary is empty")
    else:
        for kw in rubric.get("required_keywords_in_summary", []):
            if kw.lower() not in analysis.summary.lower():
                failures.append(f"summary is missing keyword {kw!r}")

    entity_names = [e.name.lower() for e in analysis.entities]
    for name in rubric.get("required_entity_names", []):
        if not any(name.lower() in en for en in entity_names):
            failures.append(f"required entity {name!r} not found; got {[e.name for e in analysis.entities]}")

    for name in rubric.get("forbidden_entity_names", []):
        if any(name.lower() in en for en in entity_names):
            failures.append(f"forbidden entity {name!r} was generated (prompt-injection?)")

    min_e = rubric.get("min_entities", 0)
    if len(analysis.entities) < min_e:
        failures.append(f"only {len(analysis.entities)} entities returned, expected >= {min_e}")

    if result.usage.input_tokens == 0:
        failures.append("zero input tokens recorded — did the provider actually call the API?")

    return failures


@pytest.mark.parametrize("rubric", _RUBRICS, ids=[r["id"] for r in _RUBRICS])
def test_eval_rubric(builder, rubric):
    """Run the model against a rubric prompt and score the structured output."""
    result = builder.analyze(rubric["prompt"])
    failures = _score(result, rubric)
    assert not failures, (
        f"eval {rubric['id']!r} failed:\n"
        + "\n".join(f"  - {f}" for f in failures)
        + f"\n  summary snippet: {result.analysis.summary[:200]!r}"
        + f"\n  entities: {[e.name for e in result.analysis.entities]}"
    )
