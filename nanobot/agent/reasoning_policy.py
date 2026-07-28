"""Reasoning profile selection for foreground chat and background Work."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class ReasoningProfile(StrEnum):
    """Product-level generation profiles accepted by Ziggy clients."""

    AUTO = "auto"
    FAST = "fast"
    THINK = "think"
    THINK_CODE = "think-code"


@dataclass(frozen=True, slots=True)
class GenerationProfile:
    """Concrete generation controls resolved from a product profile."""

    name: ReasoningProfile
    reasoning_effort: str
    temperature: float
    max_tokens: int


@dataclass(frozen=True, slots=True)
class ReasoningDecision:
    """A resolved profile plus enough provenance for bounded telemetry."""

    requested: ReasoningProfile
    generation: GenerationProfile
    source: str
    classifier_candidate: bool = False

    @property
    def allow_escalation(self) -> bool:
        return (
            self.requested is ReasoningProfile.AUTO
            and self.generation.name is ReasoningProfile.FAST
        )


_PROFILES = {
    ReasoningProfile.FAST: GenerationProfile(
        name=ReasoningProfile.FAST,
        reasoning_effort="none",
        temperature=0.7,
        max_tokens=8_192,
    ),
    ReasoningProfile.THINK: GenerationProfile(
        name=ReasoningProfile.THINK,
        reasoning_effort="high",
        temperature=1.0,
        max_tokens=32_768,
    ),
    ReasoningProfile.THINK_CODE: GenerationProfile(
        name=ReasoningProfile.THINK_CODE,
        reasoning_effort="max",
        temperature=0.6,
        max_tokens=32_768,
    ),
}

_FAST_PREFIXES = (
    "hello",
    "hi ",
    "hey ",
    "rewrite ",
    "rephrase ",
    "extract ",
    "summarize ",
    "translate ",
    "describe ",
)
_THINK_MARKERS = (
    "analyze",
    "compare",
    "constraints",
    "design",
    "evaluate",
    "geometry",
    "multi-step",
    "plan",
    "root cause",
    "spatial",
    "strategy",
    "tradeoff",
    "trade-off",
)
_CODE_MARKERS = (
    " api ",
    " architecture",
    " code",
    " concurrency",
    " debug",
    " implement",
    " migration",
    " pull request",
    " refactor",
    " repository",
    " schema",
    " stack trace",
    " swift",
    " test failure",
)


def parse_reasoning_profile(value: Any) -> ReasoningProfile | None:
    """Return a validated profile or ``None`` for unsupported input."""
    if not isinstance(value, str):
        return None
    try:
        return ReasoningProfile(value.strip().lower())
    except ValueError:
        return None


def generation_profile(profile: ReasoningProfile | str) -> GenerationProfile:
    """Return concrete controls for a non-Auto profile."""
    parsed = profile if isinstance(profile, ReasoningProfile) else parse_reasoning_profile(profile)
    if parsed is None or parsed is ReasoningProfile.AUTO:
        raise ValueError("Auto must be resolved before requesting generation controls")
    return _PROFILES[parsed]


def _latest_user_text(messages: list[dict[str, Any]]) -> tuple[str, bool]:
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content.strip().lower(), False
        if isinstance(content, list):
            text: list[str] = []
            has_media = False
            for block in content:
                if not isinstance(block, dict):
                    continue
                block_type = str(block.get("type") or "")
                if block_type == "text" and isinstance(block.get("text"), str):
                    text.append(block["text"])
                elif block_type in {"image", "image_url", "video", "video_url"}:
                    has_media = True
            return " ".join(text).strip().lower(), has_media
    return "", False


def resolve_reasoning_profile(
    requested: ReasoningProfile | str,
    messages: list[dict[str, Any]],
    *,
    background_work: bool = False,
) -> ReasoningDecision:
    """Resolve explicit choices first, then deterministic Auto rules.

    Ambiguous requests deliberately remain Fast in V1 and are marked as
    classifier candidates. That lets telemetry quantify the useful classifier
    population before adding another inference call to every request.
    """
    parsed = requested if isinstance(requested, ReasoningProfile) else parse_reasoning_profile(requested)
    if parsed is None:
        raise ValueError(f"Unsupported reasoning profile: {requested!r}")
    if parsed is not ReasoningProfile.AUTO:
        return ReasoningDecision(
            requested=parsed,
            generation=generation_profile(parsed),
            source="explicit",
        )

    text, has_media = _latest_user_text(messages)
    padded = f" {text} "
    if any(marker in padded for marker in _CODE_MARKERS):
        selected = ReasoningProfile.THINK_CODE
        source = "rule:code"
    elif background_work:
        selected = ReasoningProfile.THINK
        source = "rule:background-work"
    elif any(marker in text for marker in _THINK_MARKERS):
        selected = ReasoningProfile.THINK
        source = "rule:complex"
    elif any(text.startswith(prefix) for prefix in _FAST_PREFIXES):
        selected = ReasoningProfile.FAST
        source = "rule:fast"
    elif has_media:
        selected = ReasoningProfile.FAST
        source = "rule:media"
    else:
        selected = ReasoningProfile.FAST
        source = "rules:fallback"
        return ReasoningDecision(
            requested=parsed,
            generation=generation_profile(selected),
            source=source,
            classifier_candidate=True,
        )
    return ReasoningDecision(
        requested=parsed,
        generation=generation_profile(selected),
        source=source,
    )


def escalation_profile(current: ReasoningProfile | str) -> GenerationProfile | None:
    """Return the single stronger retry profile allowed by V1."""
    parsed = current if isinstance(current, ReasoningProfile) else parse_reasoning_profile(current)
    if parsed is ReasoningProfile.FAST:
        return generation_profile(ReasoningProfile.THINK)
    return None
