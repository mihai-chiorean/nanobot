"""Per-turn provenance record: what produced this answer.

Design: ``docs/design/turn-provenance.md`` sections 1, 4 and 7. The record
is built at turn start, filled while the turn runs (TP-05 per model call,
TP-06 prompt fingerprint and skills, TP-07 argument repairs, TP-09 used
families) and saved to the session at turn end under ``provenance_v1`` —
the capped presentation-metadata pattern from ``utils/activity_history.py``.
It records *what produced* an answer: never the answer text, never the
user's message, never tool arguments that are not repair counters.

The ``used`` families (TP-09) power the "Used: ..." line. Families come
from the **tool object**, not from parsing wire names:
``mcp_ziggy_gmail_browser_open`` cannot be split correctly by name because
the server key ``ziggy_gmail`` itself contains ``gmail``. MCP wrappers keep
``_server_name`` and ``_original_name`` (``agent/tools/mcp.py``), so the map
below matches on the original MCP tool name; built-ins are matched by name.
Only family names, counts and the single ``account_intent`` boolean are ever
recorded for that — never arguments, queries or results.
"""

from __future__ import annotations

import re
from contextvars import ContextVar, Token
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from nanobot.security.workspace_access import current_workspace_scope

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

PROVENANCE_METADATA_KEY = "provenance_v1"


def account_intent(text: str) -> bool:
    """Return whether the user's message looks like an account question."""
    if not text:
        return False
    return bool(ACCOUNT_INTENT_RE.search(text))


def release_id() -> str:
    """Resolve the running release, or ``"unknown"`` on unstamped trees."""
    try:
        from nanobot import runtime_release
    except ImportError:
        return "unknown"
    value = getattr(runtime_release, "release_id", lambda: None)()
    return value if isinstance(value, str) and value else "unknown"


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
    """What produced one turn, as bound per task and saved to
    ``session.metadata["provenance_v1"]``.

    TP-02 owns the identity fields and the lifecycle (built at turn start,
    answered + persisted at SAVE); TP-05 fills ``calls``, TP-06 the prompt
    fingerprint and skills, TP-07 ``args_repaired``, TP-09 ``used`` /
    ``other_steps`` / ``account_intent``. Every field defaults so partial
    records (e.g. TP-09's counting tests) construct unchanged.
    """

    turn_id: str = ""
    started_at: str = ""
    source: str = ""
    answered: bool = False
    release: str = "unknown"
    model: str | None = None
    model_preset: str | None = None
    reasoning_profile: str | None = None
    account_intent: bool = False
    prompt: list[dict[str, Any]] = field(default_factory=list)
    prompt_rebuilt: bool = False
    skills_listed_sha: str | None = None
    skills_loaded: list[dict[str, str]] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)
    args_repaired: dict[str, int] = field(default_factory=dict)
    used: list[dict[str, Any]] = field(default_factory=list)
    other_steps: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Return the stored form: every field plus ``v``, without ``None``s."""
        record = asdict(self)
        record["v"] = 1
        return {key: value for key, value in record.items() if value is not None}

    def note_prompt(self, sections: list[dict[str, Any]]) -> None:
        """Record one system-prompt build (TP-06).

        The first non-empty list is the turn's fingerprint; a later build
        with a different list flags ``prompt_rebuilt`` (compaction or a
        summary change mid-turn) and the last one is kept.
        """
        if self.prompt and sections and sections != self.prompt:
            self.prompt_rebuilt = True
        if sections:
            self.prompt = sections

    def note_skill(self, name: str, sha: str) -> None:
        """Append ``{name, sha}`` to skills_loaded, de-duplicated (TP-06)."""
        entry = {"name": name, "sha": sha}
        if entry not in self.skills_loaded:
            self.skills_loaded.append(entry)

    def note_read_file_skill(self, arguments: Any) -> None:
        """Record that the model pulled a skill file into the turn itself (TP-06).

        A ``read_file`` of ``.../skills/<name>/SKILL.md`` is how the model
        loads a skill on its own; the parent directory name plus the file
        hash joins ``skills_loaded`` (de-duplicated). Relative paths resolve
        against the current workspace scope; anything that is not an existing
        SKILL.md records nothing, and arguments are never retained.
        """
        args = arguments if isinstance(arguments, dict) else {}
        path = args.get("path")
        if not isinstance(path, str) or not path.endswith("/SKILL.md"):
            return
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            scope = current_workspace_scope()
            if scope is not None:
                candidate = scope.project_path / candidate
        if not candidate.is_file():
            return
        from nanobot.agent.skills import skill_file_sha

        self.note_skill(candidate.parent.name, skill_file_sha(candidate))

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
    """The record for the turn running in this task, or ``None`` outside one."""
    return CURRENT_TURN_PROVENANCE.get()


def bind_turn_provenance(record: TurnProvenance) -> Token[TurnProvenance | None]:
    return CURRENT_TURN_PROVENANCE.set(record)


def reset_turn_provenance(token: Token[TurnProvenance | None]) -> None:
    CURRENT_TURN_PROVENANCE.reset(token)


def note_tool_call(name: str, arguments: Any) -> None:
    """Note one executed tool call on the turn's provenance record (TP-06).

    Called by the progress hook where tool-start payloads are built. Only a
    ``read_file`` of an existing ``.../SKILL.md`` is a skill load; no
    arguments are retained.
    """
    if name != "read_file":
        return
    record = CURRENT_TURN_PROVENANCE.get()
    if record is not None:
        record.note_read_file_skill(arguments)


def save_to_session(session: Any, record: TurnProvenance, cap: int = 100) -> None:
    """Append ``record`` to ``session.metadata["provenance_v1"]``, newest-capped."""
    stored: Any = session.metadata.get(PROVENANCE_METADATA_KEY)
    records = stored if isinstance(stored, list) else []
    records.append(record.to_dict())
    session.metadata[PROVENANCE_METADATA_KEY] = records[-cap:] if cap > 0 else []
