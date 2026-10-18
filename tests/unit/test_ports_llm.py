import pytest
from pydantic import BaseModel

from orchivio.llm import (
    LLMError,
    LLMProvider,
    LLMRequest,
    Message,
    MockProvider,
    StructuredOutputError,
    generate_validated,
)
from orchivio.ports import (
    AuditRecord,
    InMemoryAuditSink,
    InMemoryJobQueue,
    InvalidKey,
    JobStatus,
    LocalArtifactStore,
)


class Plan(BaseModel):
    title: str
    tasks: list[str]


REQ = LLMRequest(messages=(Message("user", "plan an inventory app"),))


def test_mock_satisfies_provider_protocol():
    assert isinstance(MockProvider(), LLMProvider)


def test_valid_output_first_try():
    p = MockProvider(['{"title": "T", "tasks": ["a"]}'])
    r = generate_validated(p, REQ, Plan)
    assert r.value == Plan(title="T", tasks=["a"]) and r.attempts == 1
    assert p.requests[0].response_schema["title"] == "Plan"


def test_fenced_json_is_accepted():
    r = generate_validated(MockProvider(['```json\n{"title":"T","tasks":[]}\n```']), REQ, Plan)
    assert r.value.title == "T"


def test_invalid_output_is_retried_with_errors_shown_to_model():
    p = MockProvider(["not json", '{"title": "T"}', '{"title": "T", "tasks": ["x"]}'])
    r = generate_validated(p, REQ, Plan)
    assert r.attempts == 3 and r.usage.output_tokens >= 0
    second_prompt = p.requests[1].messages
    assert second_prompt[0] == REQ.messages[0]
    assert "not valid JSON" in second_prompt[-1].content
    assert "tasks" in p.requests[2].messages[-1].content


def test_gives_up_after_max_attempts_and_keeps_evidence():
    p = MockProvider(["x", "y"])
    with pytest.raises(StructuredOutputError) as e:
        generate_validated(p, REQ, Plan, max_attempts=2)
    assert len(e.value.attempts) == 2 and len(e.value.errors) == 2


def test_provider_errors_are_not_retried():
    p = MockProvider([LLMError("quota"), '{"title":"T","tasks":[]}'])
    with pytest.raises(LLMError):
        generate_validated(p, REQ, Plan)
    assert len(p.requests) == 1


def test_job_queue_lifecycle_retry_cancel_idempotency():
    calls = {"flaky": 0}

    def ok(job, ctx):
        ctx.report(0.5)
        return {"echo": job.payload["x"]}

    def flaky(job, ctx):
        calls["flaky"] += 1
        if calls["flaky"] < 2:
            raise RuntimeError("boom")
        return {}

    def boom(job, ctx):
        raise RuntimeError("always")

    q = InMemoryJobQueue({"ok": ok, "flaky": flaky, "boom": boom})
    a = q.submit("ok", {"x": 1}, idempotency_key="k")
    assert q.submit("ok", {"x": 2}, idempotency_key="k") is a
    b = q.submit("flaky", {}, max_attempts=2)
    c = q.submit("boom", {})
    d = q.submit("ok", {"x": 3})
    q.cancel(d.id)
    assert d.status is JobStatus.CANCELLED

    while q.run_next():
        pass
    assert (a.status, a.result, a.progress) == (JobStatus.SUCCEEDED, {"echo": 1}, 0.5)
    assert (b.status, b.attempts) == (JobStatus.SUCCEEDED, 2)
    assert c.status is JobStatus.FAILED and "always" in c.error
    assert q.cancel(a.id).status is JobStatus.SUCCEEDED  # terminal states are final
    with pytest.raises(ValueError):
        q.submit("rm -rf", {})  # unknown job types are rejected


def test_artifact_store_roundtrip_and_traversal_protection(tmp_path):
    store = LocalArtifactStore(tmp_path / "art")
    obj = store.put("exports/app-1/model.json", b"{}")
    assert obj.size == 2 and store.get("exports/app-1/model.json") == b"{}"
    assert store.exists("exports/app-1/model.json")
    store.delete("exports/app-1/model.json")
    assert not store.exists("exports/app-1/model.json")
    for bad in ["../x", "a/../../x", "/etc/passwd", "a//b", "", ".hidden", "a/./b", "C:\\x"]:
        with pytest.raises(InvalidKey):
            store.put(bad, b"x")


def test_in_memory_audit_sink():
    sink = InMemoryAuditSink()
    sink.record(AuditRecord("x.y"))
    assert sink.events[0].plane == "platform"
