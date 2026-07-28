from __future__ import annotations

import pytest

from nanobot.agent.reasoning_policy import (
    ReasoningProfile,
    escalation_profile,
    resolve_reasoning_profile,
)


def _messages(text: str) -> list[dict]:
    return [{"role": "user", "content": text}]


@pytest.mark.parametrize(
    ("requested", "resolved", "temperature", "max_tokens"),
    [
        ("fast", ReasoningProfile.FAST, 0.7, 8_192),
        ("think", ReasoningProfile.THINK, 1.0, 32_768),
        ("think-code", ReasoningProfile.THINK_CODE, 0.6, 32_768),
    ],
)
def test_explicit_profiles_win(
    requested: str,
    resolved: ReasoningProfile,
    temperature: float,
    max_tokens: int,
) -> None:
    decision = resolve_reasoning_profile(requested, _messages("hello"))

    assert decision.generation.name is resolved
    assert decision.generation.temperature == temperature
    assert decision.generation.max_tokens == max_tokens
    assert decision.source == "explicit"
    assert decision.allow_escalation is False


def test_auto_routes_code_to_precise_thinking() -> None:
    decision = resolve_reasoning_profile(
        "auto",
        _messages("Debug this Swift concurrency test failure"),
    )

    assert decision.generation.name is ReasoningProfile.THINK_CODE
    assert decision.source == "rule:code"


def test_auto_routes_background_work_to_thinking() -> None:
    decision = resolve_reasoning_profile(
        "auto",
        _messages("Read my newsletters"),
        background_work=True,
    )

    assert decision.generation.name is ReasoningProfile.THINK
    assert decision.source == "rule:background-work"


def test_ambiguous_auto_stays_fast_and_marks_classifier_candidate() -> None:
    decision = resolve_reasoning_profile("auto", _messages("What should I do next?"))

    assert decision.generation.name is ReasoningProfile.FAST
    assert decision.classifier_candidate is True
    assert decision.allow_escalation is True


def test_escalation_is_bounded_to_fast_to_think() -> None:
    assert escalation_profile("fast").name is ReasoningProfile.THINK
    assert escalation_profile("think") is None
    assert escalation_profile("think-code") is None
