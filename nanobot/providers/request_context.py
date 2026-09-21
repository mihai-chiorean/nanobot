"""Request-local scheduling metadata for OpenAI-compatible model gateways."""

from __future__ import annotations

from contextvars import ContextVar, Token
from typing import Literal

SchedulingClass = Literal["foreground", "background"]

_current_scheduling_class: ContextVar[SchedulingClass] = ContextVar(
    "model_scheduling_class",
    default="foreground",
)


def current_scheduling_class() -> SchedulingClass:
    """Return the class inherited by model calls in the current task context."""
    return _current_scheduling_class.get()


def set_scheduling_class(value: SchedulingClass) -> Token[SchedulingClass]:
    """Bind a validated scheduling class for one request tree."""
    if value not in ("foreground", "background"):
        raise ValueError("invalid scheduling class")
    return _current_scheduling_class.set(value)


def reset_scheduling_class(token: Token[SchedulingClass]) -> None:
    """Restore the scheduling context associated with *token*."""
    _current_scheduling_class.reset(token)
