"""P12 parity: the local-Qwen generation profile in ``_build_kwargs``.

Ported from production ``tests/providers/test_extra_body_config.py``
(``TestLocalQwenThinkingMode``, feat/shared-rooms @ cfccc2a2 + d4ccb836),
adapted to 0.3.0's ``_build_kwargs``.

The Spark serves Qwen from a local vLLM behind an admission gateway. The
0.2.x line pins the thinking switch per turn through
``chat_template_kwargs.enable_thinking`` and Qwen's recommended sampling per
mode, and keeps earlier assistant reasoning in context through
``preserve_thinking`` (the chat template consumes ``reasoning_content`` from
older turns). Locally those requests default to non-thinking mode and
communicate the mode with ``chat_template_kwargs`` on the wire, so the wire
``reasoning_effort`` field is suppressed for the deployment: leaving it in
sends an unknown field to a server that does not implement it and lets the
configured deployment default decide thinking, which is whatever the template
was built with -- not an explicit per-request choice.
"""

from __future__ import annotations

from typing import Any

import pytest

from nanobot.providers.openai_compat_provider import OpenAICompatProvider
from nanobot.providers.registry import find_by_name

# What deployed Ziggy actually sends (main config and all three tenants): the
# admission gateway in front of the local vLLM, not the vLLM proxy port, and
# the gateway-facing model name, not the raw vLLM served id.
SPARK_BASE = "http://127.0.0.1:8013/v1"
# The name every deployed tenant puts on the wire; the preserve gate is a
# 3.6-only string check (prod d4ccb836), and this name matches it, so the
# deployed model does receive the preserve key.
DEPLOYED_MODEL = "qwen3.6-35b"
# Prod's canonical local name from the ported tests' fixture; the deployment
# above uses it, so the profile tests exercise the real served name.
QWEN_36_MODEL = DEPLOYED_MODEL
# A Qwen name outside the 3.6 gate, used only as the negative control for the
# preserve gate; it is not what this box serves.
NON_36_MODEL = "qwen3.8-flash-next"

_CALLER_TEMPERATURE = 0.1  # caller default; the profile must override it


def _make_local_qwen(
    extra_body: dict[str, Any] | None = None,
    model: str = QWEN_36_MODEL,
    base: str = SPARK_BASE,
    spec_name: str = "custom",
) -> OpenAICompatProvider:
    return OpenAICompatProvider(
        api_key="local-placeholder",
        api_base=base,
        default_model=model,
        spec=find_by_name(spec_name),
        extra_body=extra_body,
    )


def _simple_messages() -> list[dict[str, Any]]:
    return [{"role": "user", "content": "hello"}]


def _tools() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": "lookup",
                "description": "Look up a value",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]


def _build(
    provider: OpenAICompatProvider,
    *,
    tools: list[dict[str, Any]] | None = None,
    max_tokens: int = 8192,
    reasoning_effort: str | None = None,
) -> dict[str, Any]:
    return provider._build_kwargs(
        messages=_simple_messages(),
        tools=tools,
        model=None,
        max_tokens=max_tokens,
        temperature=_CALLER_TEMPERATURE,
        reasoning_effort=reasoning_effort,
        tool_choice=None,
    )


