"""Focused tests for request-scoped JSON Schema generation."""

from __future__ import annotations

import asyncio
import copy
from typing import Any
from unittest.mock import AsyncMock

import pytest

from nanobot.providers.base import LLMProvider, LLMResponse
from nanobot.providers.openai_compat_provider import OpenAICompatProvider
from nanobot.providers.structured_output import (
    JSONSchemaOutput,
    LLMRequestOptions,
    MalformedStructuredOutputError,
    StructuredOutputValidationError,
    UnsupportedStructuredOutputProviderError,
    generate_structured,
)

_PROFILE_SCHEMA = {
    "type": "object",
    "properties": {
        "profile": {"enum": ["fast", "think"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": ["profile", "confidence"],
    "additionalProperties": False,
}


def _output(name: str = "reasoning_profile") -> JSONSchemaOutput:
    return JSONSchemaOutput(name=name, schema=_PROFILE_SCHEMA, strict=True)


def _provider(extra_body: dict[str, Any] | None = None) -> OpenAICompatProvider:
    return OpenAICompatProvider(
        api_key="test-key",
        api_base="http://127.0.0.1:8001/v1",
        default_model="test-model",
        extra_body=extra_body,
    )


def _request_kwargs(
    provider: OpenAICompatProvider,
    options: LLMRequestOptions | None = None,
) -> dict[str, Any]:
    return provider._build_kwargs(
        messages=[{"role": "user", "content": "classify"}],
        tools=None,
        model=None,
        max_tokens=128,
        temperature=0,
        reasoning_effort=None,
        tool_choice=None,
        request_options=options,
    )


def test_schema_propagates_using_standard_response_format() -> None:
    output = _output()
    provider = _provider()

    kwargs = _request_kwargs(
        provider,
        LLMRequestOptions(structured_output=output),
    )

    assert provider.supports_structured_output is True
    assert kwargs["response_format"] == {
        "type": "json_schema",
        "json_schema": {
            "name": "reasoning_profile",
            "schema": _PROFILE_SCHEMA,
            "strict": True,
        },
    }


def test_responses_api_receives_equivalent_text_format() -> None:
    provider = _provider()

    body = provider._build_responses_body(
        messages=[{"role": "user", "content": "classify"}],
        tools=None,
        model=None,
        max_tokens=128,
        temperature=0,
        reasoning_effort=None,
        tool_choice=None,
        request_options=LLMRequestOptions(structured_output=_output()),
    )

    assert body["text"]["format"] == {
        "type": "json_schema",
        "name": "reasoning_profile",
        "schema": _PROFILE_SCHEMA,
        "strict": True,
    }


def test_per_request_schema_overrides_only_static_decoding_controls() -> None:
    legacy_schema = {"type": "object", "properties": {"legacy": {"type": "boolean"}}}
    configured = {
        "guided_json": legacy_schema,
        "chat_template_kwargs": {"custom_flag": True},
        "repetition_penalty": 1.1,
    }
    provider = _provider(configured)

    structured = _request_kwargs(
        provider,
        LLMRequestOptions(structured_output=_output()),
    )
    ordinary_afterward = _request_kwargs(provider)

    assert structured["extra_body"] == {
        "chat_template_kwargs": {"custom_flag": True},
        "repetition_penalty": 1.1,
    }
    assert structured["response_format"]["json_schema"]["schema"] == _PROFILE_SCHEMA
    assert ordinary_afterward["extra_body"] == configured
    assert "response_format" not in ordinary_afterward
    assert provider._extra_body == configured


def test_schema_snapshot_does_not_alias_caller_data() -> None:
    source = copy.deepcopy(_PROFILE_SCHEMA)
    output = JSONSchemaOutput(name="snapshot", schema=source)

    source["properties"]["profile"]["enum"].append("mutated")
    first_copy = output.schema
    first_copy["properties"]["profile"]["enum"].append("also-mutated")

    assert output.schema == _PROFILE_SCHEMA


@pytest.mark.asyncio
async def test_concurrent_requests_keep_schemas_isolated() -> None:
    provider = _provider()
    calls: dict[str, dict[str, Any]] = {}

    async def create(**kwargs: Any) -> dict[str, Any]:
        prompt = kwargs["messages"][0]["content"]
        calls[prompt] = copy.deepcopy(kwargs)
        await asyncio.sleep(0)
        return {
            "choices": [
                {
                    "message": {"content": '{"profile":"fast","confidence":1}'},
                    "finish_reason": "stop",
                }
            ]
        }

    provider._client.chat.completions.create = AsyncMock(side_effect=create)
    alpha = JSONSchemaOutput(
        name="alpha",
        schema={"type": "object", "properties": {"alpha": {"type": "string"}}},
    )
    beta = JSONSchemaOutput(
        name="beta",
        schema={"type": "object", "properties": {"beta": {"type": "integer"}}},
    )

    await asyncio.gather(
        provider.chat(
            messages=[{"role": "user", "content": "alpha"}],
            request_options=LLMRequestOptions(structured_output=alpha),
        ),
        provider.chat(
            messages=[{"role": "user", "content": "beta"}],
            request_options=LLMRequestOptions(structured_output=beta),
        ),
        provider.chat(messages=[{"role": "user", "content": "ordinary"}]),
    )

    assert calls["alpha"]["response_format"]["json_schema"]["name"] == "alpha"
    assert calls["beta"]["response_format"]["json_schema"]["name"] == "beta"
    assert "response_format" not in calls["ordinary"]


class _ScriptedProvider(LLMProvider):
    supports_structured_output = True

    def __init__(self, response: LLMResponse):
        super().__init__()
        self.response = response
        self.last_kwargs: dict[str, Any] = {}

    async def chat(self, **kwargs: Any) -> LLMResponse:
        self.last_kwargs = kwargs
        return self.response

    def get_default_model(self) -> str:
        return "test-model"


class _UnsupportedProvider(LLMProvider):
    def __init__(self):
        super().__init__()
        self.called = False

    async def chat(self, **kwargs: Any) -> LLMResponse:
        self.called = True
        raise AssertionError("unsupported provider must not be called")

    def get_default_model(self) -> str:
        return "unsupported"


class _RetryingStructuredProvider(LLMProvider):
    supports_structured_output = True

    def __init__(self, responses: list[LLMResponse]):
        super().__init__()
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def chat(self, **kwargs: Any) -> LLMResponse:
        self.calls.append(kwargs)
        return self.responses.pop(0)

    def get_default_model(self) -> str:
        return "test-model"


@pytest.mark.asyncio
async def test_generate_structured_rejects_unsupported_provider_before_call() -> None:
    provider = _UnsupportedProvider()

    with pytest.raises(
        UnsupportedStructuredOutputProviderError,
        match="_UnsupportedProvider does not support",
    ):
        await generate_structured(
            provider,
            messages=[{"role": "user", "content": "classify"}],
            output=_output(),
        )

    assert provider.supports_structured_output is False
    assert provider.called is False


@pytest.mark.asyncio
async def test_generate_structured_returns_validated_json() -> None:
    provider = _ScriptedProvider(
        LLMResponse(content='{"profile":"think","confidence":0.75}')
    )

    result = await generate_structured(
        provider,
        messages=[{"role": "user", "content": "classify"}],
        output=_output(),
        max_tokens=128,
        temperature=0,
    )

    assert result.value == {"profile": "think", "confidence": 0.75}
    options = provider.last_kwargs["request_options"]
    assert options.structured_output.name == "reasoning_profile"


@pytest.mark.asyncio
async def test_request_options_survive_retry_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _RetryingStructuredProvider([
        LLMResponse(content="429 rate limit", finish_reason="error"),
        LLMResponse(content='{"profile":"fast","confidence":1}'),
    ])

    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr("nanobot.providers.base.asyncio.sleep", no_sleep)

    result = await generate_structured(
        provider,
        messages=[{"role": "user", "content": "classify"}],
        output=_output(),
    )

    assert result.value == {"profile": "fast", "confidence": 1}
    assert len(provider.calls) == 2
    first_options = provider.calls[0]["request_options"]
    second_options = provider.calls[1]["request_options"]
    assert first_options is second_options
    assert first_options.structured_output.chat_completions_format() == (
        second_options.structured_output.chat_completions_format()
    )


@pytest.mark.asyncio
async def test_generate_structured_rejects_malformed_json() -> None:
    provider = _ScriptedProvider(LLMResponse(content="```json\n{}\n```"))

    with pytest.raises(MalformedStructuredOutputError, match="line 1, column 1"):
        await generate_structured(
            provider,
            messages=[{"role": "user", "content": "classify"}],
            output=_output(),
        )


@pytest.mark.asyncio
async def test_generate_structured_rejects_schema_violation() -> None:
    provider = _ScriptedProvider(
        LLMResponse(content='{"profile":"unknown","extra":true}')
    )

    with pytest.raises(StructuredOutputValidationError) as exc_info:
        await generate_structured(
            provider,
            messages=[{"role": "user", "content": "classify"}],
            output=_output(),
        )

    errors = exc_info.value.errors
    assert any("$.profile" in error and "not one of" in error for error in errors)
    assert any("confidence" in error and "required" in error for error in errors)
    assert any("Additional properties" in error for error in errors)
