"""Tests for TTFT (time-to-first-token) capture in OpenAICompatProvider streaming.

MIT-185 / MIT-144: ttft_ms was always None because the custom provider's _stream()
(which had correct TTFT instrumentation) is dead code — config "custom" routes via
registry.py -> backend="openai_compat" -> OpenAICompatProvider, not CustomProvider.
These tests verify that OpenAICompatProvider.chat_stream() correctly records ttft_ms.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nanobot.providers.openai_compat_provider import OpenAICompatProvider


def _make_chunk(*, content=None, reasoning_content=None, reasoning=None, tool_calls=None, finish_reason=None):
    """Build a minimal streaming chunk SimpleNamespace matching the OpenAI SDK shape."""
    delta = SimpleNamespace(
        content=content,
        reasoning_content=reasoning_content,
        reasoning=reasoning,
        tool_calls=tool_calls,
    )
    choice = SimpleNamespace(delta=delta, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice], usage=None)


def _make_usage_chunk():
    """Final chunk that carries usage but no choices."""
    usage = SimpleNamespace(
        prompt_tokens=10, completion_tokens=5, total_tokens=15
    )
    return SimpleNamespace(choices=[], usage=usage)


async def _async_iter(items):
    """Turn a plain list into an async iterator."""
    for item in items:
        yield item


def _make_provider():
    with patch("nanobot.providers.openai_compat_provider.AsyncOpenAI"):
        return OpenAICompatProvider()


# ---------------------------------------------------------------------------
# chat_stream() TTFT tests
# ---------------------------------------------------------------------------

async def test_chat_stream_records_ttft_ms_on_first_content_chunk():
    """First chunk with delta.content sets ttft_ms to a positive float."""
    provider = _make_provider()
    chunks = [
        _make_chunk(content="Hello"),
        _make_chunk(content=" world", finish_reason="stop"),
        _make_usage_chunk(),
    ]

    mock_stream = MagicMock()
    mock_stream.__aiter__ = MagicMock(return_value=_async_iter(chunks))

    async def fake_create(**kwargs):
        return mock_stream

    provider._client.chat.completions.create = fake_create
    # Disable Responses API path
    provider._should_use_responses_api = MagicMock(return_value=False)

    result = await provider.chat_stream(
        messages=[{"role": "user", "content": "hi"}],
    )

    assert result.ttft_ms is not None
    assert isinstance(result.ttft_ms, float)
    assert result.ttft_ms > 0
    assert result.content == "Hello world"


async def test_chat_stream_ttft_none_on_empty_stream():
    """Stream with no content-carrying chunks leaves ttft_ms as None."""
    provider = _make_provider()
    # Only a usage chunk — no actual content
    chunks = [_make_usage_chunk()]

    mock_stream = MagicMock()
    mock_stream.__aiter__ = MagicMock(return_value=_async_iter(chunks))

    async def fake_create(**kwargs):
        return mock_stream

    provider._client.chat.completions.create = fake_create
    provider._should_use_responses_api = MagicMock(return_value=False)

    result = await provider.chat_stream(
        messages=[{"role": "user", "content": "hi"}],
    )

    assert result.ttft_ms is None


async def test_chat_stream_ttft_set_on_first_delta_regardless_of_content_vs_tool():
    """ttft_ms is captured when the first delta carries tool_calls, not just text."""
    provider = _make_provider()

    # First chunk: tool call delta only (no text content)
    tool_call_delta = SimpleNamespace(
        index=0,
        id="call_abc",
        function=SimpleNamespace(name="my_tool", arguments=""),
    )
    first_chunk = _make_chunk(tool_calls=[tool_call_delta])
    # Second chunk: tool call arguments
    tool_arg_delta = SimpleNamespace(
        index=0,
        id=None,
        function=SimpleNamespace(name=None, arguments='{"x": 1}'),
    )
    second_chunk = _make_chunk(tool_calls=[tool_arg_delta], finish_reason="tool_calls")
    chunks = [first_chunk, second_chunk, _make_usage_chunk()]

    mock_stream = MagicMock()
    mock_stream.__aiter__ = MagicMock(return_value=_async_iter(chunks))

    async def fake_create(**kwargs):
        return mock_stream

    provider._client.chat.completions.create = fake_create
    provider._should_use_responses_api = MagicMock(return_value=False)

    result = await provider.chat_stream(
        messages=[{"role": "user", "content": "hi"}],
    )

    assert result.ttft_ms is not None
    assert isinstance(result.ttft_ms, float)
    assert result.ttft_ms > 0


async def test_chat_stream_ttft_set_on_stepfun_reasoning_field():
    """ttft_ms is captured when first delta carries delta.reasoning (StepFun-style)."""
    provider = _make_provider()

    # StepFun Plan API: first tokens arrive in delta.reasoning, not delta.content
    chunks = [
        _make_chunk(reasoning="Thinking step 1..."),
        _make_chunk(reasoning="step 2.", finish_reason="stop"),
        _make_usage_chunk(),
    ]

    mock_stream = MagicMock()
    mock_stream.__aiter__ = MagicMock(return_value=_async_iter(chunks))

    async def fake_create(**kwargs):
        return mock_stream

    provider._client.chat.completions.create = fake_create
    provider._should_use_responses_api = MagicMock(return_value=False)

    result = await provider.chat_stream(
        messages=[{"role": "user", "content": "hi"}],
    )

    assert result.ttft_ms is not None
    assert isinstance(result.ttft_ms, float)
    assert result.ttft_ms > 0


# ---------------------------------------------------------------------------
# _parse_chunks() propagation tests (unit-level, no async needed)
# ---------------------------------------------------------------------------

def test_parse_chunks_propagates_ttft_ms_when_provided():
    """_parse_chunks passes ttft_ms through to LLMResponse."""
    chunk = _make_chunk(content="hi", finish_reason="stop")
    result = OpenAICompatProvider._parse_chunks([chunk], ttft_ms=42.5)
    assert result.ttft_ms == pytest.approx(42.5)


def test_parse_chunks_ttft_ms_none_by_default():
    """_parse_chunks leaves ttft_ms as None when not supplied."""
    chunk = _make_chunk(content="hi", finish_reason="stop")
    result = OpenAICompatProvider._parse_chunks([chunk])
    assert result.ttft_ms is None


def test_parse_chunks_ttft_ms_zero_not_treated_as_false():
    """ttft_ms=0.0 (theoretically instant) is stored, not silently dropped."""
    chunk = _make_chunk(content="hi", finish_reason="stop")
    result = OpenAICompatProvider._parse_chunks([chunk], ttft_ms=0.0)
    assert result.ttft_ms == 0.0
