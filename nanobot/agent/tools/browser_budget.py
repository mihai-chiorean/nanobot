"""Per-turn budget for browser tool calls (D5-14, NFR-MODEL-002).

Only the runtime knows what a turn is, so the cap lives in the tool-execution
path: every browser tool call increments a per-run counter (the same
``external_lookup_counts`` dict the repeated-lookup throttle uses) and calls
past the cap are refused with the D5 failure envelope instead of executing.
"""

from __future__ import annotations

import json
import re

MAX_BROWSER_CALLS_PER_TURN = 30

_BROWSER_EXACT = {"site_login", "vault_list_sites"}
_KEY = "browser:*"

# MCP tools are named ``mcp_<server>_<tool>`` (agent/tools/mcp.py); strip the
# server prefix only when what follows is a browser tool, so a server segment
# never swallows a non-browser tool name.
_MCP_SERVER_PREFIX = re.compile(
    r"^mcp_[a-z0-9_]+?_(?=(browser_|linkedin_|site_login$|vault_list_sites$))"
)

_ENVELOPE: dict[str, object] = {
    "ok": False,
    "outcome": "blocked",
    "source": "auth",
    "error": {
        "code": "turn_budget_exhausted",
        "class": "input",
        "message": (
            "This turn has used 30 browser actions. Stop browsing now: tell the "
            "user what you found, what is left, and ask whether to continue."
        ),
        "retryable": False,
        "human": "none",
        "next": "conclude",
    },
}

ENVELOPE = json.dumps(_ENVELOPE, separators=(",", ":"))


def is_browser_tool(name: str) -> bool:
    base = _MCP_SERVER_PREFIX.sub("", name, count=1)
    return base.startswith(("browser_", "linkedin_")) or base in _BROWSER_EXACT


def browser_budget_error(name: str, counts: dict[str, int]) -> str | None:
    """Count one browser call; return the refusal envelope past the cap."""
    if not is_browser_tool(name):
        return None
    counts[_KEY] = counts.get(_KEY, 0) + 1
    if counts[_KEY] <= MAX_BROWSER_CALLS_PER_TURN:
        return None
    return ENVELOPE
