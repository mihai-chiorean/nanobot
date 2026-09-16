"""Shared-room tool authorization (Ziggy-local, MIT-1010).

Upstream ``6e9ae5bd`` deleted ``Tool.available()`` and the request-scoped
session access grant (``INBOUND_META_SESSION_READ_SCOPE`` /
``SessionAccessScope``).  Nothing replaced them, and 0.3.0 simultaneously ships
``search_sessions`` / ``read_session`` / ``list_sessions`` /
``send_session_message``, which read and write *any* persisted session by key.

In a shared room a guest's message drives the agent's turn.  Without a gate a
guest can prompt the room agent into reading the owner's private conversations.

The replacement keeps upstream's shape -- no per-request ``available()`` hook on
the base class, no re-widened namespace scope -- and instead classifies each
tool once, statically, and enforces the classification in
``ToolRegistry.prepare_call``: the single funnel every tool call passes through.

Fail-closed by classification: a denied tool is denied whenever a room scope is
bound to the request, regardless of who is speaking.  Tools that are not
classified default to :data:`RoomPolicy.ALLOWED`, which matches today's
behaviour on the deployed 0.2.x snapshot -- a room turn there can already reach
every registered tool.  What changes is that the *cross-session* surface 0.3.0
newly introduced is closed before it is ever exposed to a guest.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Mapping

from nanobot.bus.events import INBOUND_META_ROOM_SCOPE


class RoomPolicy(str, Enum):
    """Whether a tool may run during a shared-room turn."""

    ALLOWED = "allowed"
    DENIED = "denied"


# Tools denied inside a shared room, with the reason each is denied.
#
# Three families:
#   1. cross-session readers/writers -- the actual C3 hole;
#   2. runtime mutation -- a guest must not retune the owner's runtime;
#   3. durable private memory -- room content must not enter owner memory, and
#      owner memory must not be recalled into a room transcript.
ROOM_DENIED_TOOLS: dict[str, str] = {
    # 1. cross-session
    "search_sessions": "other conversations are not readable from a shared room",
    "read_session": "other conversations are not readable from a shared room",
    "list_sessions": "other conversations are not listable from a shared room",
    "send_session_message": "messages cannot be sent to other conversations from a shared room",
    # 2. runtime mutation
    "my": "runtime settings cannot be changed from a shared room",
    "cron": "schedules cannot be changed from a shared room",
    "schedule_work": "scheduled work cannot be created from a shared room",
    # 3. private memory
    "recall": "private memory is not readable from a shared room",
    "ingest": "shared-room content is not ingested into private memory",
}


def room_policy_for(tool_name: str) -> RoomPolicy:
    """Classify *tool_name*. Unknown names are allowed, as they are today."""
    return RoomPolicy.DENIED if tool_name in ROOM_DENIED_TOOLS else RoomPolicy.ALLOWED


def room_scope(metadata: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Return the validated room scope carried by *metadata*, if any.

    A scope is only honoured when it carries both a ``room_id`` and a
    ``chat_id``: a half-populated value is a bug in the minting path, and
    treating it as "no room" would silently open the very gate it describes.
    Such a value is therefore normalised to a deny-everything scope rather than
    to ``None``.
    """
    if not isinstance(metadata, Mapping):
        return None
    raw = metadata.get(INBOUND_META_ROOM_SCOPE)
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        return {"room_id": "", "chat_id": "", "participant_id": "", "role": "guest"}
    scope = {
        "room_id": str(raw.get("room_id") or ""),
        "chat_id": str(raw.get("chat_id") or ""),
        "participant_id": str(raw.get("participant_id") or ""),
        "role": str(raw.get("role") or "guest"),
    }
    return scope


def room_scope_session_key(scope: Mapping[str, Any] | None, channel: str = "websocket") -> str | None:
    """The one session key a room-scoped turn may resolve."""
    if not scope:
        return None
    chat_id = scope.get("chat_id")
    return f"{channel}:{chat_id}" if chat_id else None


def room_denial_message(tool_name: str) -> str:
    reason = ROOM_DENIED_TOOLS.get(tool_name, "this tool is unavailable in a shared room")
    return f"Error: Tool '{tool_name}' is unavailable in a shared room: {reason}."


__all__ = [
    "ROOM_DENIED_TOOLS",
    "RoomPolicy",
    "room_denial_message",
    "room_policy_for",
    "room_scope",
    "room_scope_session_key",
]
