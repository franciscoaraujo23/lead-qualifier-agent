"""OpenRouterProvider tests — offline. The real httpx client is constructed (so
its timeout is checked for real), but the network POST is replaced.

Like the Anthropic file, this is the only place the OpenRouter transport code is
exercised — the rest of the suite runs on the mock provider.
"""

import pytest

pytest.importorskip("httpx", reason="httpx not installed")

from lead_qualifier.errors import LLMCallError, LLMRefusalError  # noqa: E402
from lead_qualifier.llm.openrouter_provider import OpenRouterProvider  # noqa: E402


class _FakeResponse:
    def __init__(self, body, *, raise_exc=None):
        self._body = body
        self._raise_exc = raise_exc

    def raise_for_status(self):
        if self._raise_exc:
            raise self._raise_exc

    def json(self):
        return self._body


def _provider(body=None, *, raise_exc=None, post_raises=None) -> OpenRouterProvider:
    provider = OpenRouterProvider("test-key", "anthropic/claude-haiku-4.5", timeout_s=20.0)

    async def fake_post(url, *, headers, json):
        fake_post.url = url
        fake_post.headers = headers
        fake_post.json = json
        if post_raises:
            raise post_raises
        return _FakeResponse(body, raise_exc=raise_exc)

    provider._client.post = fake_post
    provider.fake_post = fake_post
    return provider


def _body(content="{}", *, prompt_tokens=120, completion_tokens=45, cost=0.0075, finish="stop"):
    usage = {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens}
    if cost is not None:
        usage["cost"] = cost
    return {
        "choices": [{"finish_reason": finish, "message": {"content": content}}],
        "usage": usage,
    }


def test_client_has_the_time_budget():
    """The per-call budget §4.8's n8n arithmetic depends on. httpx does not retry
    by default, so there is no hidden retry layer to disable."""
    provider = OpenRouterProvider("test-key", "anthropic/claude-haiku-4.5", timeout_s=20.0)
    assert provider._client.timeout.read == 20.0
    assert provider._client.timeout.connect == 20.0


async def test_sends_openai_shape_and_reports_usage_and_cost():
    provider = _provider(_body(content='{"score": 71}', cost=0.0075))
    completion = await provider.complete(system="sys", user="profile", max_tokens=512)

    assert completion.text == '{"score": 71}'
    assert (completion.usage.input_tokens, completion.usage.output_tokens) == (120, 45)
    assert completion.usage.model == "anthropic/claude-haiku-4.5"
    # The whole reason this provider exists alongside Anthropic: the real charge.
    assert completion.usage.cost_usd == 0.0075

    sent = provider.fake_post
    assert sent.headers["Authorization"] == "Bearer test-key"
    assert sent.json["model"] == "anthropic/claude-haiku-4.5"
    assert sent.json["max_tokens"] == 512
    assert sent.json["messages"] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "profile"},
    ]


async def test_missing_cost_falls_back_to_none_not_zero():
    """No `cost` field must not read as a free call — None lets the table estimate."""
    provider = _provider(_body(content="{}", cost=None))
    completion = await provider.complete(system="s", user="u")
    assert completion.usage.cost_usd is None


async def test_content_filter_is_a_non_retryable_refusal():
    provider = _provider(_body(content="", finish="content_filter"))
    with pytest.raises(LLMRefusalError) as exc:
        await provider.complete(system="s", user="u")
    assert exc.value.http_status == 422, "n8n only retries status 0 and 5xx"
    assert exc.value.failure_stage == "llm"


async def test_empty_text_is_a_refusal_not_malformed_json():
    provider = _provider(_body(content="   ", finish="stop"))
    with pytest.raises(LLMRefusalError):
        await provider.complete(system="s", user="u")


async def test_http_error_becomes_a_typed_llm_error():
    import httpx

    err = httpx.HTTPStatusError("500", request=None, response=None)
    provider = _provider(_body(), raise_exc=err)
    with pytest.raises(LLMCallError):
        await provider.complete(system="s", user="u")


async def test_network_error_becomes_a_typed_llm_error():
    import httpx

    provider = _provider(post_raises=httpx.ConnectError("connection reset"))
    with pytest.raises(LLMCallError, match="connection reset"):
        await provider.complete(system="s", user="u")
