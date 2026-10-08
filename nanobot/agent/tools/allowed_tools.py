"""Turn-scoped tool allowlist (skill-scoped runs).

A scheduled run created for a skill knows in advance which tools the skill
uses, so the run can be narrowed to exactly those tools instead of the full
registry. The allowlist arrives via turn metadata (the same mechanism as the
read-only turn filter in ``read_only.py``), and the registry both hides the
other tools from the advertised definitions and refuses calls to them.

The names are registry tool names: built-in names and ``mcp_<server>_<tool>``
for MCP tools. Both this filter and the read-only filter apply to a turn, so
the effective tool set is their intersection.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Mapping, cast

ALLOWED_TOOLS_META_KEY = "allowed_tools"


def normalize_allowed_tools(value: Any) -> frozenset[str] | None:
    """Parse an allowlist value into tool names, or ``None`` for no filter.

    Accepts a space- and/or comma-separated string, or a list/tuple/set of
    such strings. ``None`` (and an empty/blank string) means the field was not
    set: no filter. Anything unrecognizable fails closed as an empty allowlist
    -- a malformed security-sensitive value must not silently expose every
    tool.
    """
    if value is None:
        return None
    if isinstance(value, str):
        names = frozenset(token for token in value.replace(",", " ").split())
        return names or None
    if isinstance(value, (list, tuple, set, frozenset)):
        collected: set[str] = set()
        for item in cast(Iterable[object], value):
            if not isinstance(item, str):
                return frozenset()
            collected.update(item.replace(",", " ").split())
        return frozenset(collected)
    return frozenset()


def allowed_tools_for_turn(metadata: Mapping[str, Any] | None) -> frozenset[str] | None:
    """Return the tool names the current turn may use, or ``None`` for all.

    A missing (not merely empty) ``allowed_tools`` key means the turn carries
    no skill scoping. A present-but-malformed value fails closed to an empty
    allowlist, matching the read-only flag's posture on malformed input.
    """
    if not isinstance(metadata, Mapping):
        return None
    if ALLOWED_TOOLS_META_KEY not in metadata:
        return None
    return normalize_allowed_tools(metadata[ALLOWED_TOOLS_META_KEY])


def allowed_tools_denial_message(tool_name: str) -> str:
    return (
        f"Error: Tool '{tool_name}' is not available for this run. "
        "This turn is scoped to a fixed set of tools; use one of the tools it "
        "was given, or report the finding without taking the action."
    )


__all__ = [
    "ALLOWED_TOOLS_META_KEY",
    "allowed_tools_denial_message",
    "allowed_tools_for_turn",
    "normalize_allowed_tools",
]
