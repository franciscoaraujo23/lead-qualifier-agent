"""AnthropicProvider tests — offline. The real SDK client is constructed (so its
configuration is checked for real), but the network call is replaced.

The rest of the suite runs on the mock provider, so without this file the only
code that talks to a real model is never executed by any test.
"""

from types import SimpleNamespace

import pytest

pytest.importorskip("anthropic", reason="anthropic extra not installed")

from lead_qualifier.errors import LLMCallError, LLMRefusalError  # noqa: E402
from lead_qualifier.llm.anthropic_provider import AnthropicProvider  # noqa: E402


def _provider(response=None, raises=None) -> AnthropicProvider:
    provider = AnthropicProvider("test-key", "claude-haiku-4-5", timeout_s=20.0)

    async def fake_create(**kwargs):
        fake_create.kwargs = kwargs
        if raises:
            raise raises
        return response

    provider._client.messages.create = fake_create
    provider.fake_create = fake_create
    return provider


def _response(blocks, stop_reason="end_turn", input_tokens=120, output_tokens=45):
    return SimpleNamespace(
        content=blocks,
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
    )


def test_client_has_the_time_budget_and_no_hidden_retries():
    """The two settings §4.2 and §4.8 depend on. SDK defaults are 10 minutes and
    2 retries, which would silently break the n8n timeout arithmetic."""
    provider = AnthropicProvider("test-key", "claude-haiku-4-5", timeout_s=20.0)
    assert provider._client.timeout == 20.0
    assert provider._client.max_retries == 0


async def test_joins_text_blocks_and_reports_usage():
    provider = _provider(
        _response(
            [
                SimpleNamespace(type="text", text='{"score": 7'),
                SimpleNamespace(type="text", text="1}"),
            ]
        )
    )
    completion = await provider.complete(system="sys", user="profile", max_tokens=512)

    assert completion.text == '{"score": 71}'
    assert (completion.usage.input_tokens, completion.usage.output_tokens) == (120, 45)
    assert completion.usage.model == "claude-haiku-4-5", "cost lookup keys on this id"

    sent = provider.fake_create.kwargs
    assert sent["model"] == "claude-haiku-4-5"
    assert sent["system"] == "sys"
    assert sent["max_tokens"] == 512
    assert sent["messages"] == [{"role": "user", "content": "profile"}]


async def test_ignores_non_text_blocks():
    provider = _provider(
        _response(
            [
                SimpleNamespace(type="thinking", thinking="..."),
                SimpleNamespace(type="text", text="{}"),
            ]
        )
    )
    assert (await provider.complete(system="s", user="u")).text == "{}"


async def test_refusal_is_a_non_retryable_typed_error():
    provider = _provider(_response([], stop_reason="refusal"))
    with pytest.raises(LLMRefusalError) as exc:
        await provider.complete(system="s", user="u")
    assert exc.value.http_status == 422, "n8n only retries status 0 and 5xx"
    assert exc.value.failure_stage == "llm"


async def test_sdk_errors_become_typed_llm_errors():
    provider = _provider(raises=RuntimeError("connection reset"))
    with pytest.raises(LLMCallError, match="connection reset"):
        await provider.complete(system="s", user="u")