class TestLocalQwenProfile:
    """The ported prod contract: effort-graded template switch, pinned
    sampling, preserved reasoning, and no wire ``reasoning_effort``."""

    @pytest.mark.parametrize("effort", [None, "none", "minimal"])
    def test_low_effort_keeps_thinking_off_with_non_thinking_sampling(
        self, effort: str | None
    ) -> None:
        """Low-end effort: the template switch is explicitly off and the
        non-thinking sampling profile applies (prod's
        test_default_is_explicitly_non_thinking)."""
        kwargs = _build(_make_local_qwen(), reasoning_effort=effort)

        assert kwargs["extra_body"]["chat_template_kwargs"] == {
            "enable_thinking": False,
            "preserve_thinking": True,
        }
        assert kwargs["temperature"] == 0.7
        assert kwargs["top_p"] == 0.8
        assert kwargs["presence_penalty"] == 1.5
        assert kwargs["extra_body"]["top_k"] == 20
        assert kwargs["extra_body"]["min_p"] == 0
        assert kwargs["extra_body"]["repetition_penalty"] == 1.0
        assert kwargs["max_tokens"] == 8192
        assert kwargs["model"] == QWEN_36_MODEL
        assert "reasoning_effort" not in kwargs

    def test_static_config_cannot_leave_thinking_undecided(self) -> None:
        """A configured ``enable_thinking: false`` must not survive as an
        implicit default: the profile recomputes the switch from the effort,
        exactly as prod's merge-then-override order does."""
        kwargs = _build(
            _make_local_qwen({"chat_template_kwargs": {"enable_thinking": False}}),
            reasoning_effort=None,
        )

        assert kwargs["extra_body"]["chat_template_kwargs"] == {
            "enable_thinking": False,
            "preserve_thinking": True,
        }

    @pytest.mark.parametrize("effort", ["low", "medium", "high"])
    def test_thinking_effort_enables_template_and_preserves_reasoning(
        self, effort: str
    ) -> None:
        """Low/medium/high effort: thinking on, thinking-mode sampling, and
        preserve_thinking keeps earlier reasoning in context (prod's
        test_per_request_thinking_overrides_static_default, which also proves
        the per-request profile wins over a stale static False). Production
        turns thinking off only for None/none/minimal, so "low" is on."""
        kwargs = _build(
            _make_local_qwen({"chat_template_kwargs": {"enable_thinking": False}}),
            tools=_tools(),
            max_tokens=16384,
            reasoning_effort=effort,
        )

        assert kwargs["extra_body"]["chat_template_kwargs"] == {
            "enable_thinking": True,
            "preserve_thinking": True,
        }
        assert kwargs["temperature"] == 1.0
        assert kwargs["top_p"] == 0.95
        assert kwargs["presence_penalty"] == 1.5
        assert kwargs["extra_body"]["top_k"] == 20
        assert kwargs["extra_body"]["min_p"] == 0
        assert kwargs["extra_body"]["repetition_penalty"] == 1.0
        assert kwargs["tools"] == _tools()
        assert kwargs["tool_choice"] == "auto"
        assert kwargs["max_tokens"] == 16384
        assert "reasoning_effort" not in kwargs

    @pytest.mark.parametrize("effort", ["max", "xhigh"])
    def test_precise_coding_profile_pins_qwen_recommended_sampling(
        self, effort: str
    ) -> None:
        """Max/high effort: Qwen's recommended coding sampling exactly (prod's
        test_precise_coding_profile_uses_qwen_recommended_sampling)."""
        kwargs = _build(_make_local_qwen(), max_tokens=32768, reasoning_effort=effort)

        assert kwargs["temperature"] == 0.6
        assert kwargs["top_p"] == 0.95
        assert kwargs["presence_penalty"] == 0.0
        assert kwargs["max_tokens"] == 32768
        assert kwargs["extra_body"] == {
            "top_k": 20,
            "min_p": 0,
            "repetition_penalty": 1.0,
            "chat_template_kwargs": {
                "enable_thinking": True,
                "preserve_thinking": True,
            },
        }
        assert "reasoning_effort" not in kwargs

    def test_preserve_thinking_can_be_disabled_for_canary_rollback(self) -> None:
        """A configured ``preserve_thinking: false`` must win over the profile
        default (prod's canary-rollback test; d4ccb836 escape hatch)."""
        kwargs = _build(
            _make_local_qwen({"chat_template_kwargs": {"preserve_thinking": False}}),
            reasoning_effort="high",
        )

        assert kwargs["extra_body"]["chat_template_kwargs"] == {
            "enable_thinking": True,
            "preserve_thinking": False,
        }

    def test_repeated_builds_do_not_bleed_into_the_shared_config(self) -> None:
        """The provider keeps its configured extra_body across calls; the
        profile must merge into fresh containers instead of writing through
        the shared reference, so a profile value never leaks into the config
        itself or across requests."""
        config = {"chat_template_kwargs": {"preserve_thinking": False}}
        provider = _make_local_qwen(config)
        first = _build(provider, reasoning_effort="high")
        second = _build(provider, reasoning_effort="high")

        assert first["extra_body"]["chat_template_kwargs"] == {
            "enable_thinking": True,
            "preserve_thinking": False,
        }
        assert second["extra_body"]["chat_template_kwargs"] == {
            "enable_thinking": True,
            "preserve_thinking": False,
        }
        assert config == {"chat_template_kwargs": {"preserve_thinking": False}}
        assert provider._extra_body == {"chat_template_kwargs": {"preserve_thinking": False}}

    def test_non_3_6_qwen_name_gets_no_preserve_gate(self) -> None:
        """A Qwen name outside the 3.6 string gate (a future 3.8 serve) gets
        the per-effort switch and sampling, but the preserve gate is prod's
        3.6-only check, so it must not receive the preserve key. Pinning the
        gate; widening it is an open owner question (prod has the same gap)."""
        kwargs = _build(_make_local_qwen(model=NON_36_MODEL), reasoning_effort="high")

        assert kwargs["extra_body"]["chat_template_kwargs"] == {"enable_thinking": True}
        assert "preserve_thinking" not in kwargs["extra_body"]["chat_template_kwargs"]
        assert kwargs["temperature"] == 1.0
        assert kwargs["top_p"] == 0.95
        assert kwargs["extra_body"]["top_k"] == 20
        assert "reasoning_effort" not in kwargs

    def test_deployed_model_at_gateway_base_gets_preserve(self) -> None:
        """The real deployment: the name and admission-gateway base that
        deployed Ziggy actually sends (main config plus all three tenants) sit
        behind the local gate, so the served 3.6 model gets the preserved
        reasoning key on the wire -- the whole point of the profile. If the
        deployment constants and the gate ever drift apart, this is the test
        that goes red."""
        assert _make_local_qwen().api_base == SPARK_BASE
        assert _make_local_qwen().default_model == DEPLOYED_MODEL

        kwargs = _build(_make_local_qwen(), reasoning_effort="high")

        assert kwargs["model"] == DEPLOYED_MODEL
        assert kwargs["extra_body"]["chat_template_kwargs"] == {
            "enable_thinking": True,
            "preserve_thinking": True,
        }
        assert kwargs["temperature"] == 1.0
        assert kwargs["top_p"] == 0.95
        assert "reasoning_effort" not in kwargs


