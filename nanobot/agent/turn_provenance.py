"""The per-turn provenance record (TP-02; source families from TP-09).

Design: ``docs/design/turn-provenance.md`` sections 1, 2 and 7. One record is
built at turn start, filled as the turn runs (per call by TP-05, prompt by
TP-06, repairs by TP-07, ``used`` by TP-09) and saved to the session at turn
end as ``session.metadata["provenance_v1"]``, capped to the newest 100 turns
— the pattern ``utils/activity_history.py`` sets for ``activity_v1``.

The server decides the families and labels so both apps show the same names
under each answer. Families come from the **tool object**, not from parsing
wire names: ``mcp_ziggy_gmail_browser_open`` cannot be split correctly by
name because the server key ``ziggy_gmail`` itself contains ``gmail``. MCP
wrappers keep ``_server_name`` and ``_original_name``
(``agent/tools/mcp.py``), so the map below matches on the original MCP tool
name; built-ins are matched by name.

Only family names, counts and the single ``account_intent`` boolean are ever
recorded from tool use — never arguments, queries, results or message text.
"""

from __future__ import annotations

import re
from contextvars import ContextVar, Token
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


# Session-metadata key holding the bounded list of turn records, mirroring
# ``utils/activity_history.py``'s ``activity_v1``.
PROVENANCE_KEY = "provenance_v1"

# Field order of the stored entry (design section 7). ``None`` values are
# omitted by ``to_dict``; ``False``/0/[]/{} are kept so consumers can
# distinguish "decided false" from "not recorded".
_TO_DICT_FIELDS: tuple[str, ...] = (
    "turn_id",
    "started_at",
    "source",
    "answered",
    "release",
    "model",
    "model_preset",
    "reasoning_profile",
    "account_intent",
    "prompt",
    "prompt_rebuilt",
    "skills_listed_sha",
    "skills_loaded",
    "calls",
    "args_repaired",
    "used",
    "other_steps",
)


@dataclass
class TurnProvenance:
    """What produced one turn, as stored in ``provenance_v1``.

    Built at turn start (turn id, started time, source, release,
    ``account_intent``) and filled as the turn runs: model, preset and
    reasoning profile from the turn runtime decision; per-call entries by
    TP-05; prompt fingerprint by TP-06; argument-repair counters by TP-07
    (via the ``CURRENT_TURN_PROVENANCE`` contextvar); ``used``/``other_steps``
    by TP-09. ``answered`` is set by the save stage from the final content.
    """

    turn_id: str | None = None
    started_at: str | None = None
    source: str | None = None
    answered: bool = False
    release: str | None = None
    model: str | None = None
    model_preset: str | None = None
    reasoning_profile: str | None = None
    account_intent: bool = False
    prompt: list[dict[str, Any]] = field(default_factory=list)
    prompt_rebuilt: bool = False
    skills_listed_sha: str | None = None
    skills_loaded: list[dict[str, Any]] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)
    args_repaired: dict[str, int] = field(default_factory=dict)
    used: list[dict[str, Any]] = field(default_factory=list)
    other_steps: int = 0

    def to_dict(self) -> dict[str, Any]:
        """The stored entry: every field plus the schema tag ``v``, sans ``None``."""
        entry: dict[str, Any] = {"v": 1}
        for name in _TO_DICT_FIELDS:
            value = getattr(self, name)
            if value is None:
                continue
            if isinstance(value, list):
                value = list(value)
            elif isinstance(value, dict):
                value = dict(value)
            entry[name] = value
        return entry

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


CURRENT_TURN_PROVENANCE: ContextVar[TurnProvenance | None] = ContextVar(
    "nanobot_turn_provenance",
    default=None,
)


def current_turn_provenance() -> TurnProvenance | None:
    """The record for the turn running on this task, or ``None``.

    The turn's tools and per-call observers fill the record through this
    getter (``agent/tools/execution.py`` counts argument repairs that way),
    so a call site never needs the session key.
    """
    return CURRENT_TURN_PROVENANCE.get()


def bind_turn_provenance(record: TurnProvenance) -> Token[TurnProvenance | None]:
    return CURRENT_TURN_PROVENANCE.set(record)


def reset_turn_provenance(token: Token[TurnProvenance | None]) -> None:
    CURRENT_TURN_PROVENANCE.reset(token)


def save_to_session(session: Any, record: TurnProvenance, cap: int = 100) -> None:
    """Append one turn record to ``session.metadata`` under ``provenance_v1``.

    Keeps only the newest ``cap`` entries, bounded the same way
    ``utils/activity_history.py`` bounds ``activity_v1``.
    """
    entry = record.to_dict()
    stored: Any = session.metadata.setdefault(PROVENANCE_KEY, [])
    if not isinstance(stored, list):
        stored = []
        session.metadata[PROVENANCE_KEY] = stored
    stored.append(entry)
    session.metadata[PROVENANCE_KEY] = stored[-cap:]
