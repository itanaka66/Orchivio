import json

import httpx
import pytest

from orchivio.llm import AnnaSamplingProvider, LLMError, LLMProvider, LLMRequest, LLMTimeout, Message


def provider(handler, **kw):
    client = httpx.Client(base_url="http://relay.test", transport=httpx.MockTransport(handler))
    return AnnaSamplingProvider("http://relay.test", client=client, **kw)


def ok(request):
    return httpx.Response(200, json={
        "text": '{"a":1}',
        "model": "anna-claude",
        "input_tokens": 12,
        "output_tokens": 5,
        "stop_reason": "end_turn",
    })


def test_request_shape_and_response_parsing():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        seen["url"] = str(request.url)
        return ok(request)

    p = provider(handler)
    assert isinstance(p, LLMProvider) and p.external is False
    req = LLMRequest(
        messages=(Message("system", "be brief"), Message("user", "hi"), Message("assistant", "yo"), Message("user", "again")),
        response_schema={"type": "object"},
        max_output_tokens=99,
    )
    r = p.complete(req)
    assert r.text == '{"a":1}' and (r.usage.input_tokens, r.usage.output_tokens) == (12, 5)
    assert r.finish_reason == "end_turn" and r.provider == "anna"
    b = seen["body"]
    assert seen["url"] == "http://relay.test/complete"
    assert b["max_tokens"] == 99
    assert [m["role"] for m in b["messages"]] == ["user", "assistant", "user"]
    assert "be brief" in b["system"] and "JSON Schema" in b["system"]


def test_errors_are_mapped():
    def timeout(request):
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(LLMTimeout):
        provider(timeout).complete(LLMRequest(messages=(Message("user", "x"),)))

    def limited(request):
        return httpx.Response(502, text="bad gateway")

    with pytest.raises(LLMError, match="502"):
        provider(limited).complete(LLMRequest(messages=(Message("user", "x"),)))

    def garbage(request):
        return httpx.Response(200, text="not json")

    with pytest.raises(LLMError, match="unexpected response"):
        provider(garbage).complete(LLMRequest(messages=(Message("user", "x"),)))

    def down(request):
        raise httpx.ConnectError("boom", request=request)

    with pytest.raises(LLMError):
        provider(down).complete(LLMRequest(messages=(Message("user", "x"),)))


def test_tool_role_is_rejected():
    with pytest.raises(LLMError):
        provider(ok).complete(LLMRequest(messages=(Message("tool", "x"),)))


def test_anna_billing_is_zero_cost():
    r = provider(ok).complete(LLMRequest(messages=(Message("user", "hi"),)))
    assert r.usage.cost_usd == 0.0
