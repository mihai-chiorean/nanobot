"""Turn-scoped skill tool allowlist.

Mirror of ``read_only.py`` for the Agent Skills ``allowed-tools`` frontmatter
field (SR-17). A scheduled run knows its skill in advance, so the skill can
scope which tools that turn may see and call; the registry enforces the set
carried on the turn metadata. Absent or empty means no filter: unlike the
read-only flag, an unparseable value degrades to the ordinary (unscoped) turn
rather than hiding every tool, because skills legitimately ship without the
field and a typo in one must not brick the run.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, cast

ALLOWED_TOOLS_META_KEY = "allowed_tools"


def _names_from_value(value: Any) -> list[str]:
    if isinstance(value, str):
        return [part.strip() for part in value.replace(",", " ").split() if part.strip()]
    if isinstance(value, (list, tuple, set, frozenset)):
        return [
            item.strip()
            for item in cast(Iterable[object], value)
            if isinstance(item, str) and item.strip()
        ]
    return []


def allowed_tools_for_turn(metadata: Mapping[str, Any] | None) -> frozenset[str] | None:
    """Return the tool allowlist for the turn, or ``None`` when unscoped.

    Accepts a pre-built set (what the scheduled runner stamps) or the raw
    frontmatter shapes (space/comma-separated string, list of names).
    """
    if not isinstance(metadata, Mapping):
        return None
    names = _names_from_value(metadata.get(ALLOWED_TOOLS_META_KEY))
    return frozenset(names) if names else None


def allowed_tools_denial_message(tool_name: str) -> str:
    return (
        f"Error: Tool '{tool_name}' is not in this turn's allowed tools. "
        "This turn runs under a skill tool policy; use one of the allowed "
        "tools or report the finding without taking the action."
    )


__all__ = [
    "ALLOWED_TOOLS_META_KEY",
    "allowed_tools_denial_message",
    "allowed_tools_for_turn",
]
