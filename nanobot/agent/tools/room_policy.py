"""Shared-room tool authorization (Ziggy-local, MIT-1010).

Upstream ``6e9ae5bd`` deleted ``Tool.available()`` and the request-scoped
session access grant (``INBOUND_META_SESSION_READ_SCOPE`` /
``SessionAccessScope``).  Nothing replaced them, and 0.3.0 simultaneously ships
``search_sessions`` / ``read_session`` / ``list_sessions`` /
``send_session_message``, which read and write *any* persisted session by key.

In a shared room a guest's message drives the agent's turn, so without a gate a
guest can prompt the room agent into reading the owner's private data.

**This is an allow-list.**  An earlier revision of this module was a deny-list
of eleven tool names while claiming to be an allow-list, which left every MCP
connector tool (`gmail_search`, `gmail_send_message`, every `linkedin_*`) and
most built-ins (`grep`, `read_file`, `message`, `create_goal`, …) reachable from
a guest turn.  Three exploits were demonstrated against that version, each
walking around one deny-list family.  The rule now is the one the room system
prompt already promises participants:

    unclassified means denied.

Adding a tool to :data:`ROOM_ALLOWED_TOOLS` is a deliberate act with a written
reason.  A new upstream tool, a new fork tool, and every MCP tool a connector
exposes are all denied on arrival, which is the only posture that stays correct
as the tool surface grows.

Enforcement lives in :meth:`ToolRegistry.prepare_call` — the single funnel every
tool call passes through — because ``Tool.available()`` no longer exists.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Mapping

from nanobot.bus.events import INBOUND_META_ROOM_SCOPE


class RoomPolicy(str, Enum):
    """Whether a tool may run during a shared-room turn."""

    ALLOWED = "allowed"
    DENIED = "denied"


# The complete set of tools a shared-room turn may call, each with the reason it
# is safe.  "Safe" here means: it cannot read or write anything outside the room
# transcript the guest can already see, and it cannot mutate tenant state.
#
# Deliberately NOT here, and why:
#   read/search/list/send_session_*  cross the session boundary -- the C3 hole
#   recall, ingest                   private memory
#   read_file, grep, find_files,     the workspace holds memory/, sessions/,
#     list_dir, write_file,          config and credentials; filesystem.py:130
#     edit_file, apply_patch         allowlists memory/history.jsonl even under
#                                    restrictToWorkspace
#   message                          can address another channel/chat and attach
#                                    arbitrary local files as media
#   my, cron, schedule_work          mutate tenant runtime state
#   create_goal, update_goal         durable sustained-goal state (the real
#                                    tools; "long_task" was never a tool name)
#   spawn                            a fresh turn one level down
#   exec, exec_session, run_cli_app  arbitrary code execution
#   generate_image                   spends the owner's provider quota
#   every MCP tool                   a connector speaks for the owner's
#                                    identity: gmail, linkedin, calendar
ROOM_ALLOWED_TOOLS: dict[str, str] = {
    "web_search": "public web search; reads nothing tenant-local",
    "web_fetch": "public URL fetch, already SSRF-guarded; reads nothing tenant-local",
    "report_progress": "writes only to the Work task this turn already owns",
}


def room_policy_for(tool_name: str) -> RoomPolicy:
    """Classify *tool_name*. **Unknown names are denied.**"""
    return RoomPolicy.ALLOWED if tool_name in ROOM_ALLOWED_TOOLS else RoomPolicy.DENIED


# A scope that names no room. Returned wherever a room's authority cannot be
# established, so "we could not tell" denies instead of falling open.
DENY_EVERYTHING_SCOPE: dict[str, Any] = {
    "room_id": "",
    "chat_id": "",
    "participant_id": "",
    "role": "guest",
}


def room_scope(metadata: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Return the room scope carried by *metadata*, if any.

    ``None`` means "not a room turn". Anything present but malformed normalises
    to :data:`DENY_EVERYTHING_SCOPE` rather than to ``None``: a half-populated
    scope is a bug in the minting path, and reading it as "no room" would open
    the very gate it describes.
    """
    if not isinstance(metadata, Mapping):
        return None
    raw = metadata.get(INBOUND_META_ROOM_SCOPE)
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        return dict(DENY_EVERYTHING_SCOPE)
    return {
        "room_id": str(raw.get("room_id") or ""),
        "chat_id": str(raw.get("chat_id") or ""),
        "participant_id": str(raw.get("participant_id") or ""),
        "role": str(raw.get("role") or "guest"),
    }


# Sentinel session key that matches nothing, so a scope with no chat_id resolves
# to "no session is readable" instead of to "no restriction".
NO_SESSION_KEY = "\0"


def room_scope_session_key(
    scope: Mapping[str, Any] | None,
    channel: str = "websocket",
) -> str | None:
    """The one session key a room-scoped turn may resolve.

    ``None`` only when there is no scope at all. A scope with no ``chat_id``
    yields :data:`NO_SESSION_KEY`, which no real session can equal.
    """
    if scope is None:
        return None
    chat_id = scope.get("chat_id")
    return f"{channel}:{chat_id}" if chat_id else NO_SESSION_KEY


def room_denial_message(tool_name: str) -> str:
    return (
        f"Error: Tool '{tool_name}' is unavailable in a shared conversation. "
        "Only public web lookups and visible progress updates are available here."
    )


__all__ = [
    "DENY_EVERYTHING_SCOPE",
    "NO_SESSION_KEY",
    "ROOM_ALLOWED_TOOLS",
    "RoomPolicy",
    "room_denial_message",
    "room_policy_for",
    "room_scope",
    "room_scope_session_key",
]