class TestNonLocalQwenUntouched:
    """Negative controls: the profile must fire only on the local-Qwen
    conjunction, never on hosted, non-Qwen, or non-custom traffic."""

    def test_hosted_qwen_slug_keeps_wire_effort_and_no_profile(self) -> None:
        """A qwen slug on a hosted endpoint (no local base) is not the local
        deployment: no profile keys, the wire reasoning_effort field stays
        intact, and the hosted thinking-style control (top-level
        ``enable_thinking``, not ``chat_template_kwargs``) still applies."""
        provider = OpenAICompatProvider(
            api_key="sk-test",
            default_model="qwen3.6-flash",
        )
        kwargs = _build(provider, reasoning_effort="high")

        extra_body = kwargs.get("extra_body", {})
        assert "chat_template_kwargs" not in extra_body
        assert "top_k" not in extra_body
        assert "min_p" not in extra_body
        assert "repetition_penalty" not in extra_body
        assert kwargs["extra_body"] == {"enable_thinking": True}
        assert "top_p" not in kwargs
        assert "presence_penalty" not in kwargs
        assert kwargs["reasoning_effort"] == "high"

    @pytest.mark.parametrize("model", ["gpt-4o", "llama-3.3-70b"])
    def test_local_non_qwen_model_gets_no_profile(self, model: str) -> None:
        """A non-Qwen model on the same local deployment must not gain the
        profile (the detection is a conjunction, not an endpoint check)."""
        kwargs = _build(_make_local_qwen(model=model), reasoning_effort="high")

        assert "extra_body" not in kwargs
        assert "top_p" not in kwargs
        assert "presence_penalty" not in kwargs
        assert "temperature" not in kwargs  # caller 0.1 dropped by the gate;
        # a misfired profile would have pinned 1.0 here
        assert kwargs["reasoning_effort"] == "high"

    def test_local_qwen_under_non_custom_spec_is_not_treated_local(self) -> None:
        """Detection requires the custom spec; a loopback base alone must not
        trigger the profile (the third spec conjunct)."""
        kwargs = _build(
            _make_local_qwen(spec_name="openai"), reasoning_effort="high"
        )

        assert "chat_template_kwargs" not in kwargs.get("extra_body", {})
        assert "top_p" not in kwargs
        assert kwargs["reasoning_effort"] == "high"

    def test_qwen_under_custom_spec_on_remote_base_is_not_local(self) -> None:
        """A public hostname under the custom spec stays remote: the profile
        would misfire and send a private header plus template kwargs to an
        upstream that does not implement them."""
        provider = OpenAICompatProvider(
            api_key="sk-test",
            api_base="https://infer.example.com/v1",
            default_model=QWEN_36_MODEL,
            spec=find_by_name("custom"),
        )
        kwargs = _build(provider, reasoning_effort="high")

        assert "chat_template_kwargs" not in kwargs.get("extra_body", {})
        assert "top_p" not in kwargs
        assert "presence_penalty" not in kwargs
        assert kwargs["reasoning_effort"] == "high"
