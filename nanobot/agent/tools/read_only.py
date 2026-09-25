"""Turn-scoped read-only tool mode.

Background Work tasks can read untrusted content, so a prompt injection can
try to steer the agent into a side-effecting tool call. The registry uses this
module to honor an explicit opt-in restriction for a turn while leaving ordinary
turns unchanged.
"""

from __future__ import annotations

from typing import Any, Mapping

READ_ONLY_META_KEY = "read_only"

_TRUE_MARKERS = frozenset(("true", "1", "yes", "on"))
_FALSE_MARKERS = frozenset(("", "false", "0", "no", "off", "none", "null"))


def read_only_value(value: Any) -> bool:
    """Return whether *value* is an explicit read-only opt-in.

    Missing or recognized false values are permissive; unrecognized values fail
    closed because a malformed security-sensitive flag should not silently
    expose side-effecting tools.
    """
    if value is None or value is False:
        return False
    if value is True:
        return True
    if isinstance(value, str):
        marker = value.strip().casefold()
        return marker not in _FALSE_MARKERS
    if isinstance(value, int):
        return value != 0
    return True


def read_only_turn(metadata: Mapping[str, Any] | None) -> bool:
    """Return whether the current turn metadata opts into read-only tools."""
    if not isinstance(metadata, Mapping):
        return False
    return read_only_value(metadata.get(READ_ONLY_META_KEY, False))


def read_only_denial_message(tool_name: str) -> str:
    return (
        f"Error: Tool '{tool_name}' is unavailable in a read-only turn. "
        "This turn can inspect context but cannot change state, send messages, "
        "execute commands, or publish results. Use a read-only tool or report "
        "the finding without taking the action."
    )


__all__ = [
    "READ_ONLY_META_KEY",
    "read_only_denial_message",
    "read_only_turn",
    "read_only_value",
]
