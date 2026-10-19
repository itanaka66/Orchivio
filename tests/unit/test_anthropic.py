import json

import httpx
import pytest

from orchivio.llm import AnthropicProvider, LLMError, LLMProvider, LLMRequest, LLMTimeout, Message


def provider(handler, **kw):
    client = httpx.Client(base_url="https://api.test", transport=httpx.MockTransport(handler))
    return AnthropicProvider("sk-test", client=client, **kw)


def ok(request):
    return httpx.Response(200, json={
        "model": "claude-sonnet-5-5", "stop_reason": "end_turn",
        "content": [{"type": "text", "text": "{\"a\":"}, {"type": "text", "text": "1}"}],
        "usage": {"input_tokens": 12, "output_tokens": 5}})


def test_request_shape_and_response_parsing():
    seen = {}

    def handler(request):
        seen["body"], seen["headers"], seen["url"] = json.loads(request.content), request.headers, str(request.url)
        return ok(request)

    p = provider(handler)
    assert isinstance(p, LLMProvider) and p.external is True
    req = LLMRequest(messages=(Message("system", "be brief"), Message("user", "hi"), Message("assistant", "yo"),
                               Message("user", "again")), response_schema={"type": "object"}, max_output_tokens=99)
    r = p.complete(req)
    assert r.text == '{"a":1}' and (r.usage.input_tokens, r.usage.output_tokens) == (12, 5)
    assert r.finish_reason == "end_turn" and r.provider == "anthropic"
    b = seen["body"]
    assert seen["url"] == "https://api.test/v1/messages" and seen["headers"]["x-api-key"] == "sk-test"
    assert seen["headers"]["anthropic-version"] and b["max_tokens"] == 99 and b["model"] == "claude-sonnet-5-5"
    assert [m["role"] for m in b["messages"]] == ["user", "assistant", "user"]
    assert "be brief" in b["system"] and "JSON Schema" in b["system"]


def test_errors_are_mapped_and_do_not_leak_the_key():
    def timeout(request):
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(LLMTimeout):
        provider(timeout).complete(LLMRequest(messages=(Message("user", "x"),)))

    def limited(request):
        return httpx.Response(429, json={"error": {"message": "rate limited"}})

    with pytest.raises(LLMError, match="429.*rate limited"):
        provider(limited).complete(LLMRequest(messages=(Message("user", "x"),)))

    def garbage(request):
        return httpx.Response(200, text="not json")

    with pytest.raises(LLMError, match="unexpected response"):
        provider(garbage).complete(LLMRequest(messages=(Message("user", "x"),)))

    def down(request):
        raise httpx.ConnectError("boom", request=request)

    with pytest.raises(LLMError) as e:
        provider(down).complete(LLMRequest(messages=(Message("user", "x"),)))
    assert "sk-test" not in str(e.value)


def test_tool_role_and_missing_key_are_rejected():
    with pytest.raises(LLMError):
        provider(ok).complete(LLMRequest(messages=(Message("tool", "x"),)))
    with pytest.raises(ValueError):
        AnthropicProvider("")


def test_cost_is_calculated_from_pricing_table():
    r = provider(ok).complete(LLMRequest(messages=(Message("user", "hi"),)))
    # claude-sonnet-5-5 prefix → $3/M in, $15/M out; 12 in + 5 out
    assert r.usage.cost_usd == pytest.approx((12 * 3.0 + 5 * 15.0) / 1_000_000)


def test_custom_prices_override_default():
    r = provider(ok, prices={"claude-sonnet-5-5": (1.0, 2.0)}).complete(LLMRequest(messages=(Message("user", "hi"),)))
    assert r.usage.cost_usd == pytest.approx((12 * 1.0 + 5 * 2.0) / 1_000_000)


def test_unknown_model_returns_zero_cost():
    def custom_ok(request):
        return httpx.Response(200, json={"model": "unknown-future-model", "stop_reason": "end_turn",
                                         "content": [{"type": "text", "text": "hi"}],
                                         "usage": {"input_tokens": 10, "output_tokens": 10}})
    r = provider(custom_ok).complete(LLMRequest(messages=(Message("user", "hi"),)))
    assert r.usage.cost_usd == 0.0
