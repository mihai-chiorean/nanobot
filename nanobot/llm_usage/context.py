"""Request-local metadata for LLM usage records."""

from __future__ import annotations

from collections.abc import Generator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Literal

LLMUsageSource = Literal["user", "api", "cron", "dream", "system"]

_CURRENT_SOURCE: ContextVar[LLMUsageSource] = ContextVar(
    "nanobot_llm_usage_source",
    default="system",
)

# TP-05: the effective turn id of the running agent turn, bound by the runner
# next to ``source`` so ``LLMProvider._observe_llm_call`` can key its row on
# the same id the provenance record and the wire frames use. ``None`` for
# provider calls made outside an agent turn.
_CURRENT_TURN_ID: ContextVar[str | None] = ContextVar(
    "nanobot_llm_usage_turn_id",
    default=None,
)


def source_from_session_key(session_key: str | None) -> LLMUsageSource:
    """Classify a private session key without persisting that key."""
    key = session_key or ""
    if key.startswith("dream:"):
        return "dream"
    if key == "heartbeat" or key.startswith("cron:"):
        return "cron"
    if key.startswith("api:"):
        return "api"
    if key.startswith("system:"):
        return "system"
    return "user"


def source_from_request(
    session_key: str | None,
    *,
    channel: str | None,
    metadata: Mapping[str, object] | None,
) -> LLMUsageSource:
    """Classify a turn from trusted ingress metadata without retaining identifiers."""
    values = metadata or {}
    if isinstance(values.get("_cron_trigger"), Mapping):
        return "cron"
    if isinstance(values.get("_local_trigger"), Mapping):
        return "cron"
    if channel == "api":
        return "api"
    if channel == "system":
        return "system"
    return source_from_session_key(session_key)


def current_llm_usage_source() -> LLMUsageSource:
    return _CURRENT_SOURCE.get()


def bind_llm_usage_source(source: LLMUsageSource) -> Token[LLMUsageSource]:
    return _CURRENT_SOURCE.set(source)


def reset_llm_usage_source(token: Token[LLMUsageSource]) -> None:
    _CURRENT_SOURCE.reset(token)


def current_llm_usage_turn_id() -> str | None:
    return _CURRENT_TURN_ID.get()


def bind_llm_usage_turn_id(turn_id: str | None) -> Token[str | None]:
    return _CURRENT_TURN_ID.set(turn_id)


def reset_llm_usage_turn_id(token: Token[str | None]) -> None:
    _CURRENT_TURN_ID.reset(token)


@contextmanager
def llm_usage_source(source: LLMUsageSource) -> Generator[None]:
    """Bind a coarse usage source for nested provider calls."""
    token = bind_llm_usage_source(source)
    try:
        yield
    finally:
        reset_llm_usage_source(token)
