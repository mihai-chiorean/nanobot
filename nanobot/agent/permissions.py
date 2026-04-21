"""Scoped permission system for sub-agent tool access.

Each scope defines a whitelist of tools that a sub-agent is allowed to use.
When spawning a sub-agent, the caller can request a specific scope; otherwise
the default scope for the agent type is applied.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Scope constants
# ---------------------------------------------------------------------------

SCOPE_READ_ONLY: str = "read-only"
SCOPE_CODE_REVIEW: str = "code-review"
SCOPE_RESEARCH: str = "research"
SCOPE_FULL: str = "full"

# ---------------------------------------------------------------------------
# Scope -> allowed tool names
# ---------------------------------------------------------------------------

SCOPE_TOOL_WHITELIST: dict[str, list[str]] = {
    SCOPE_READ_ONLY: ["read_file", "list_dir", "web_search", "web_fetch", "recall"],
    SCOPE_CODE_REVIEW: ["read_file", "list_dir", "exec", "recall", "web_search"],
    SCOPE_RESEARCH: ["web_search", "web_fetch", "read_file", "write_file", "recall"],
    SCOPE_FULL: [],  # empty = all tools allowed
}

# ---------------------------------------------------------------------------
# Agent type -> default scope
# ---------------------------------------------------------------------------

DEFAULT_TYPE_SCOPES: dict[str, str] = {
    "planner": SCOPE_READ_ONLY,
    "researcher": SCOPE_RESEARCH,
    "reviewer": SCOPE_CODE_REVIEW,
    "code": SCOPE_FULL,
}

# All valid scope names (for parameter validation)
ALL_SCOPES: tuple[str, ...] = (
    SCOPE_READ_ONLY,
    SCOPE_CODE_REVIEW,
    SCOPE_RESEARCH,
    SCOPE_FULL,
)


def resolve_scope(agent_type: str, explicit_scope: str | None = None) -> str:
    """Determine the effective permission scope for a sub-agent.

    Parameters
    ----------
    agent_type:
        The agent type name (e.g. ``"code"``, ``"planner"``).
    explicit_scope:
        An optional scope override provided by the caller.  When given, it
        takes precedence over the type default.

    Returns
    -------
    str
        One of the ``SCOPE_*`` constants.  Falls back to ``SCOPE_FULL`` when
        neither the explicit scope nor a type default is defined.
    """
    if explicit_scope is not None and explicit_scope in ALL_SCOPES:
        return explicit_scope
    return DEFAULT_TYPE_SCOPES.get(agent_type, SCOPE_FULL)
