"""Per-turn source-family accounting for the "Used: ..." line (TP-09).

Design: ``docs/design/turn-provenance.md`` sections 7 and 8. The server
decides the families and labels so both apps show the same names under each
answer. Families come from the **tool object**, not from parsing wire names:
``mcp_ziggy_gmail_browser_open`` cannot be split correctly by name because
the server key ``ziggy_gmail`` itself contains ``gmail``. MCP wrappers keep
``_server_name`` and ``_original_name`` (``agent/tools/mcp.py``), so the map
below matches on the original MCP tool name; built-ins are matched by name.

Only family names, counts and the single ``account_intent`` boolean are ever
recorded here — never arguments, queries or results.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# Built-in tools that earn a named family. Every other built-in is an
# unlisted step and only counts toward ``other_steps``.
_BUILTIN_FAMILIES: dict[str, tuple[str, str, bool]] = {
    "web_search": ("web_search", "Web search", False),
    "web_fetch": ("web_pages", "Web pages", False),
}

# MCP original tool names that are listed exactly (not by prefix).
_MCP_EXACT_FAMILIES: dict[str, tuple[str, str, bool]] = {
    "browser_read_page": ("web_pages", "Web pages", False),
    "site_login": ("logins", "Saved logins", True),
}

# MCP ``browser_open`` / ``browser_find`` / ``browser_act`` /
# ``browser_fill_form`` share one family.
_BROWSER_FAMILY_NAMES = frozenset(
    {"browser_open", "browser_find", "browser_act", "browser_fill_form"}
)

# MCP original-name prefixes mapped to a family, in table order.
_MCP_PREFIX_FAMILIES: tuple[tuple[str, tuple[str, str, bool]], ...] = (
    ("gmail_", ("gmail", "Gmail", True)),
    ("vault_", ("logins", "Saved logins", True)),
    ("work_app_", ("work_apps", "Work apps", False)),
    ("scholarly_", ("scholarly", "Scholarly search", False)),
)

_BROWSER_FAMILY = ("browser", "Your signed-in sites", True)
_CONNECTOR_FAMILY_LABEL = "Connector"

# One case-insensitive regex decides ``account_intent`` at turn start (design
# section 7). Broad on purpose: a false "true" only removes a turn from the
# over-calling metric's denominator. Only the boolean is stored, never text.
ACCOUNT_INTENT_RE = re.compile(
    r"e-?mail|inbox|gmail|\bmail\b|unread|newsletter"
    r"|my (account|bank|order|booking|reservation)"
    r"|sign(ed)? in|log(ged)? ?in|password|confirmation|receipt",
    re.I,
)


def account_intent(text: str) -> bool:
    """Return whether the user's message looks like an account question."""
    if not text:
        return False
    return bool(ACCOUNT_INTENT_RE.search(text))


def _mcp_family(server_name: str, original_name: str) -> tuple[str, str, bool]:
    exact = _MCP_EXACT_FAMILIES.get(original_name)
    if exact is not None:
        return exact
    if original_name in _BROWSER_FAMILY_NAMES:
        return _BROWSER_FAMILY
    for prefix, family in _MCP_PREFIX_FAMILIES:
        if original_name.startswith(prefix):
            return family
    # Unknown connectors default to private: the line can be flagged, so an
    # unmapped new connector is safe until it joins the map.
    return (f"mcp:{server_name}", _CONNECTOR_FAMILY_LABEL, True)


def family_for(tool: Any) -> tuple[str, str, bool] | None:
    """Map one tool object to ``(family, label, private)``, or ``None``.

    ``None`` means an unlisted built-in: it is not named on the "Used:" line
    and only counts toward ``other_steps``.
    """
    if tool is None:
        return None
    server_name = getattr(tool, "_server_name", None)
    original_name = getattr(tool, "_original_name", None)
    if isinstance(server_name, str) and server_name and isinstance(original_name, str) and original_name:
        return _mcp_family(server_name, original_name)
    name = getattr(tool, "name", None)
    if isinstance(name, str):
        return _BUILTIN_FAMILIES.get(name)
    return None


@dataclass
class TurnProvenance:
    """The TP-09 slice of the turn's provenance record.

    TP-02 owns the full record (turn id, release, prompt fingerprint,
    per-call entries); this carries only the fields the ``turn_end`` frame
    and the over-calling metric need, and later TP issues extend it.
    """

    account_intent: bool = False
    used: list[dict[str, Any]] = field(default_factory=list)
    other_steps: int = 0

    def note_tool_result(self, tool: Any, status: str) -> None:
        """Record one executed tool call.

        Callers must invoke this only for calls that actually ran; calls
        refused by ``prepare_call`` are not counted at all. A listed family
        upserts its entry in first-use order; unlisted built-ins only bump
        ``other_steps``. Failed calls still count as used, and increment
        ``errors``.
        """
        mapped = family_for(tool)
        if mapped is None:
            self.other_steps += 1
            return
        family, label, private = mapped
        for entry in self.used:
            if entry["family"] == family:
                entry["calls"] += 1
                if status == "error":
                    entry["errors"] += 1
                return
        self.used.append(
            {
                "family": family,
                "label": label,
                "private": private,
                "calls": 1,
                "errors": 1 if status == "error" else 0,
            }
        )
