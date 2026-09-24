"""Reasoning profile policy (ported from production feat/shared-rooms).

Unit tests for ``nanobot.agent.reasoning_policy`` plus integration tests
that drive the full ``AgentLoop._run_agent_loop`` with a stub provider
recording the generation it receives (MIT-1409).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from nanobot.agent.context import TranscriptInput
from nanobot.agent.loop import AgentLoop, _reasoning_profile_messages
from nanobot.agent.reasoning_policy import (
    ReasoningProfile,
    escalation_profile,
    generation_profile,
    parse_reasoning_profile,
    resolve_reasoning_profile,
)
from nanobot.agent.tools.context import RequestContext
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import GenerationSettings, LLMResponse
from nanobot.utils.llm_runtime import LLMRuntime

_DEFAULT_EFFORT = "low"  # the provider's configured default in these tests


def _messages(*texts: str) -> list[dict[str, Any]]:
    return [{"role": "user", "content": text} for text in texts]


# ---------------------------------------------------------------------------
# Policy: explicit profiles win, Auto is the default, unknown -> None/auto.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("requested", "name", "effort", "temperature", "max_tokens"),
    [
        ("fast", ReasoningProfile.FAST, "none", 0.7, 8_192),
        ("deep", ReasoningProfile.DEEP, "high", 1.0, 32_768),
        ("think", ReasoningProfile.THINK, "high", 1.0, 32_768),
        ("think-code", ReasoningProfile.THINK_CODE, "max", 0.6, 32_768),
    ],
)
def test_explicit_profiles_map_to_production_controls(
    requested: str,
    name: ReasoningProfile,
    effort: str,
    temperature: float,
    max_tokens: int,
) -> None:
    decision = resolve_reasoning_profile(requested, _messages("hello"))

    assert decision.requested is ReasoningProfile(requested)
    assert decision.generation.name is name
    assert decision.generation.reasoning_effort == effort
    assert decision.generation.temperature == temperature
    assert decision.generation.max_tokens == max_tokens
    assert decision.source == "explicit"
    assert decision.classifier_candidate is False
    # Escalation is reserved for the Auto path; explicit choices are honored.
    assert decision.allow_escalation is False


def test_fast_requests_low_or_no_reasoning() -> None:
    assert resolve_reasoning_profile("fast", _messages("hi")).generation.reasoning_effort == "none"


def test_deep_requests_high_reasoning() -> None:
    assert resolve_reasoning_profile("deep", _messages("hi")).generation.reasoning_effort == "high"


def test_auto_is_the_default_and_requests_the_default_effort() -> None:
    # Auto resolves via the rules; the loop leaves the provider's default
    # effort in place for Auto turns (asserted in the loop tests below).
    decision = resolve_reasoning_profile("auto", _messages("what is the weather today"))
    assert decision.requested is ReasoningProfile.AUTO
    assert decision.source == "rules:fallback"
    assert decision.classifier_candidate is True
    assert decision.allow_escalation is True


def test_unknown_profile_is_unsupported_and_stays_auto() -> None:
    assert parse_reasoning_profile("bogus") is None
    with pytest.raises(ValueError):
        resolve_reasoning_profile("bogus", _messages("hi"))


def test_parses_profiles_case_insensitively_with_surrounding_space() -> None:
    assert parse_reasoning_profile("  FAST ") is ReasoningProfile.FAST
    assert parse_reasoning_profile("Deep") is ReasoningProfile.DEEP
    assert parse_reasoning_profile(ReasoningProfile.THINK) is ReasoningProfile.THINK
    assert parse_reasoning_profile(None) is None
    assert parse_reasoning_profile(123) is None


def test_generation_profile_rejects_unresolved_auto() -> None:
    with pytest.raises(ValueError):
        generation_profile(ReasoningProfile.AUTO)
    assert generation_profile("deep").reasoning_effort == "high"


# ---------------------------------------------------------------------------
# Policy: Auto deterministic rules (no network calls).
# ---------------------------------------------------------------------------


def test_auto_routes_code_to_precise_thinking() -> None:
    decision = resolve_reasoning_profile("auto", _messages("review the api code"))
    assert decision.generation.name is ReasoningProfile.THINK_CODE
    assert decision.source == "rule:code"


def test_auto_routes_background_work_to_thinking() -> None:
    decision = resolve_reasoning_profile(
        "auto", _messages("check on my running jobs"), background_work=True
    )
    assert decision.generation.name is ReasoningProfile.THINK
    assert decision.source == "rule:background-work"


def test_auto_routes_complex_marker_to_thinking() -> None:
    decision = resolve_reasoning_profile("auto", _messages("analyze the tradeoff"))
    assert decision.generation.name is ReasoningProfile.THINK
    assert decision.source == "rule:complex"


def test_auto_routes_greeting_prefix_to_fast() -> None:
    decision = resolve_reasoning_profile("auto", _messages("hello there"))
    assert decision.generation.name is ReasoningProfile.FAST
    assert decision.source == "rule:fast"


def test_auto_routes_media_to_fast() -> None:
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "what is shown here"},
                {"type": "image_url", "image_url": {"url": "https://example.test/a.png"}},
            ],
        }
    ]
    decision = resolve_reasoning_profile("auto", messages)
    assert decision.generation.name is ReasoningProfile.FAST
    assert decision.source == "rule:media"


def test_auto_classifies_only_the_latest_user_turn() -> None:
    messages = _messages("plan the migration refactor")
    messages.append({"role": "assistant", "content": "working on it"})
    messages.append({"role": "user", "content": "hello again"})
    decision = resolve_reasoning_profile("auto", messages)
    assert decision.generation.name is ReasoningProfile.FAST
    assert decision.source == "rule:fast"


def test_auto_matches_production_reference_classifications() -> None:
    # Verbatim inputs from production test_reasoning_policy.py (1ff35d02).
    decision = resolve_reasoning_profile(
        "auto", _messages("Debug this Swift concurrency test failure")
    )
    assert decision.generation.name is ReasoningProfile.THINK_CODE
    assert decision.source == "rule:code"

    decision = resolve_reasoning_profile(
        "auto", _messages("Read my newsletters"), background_work=True
    )
    assert decision.generation.name is ReasoningProfile.THINK
    assert decision.source == "rule:background-work"

    decision = resolve_reasoning_profile("auto", _messages("What should I do next?"))
    assert decision.generation.name is ReasoningProfile.FAST
    assert decision.source == "rules:fallback"
    assert decision.classifier_candidate is True
    assert decision.allow_escalation is True


def test_escalation_is_bounded_one_step_fast_to_think() -> None:
    escalated = escalation_profile("fast")
    assert escalated is not None
    assert escalated.name is ReasoningProfile.THINK
    assert escalation_profile("think") is None
    assert escalation_profile("think-code") is None
    assert escalation_profile("deep") is None
    assert escalation_profile("auto") is None
    assert escalation_profile("bogus") is None


# ---------------------------------------------------------------------------
# Loop adapter input (current turn + media reach the classifier view).
# ---------------------------------------------------------------------------


def test_profile_messages_prefer_caller_supplied_transcript() -> None:
    transcript = TranscriptInput(history=[{"role": "user", "content": "stale"}], current_message=None)
    supplied = _messages("current question")
    assert _reasoning_profile_messages(transcript, supplied) is supplied


def test_profile_messages_combine_history_and_current_turn() -> None:
    history = [{"role": "user", "content": "old"}]
    transcript = TranscriptInput(history=history, current_message="new message")
    result = _reasoning_profile_messages(transcript, None)
    assert [m["content"] for m in result if m["role"] == "user"] == ["old", "new message"]
    assert history == [{"role": "user", "content": "old"}]  # not mutated


def test_profile_messages_render_current_turn_media_as_blocks() -> None:
    transcript = TranscriptInput(
        history=[], current_message="describe this", media=["/tmp/a.png"]
    )
    result = _reasoning_profile_messages(transcript, None)
    content = result[-1]["content"]
    assert isinstance(content, list)
    assert {"type": "text", "text": "describe this"} in content
    assert any(block.get("type") == "image" for block in content)


# ---------------------------------------------------------------------------
# Loop integration: the resolved effort reaches the provider, per turn only.
# ---------------------------------------------------------------------------


class _LoopHarness:
    """Builds an AgentLoop over a stub provider recording provider calls."""

    def __init__(self, tmp_path: Path) -> None:
        self.calls: list[dict[str, Any]] = []
        provider = MagicMock()
        provider.get_default_model.return_value = "test-model"

        async def chat_stream_with_retry(**kwargs: Any) -> LLMResponse:
            self.calls.append(kwargs)
            return LLMResponse(content="done", tool_calls=[])

        provider.chat_stream_with_retry = chat_stream_with_retry
        self.provider = provider
        self.loop = AgentLoop(
            bus=MessageBus(),
            provider=provider,
            workspace=tmp_path,
            model="test-model",
        )
        self.runtime = LLMRuntime(
            provider=provider,
            model="test-model",
            generation=GenerationSettings(
                temperature=0.7,
                max_tokens=4096,
                reasoning_effort=_DEFAULT_EFFORT,
            ),
            context_window_tokens=200_000,
        )

    async def run_turn(
        self, metadata: dict[str, Any] | None = None, *, text: str = "hello"
    ) -> dict[str, Any]:
        await self.loop._run_agent_loop(
            TranscriptInput(history=[], current_message=text, media=[]),
            runtime=self.runtime,
            request_context=RequestContext(
                channel="test",
                chat_id="c1",
                session_key="test:c1",
                runtime=self.runtime,
                metadata=dict(metadata or {}),
            ),
        )
        return self.calls[-1]


@pytest.mark.asyncio
async def test_loop_binds_deep_profile_to_high_effort_for_the_turn(tmp_path: Path) -> None:
    harness = _LoopHarness(tmp_path)

    kwargs = await harness.run_turn({"reasoning_profile": "deep"})

    assert kwargs["reasoning_effort"] == "high"
    assert kwargs["temperature"] == 1.0
    assert kwargs["max_tokens"] == 32_768
    # The admitted runtime is a frozen value and was not mutated.
    assert harness.runtime.generation.reasoning_effort == _DEFAULT_EFFORT
    assert harness.runtime.generation.max_tokens == 4096


@pytest.mark.asyncio
async def test_loop_binds_fast_profile_to_low_effort_for_the_turn(tmp_path: Path) -> None:
    harness = _LoopHarness(tmp_path)

    kwargs = await harness.run_turn({"reasoning_profile": "fast"})

    assert kwargs["reasoning_effort"] == "none"
    assert kwargs["temperature"] == 0.7
    assert kwargs["max_tokens"] == 8_192


@pytest.mark.asyncio
async def test_loop_binds_explicit_auto_to_the_resolved_generation(tmp_path: Path) -> None:
    harness = _LoopHarness(tmp_path)

    kwargs = await harness.run_turn({"reasoning_profile": "auto"})

    # Production parity (feat/shared-rooms loop.py:1029-1039): the resolved
    # auto generation is applied, not discarded.  "hello" is a fast-prefix
    # greeting -> fast (none, 0.7, 8192), not the provider default.
    assert kwargs["reasoning_effort"] == "none"
    assert kwargs["temperature"] == 0.7
    assert kwargs["max_tokens"] == 8_192
    # The admitted runtime is a frozen value and was not mutated.
    assert harness.runtime.generation.reasoning_effort == _DEFAULT_EFFORT
    assert harness.runtime.generation.max_tokens == 4096


@pytest.mark.asyncio
async def test_loop_auto_resolves_code_text_to_think_code(tmp_path: Path) -> None:
    harness = _LoopHarness(tmp_path)

    kwargs = await harness.run_turn(
        {"reasoning_profile": "auto"}, text="please refactor this module"
    )

    # The auto rules classify the current turn's code request as think-code
    # (max), and the resolved generation reaches the provider call.
    assert kwargs["reasoning_effort"] == "max"
    assert kwargs["temperature"] == 0.6
    assert kwargs["max_tokens"] == 32_768


@pytest.mark.asyncio
async def test_loop_auto_resolves_background_work_to_think(tmp_path: Path) -> None:
    harness = _LoopHarness(tmp_path)

    # Owner-created Work jobs are always auto + background (production
    # websocket.py:1745); the background class must lift the turn to think
    # even when the prompt text carries no code marker.
    kwargs = await harness.run_turn(
        {"reasoning_profile": "auto", "work_mode": "background"}
    )

    assert kwargs["reasoning_effort"] == "high"
    assert kwargs["temperature"] == 1.0
    assert kwargs["max_tokens"] == 32_768

    # "scheduled" is in BACKGROUND_WORK_MODES too: same class, same result.
    harness2 = _LoopHarness(tmp_path)
    kwargs = await harness2.run_turn(
        {"reasoning_profile": "auto", "work_mode": "scheduled"}, text="check my calendar"
    )
    assert kwargs["reasoning_effort"] == "high"
    assert kwargs["max_tokens"] == 32_768


@pytest.mark.asyncio
async def test_loop_explicit_profile_is_never_rerouted_through_auto(tmp_path: Path) -> None:
    harness = _LoopHarness(tmp_path)

    # Negative control for the unknown->auto fallback: a VALID value must
    # take the explicit path even when the auto rules would decide otherwise.
    # "please refactor this module" contains a code marker, so if "fast"
    # were (wrongly) treated as unknown it would resolve auto -> think-code
    # (max, 0.6, 32768).  The explicit fast profile must survive instead.
    kwargs = await harness.run_turn({"reasoning_profile": "fast"}, text="please refactor this module")
    assert kwargs["reasoning_effort"] == "none"
    assert kwargs["max_tokens"] == 8_192

    # And symmetrically: "deep" on the same code text stays high (explicit),
    # not max (what the auto rules would pick for a code request).
    harness2 = _LoopHarness(tmp_path)
    kwargs = await harness2.run_turn({"reasoning_profile": "deep"}, text="please refactor this module")
    assert kwargs["reasoning_effort"] == "high"
    assert kwargs["max_tokens"] == 32_768


@pytest.mark.asyncio
async def test_loop_non_string_profile_falls_back_to_auto(tmp_path: Path) -> None:
    harness = _LoopHarness(tmp_path)

    # A non-string value (e.g. a bool slipped through a JSON edge) is not a
    # valid profile: parse returns None and the present value resolves as
    # auto, matching the "unknown values -> auto" acceptance criterion.
    kwargs = await harness.run_turn({"reasoning_profile": True})
    assert kwargs["reasoning_effort"] == "none"  # auto -> fast for "hello"
    assert kwargs["max_tokens"] == 8_192

    # None (explicit null) means absent: the admitted generation is untouched.
    harness2 = _LoopHarness(tmp_path)
    kwargs = await harness2.run_turn({"reasoning_profile": None})
    assert kwargs["reasoning_effort"] == _DEFAULT_EFFORT
    assert kwargs["max_tokens"] == 4096


@pytest.mark.asyncio
async def test_loop_without_profile_keeps_provider_default(tmp_path: Path) -> None:
    harness = _LoopHarness(tmp_path)

    kwargs = await harness.run_turn()

    assert kwargs["reasoning_effort"] == _DEFAULT_EFFORT


@pytest.mark.asyncio
async def test_loop_falls_back_to_auto_for_unknown_profile(tmp_path: Path) -> None:
    harness = _LoopHarness(tmp_path)

    kwargs = await harness.run_turn({"reasoning_profile": "bogus"})

    # MIT-1409 acceptance: unknown values resolve as auto, so the turn runs
    # with the auto-resolved generation ("hello" -> fast: none, 0.7, 8192),
    # not the untouched provider default.  An absent value stays untouched
    # (test_loop_without_profile_keeps_provider_default).
    assert kwargs["reasoning_effort"] == "none"
    assert kwargs["temperature"] == 0.7
    assert kwargs["max_tokens"] == 8_192


@pytest.mark.asyncio
async def test_profile_applies_to_every_provider_call_on_the_turn(tmp_path: Path) -> None:
    harness = _LoopHarness(tmp_path)

    await harness.run_turn({"reasoning_profile": "deep"})

    assert [call["reasoning_effort"] for call in harness.calls] == ["high"] * len(harness.calls)
    assert len(harness.calls) >= 1


@pytest.mark.asyncio
async def test_next_turn_without_profile_restores_default(tmp_path: Path) -> None:
    harness = _LoopHarness(tmp_path)

    await harness.run_turn({"reasoning_profile": "deep"})
    kwargs = await harness.run_turn()

    assert kwargs["reasoning_effort"] == _DEFAULT_EFFORT


@pytest.mark.asyncio
async def test_raw_reasoning_effort_override_wins_over_profile(tmp_path: Path) -> None:
    harness = _LoopHarness(tmp_path)

    kwargs = await harness.run_turn({"reasoning_profile": "fast", "reasoning_effort": "high"})

    # Production precedence: raw per-call controls beat the product profile.
    assert kwargs["reasoning_effort"] == "high"


@pytest.mark.asyncio
async def test_raw_max_tokens_override_applied_and_profile_ignored(tmp_path: Path) -> None:
    harness = _LoopHarness(tmp_path)

    kwargs = await harness.run_turn({"reasoning_profile": "fast", "max_tokens": 999})

    assert kwargs["max_tokens"] == 999
    assert kwargs["reasoning_effort"] == _DEFAULT_EFFORT


@pytest.mark.asyncio
async def test_legacy_max_tokens_passthrough_preserves_effort(tmp_path: Path) -> None:
    harness = _LoopHarness(tmp_path)

    # Production test_loop_preserves_legacy_max_tokens_without_effort (cfccc2a2):
    # a lone max_tokens override must reach the provider untouched and must not
    # switch the effort away from the configured default.
    kwargs = await harness.run_turn({"max_tokens": 16_384})

    assert kwargs["max_tokens"] == 16_384
    assert kwargs["reasoning_effort"] == _DEFAULT_EFFORT


@pytest.mark.asyncio
async def test_unrecognized_effort_string_is_forwarded_untouched(tmp_path: Path) -> None:
    harness = _LoopHarness(tmp_path)

    # Parity with production (cfccc2a2): the loop never validates the effort
    # vocabulary, it only screens out non-strings.  An unrecognized value is
    # passed through for the provider to accept or reject, so profile
    # resolution can never silently swallow a caller's explicit request.
    kwargs = await harness.run_turn({"reasoning_profile": "deep", "reasoning_effort": "banana"})

    assert kwargs["reasoning_effort"] == "banana"
    assert kwargs["max_tokens"] == 4096  # profile ignored: raw controls present

    kwargs = await harness.run_turn({"reasoning_profile": "deep", "reasoning_effort": ""})

    # Production (cfccc2a2 loop.py:926) accepts any str, including the empty
    # string the cleared UI picker produces; it screens falsy values
    # downstream, so the turn resolves with the provider's default effort —
    # never the rejected "high" from the profile.
    assert kwargs.get("reasoning_effort", "") == ""
    assert kwargs["max_tokens"] == 4096


@pytest.mark.asyncio
async def test_invalid_raw_control_key_present_disables_profile(tmp_path: Path) -> None:
    harness = _LoopHarness(tmp_path)

    kwargs = await harness.run_turn({"reasoning_profile": "deep", "max_tokens": True})

    # Production precedence: the presence of raw generation controls wins over
    # the profile even when the value is invalid, and the invalid value is
    # dropped (base default retained) rather than raising.
    assert kwargs["max_tokens"] == 4096
    assert kwargs["reasoning_effort"] == _DEFAULT_EFFORT
