"""The admission gateway's background cap rides on a request header.

Ziggy puts an admission gateway in front of the local vLLM. It caps background
concurrency separately from foreground chat so a long Work run cannot take
every vLLM slot and stall an interactive turn. The gateway reads the class from
``X-Ziggy-Scheduling-Class``; if the header stops being sent, every request
looks identical to it and the cap silently stops separating anything. Nothing
raises -- the turn just gets slow -- so these tests are the only thing standing
between that regression and production.
"""

from __future__ import annotations

from unittest.mock import patch

from nanobot.providers.openai_compat_provider import OpenAICompatProvider
from nanobot.providers.registry import find_by_name
from nanobot.providers.request_context import (
    reset_scheduling_class,
    set_scheduling_class,
)

HEADER = "X-Ziggy-Scheduling-Class"


def _local_qwen_provider() -> OpenAICompatProvider:
    """A provider shaped like the Spark: custom spec, qwen model, loopback."""
    spec = find_by_name("custom")
    with patch("nanobot.providers.openai_compat_provider.AsyncOpenAI"):
        return OpenAICompatProvider(
            api_key="local",
            api_base="http://127.0.0.1:8013/v1",
            default_model="qwen3.8-flash-next",
            spec=spec,
        )


def _kwargs(provider: OpenAICompatProvider) -> dict:
    return provider._build_kwargs(
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        model="qwen3.8-flash-next",
        max_tokens=256,
        temperature=0.7,
        reasoning_effort=None,
        tool_choice=None,
    )


def test_foreground_is_the_default_class() -> None:
    provider = _local_qwen_provider()
    assert _kwargs(provider)["extra_headers"][HEADER] == "foreground"


def test_a_bound_background_class_reaches_the_header() -> None:
    provider = _local_qwen_provider()
    token = set_scheduling_class("background")
    try:
        assert _kwargs(provider)["extra_headers"][HEADER] == "background"
    finally:
        reset_scheduling_class(token)


def test_the_class_is_restored_after_the_run() -> None:
    """A background Work run must not leave later chat turns marked background."""
    provider = _local_qwen_provider()
    token = set_scheduling_class("background")
    reset_scheduling_class(token)
    assert _kwargs(provider)["extra_headers"][HEADER] == "foreground"


def test_the_private_header_is_not_sent_to_third_party_providers() -> None:
    """It is a Ziggy-private header; upstreams have no use for it."""
    spec = find_by_name("openai")
    with patch("nanobot.providers.openai_compat_provider.AsyncOpenAI"):
        provider = OpenAICompatProvider(
            api_key="sk-test",
            default_model="gpt-4o",
            spec=spec,
        )
    kwargs = provider._build_kwargs(
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        model="gpt-4o",
        max_tokens=256,
        temperature=0.7,
        reasoning_effort=None,
        tool_choice=None,
    )
    assert HEADER not in kwargs.get("extra_headers", {})


def test_a_remote_qwen_endpoint_does_not_get_the_header() -> None:
    """Guard the guard: 'qwen in the model name' alone must not be enough,
    or the header leaks to any hosted Qwen provider."""
    spec = find_by_name("custom")
    with patch("nanobot.providers.openai_compat_provider.AsyncOpenAI"):
        provider = OpenAICompatProvider(
            api_key="k",
            api_base="https://api.example.com/v1",
            default_model="qwen3.8-flash-next",
            spec=spec,
        )
    kwargs = provider._build_kwargs(
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        model="qwen3.8-flash-next",
        max_tokens=256,
        temperature=0.7,
        reasoning_effort=None,
        tool_choice=None,
    )
    assert HEADER not in kwargs.get("extra_headers", {})
