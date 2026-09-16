"""Shared rooms: credentials, liveness, and room-v1 intents (Ziggy-local, MIT-1010).

A shared room is a branch of an owner conversation that named guests can read
and post into.  Everything in this module is transport-agnostic so it can be
unit-tested without a listener; the HTTP surface lives in
``nanobot/webui/shared_rooms_http.py`` and the wiring in ``runtime.py``.

Authorization model
-------------------
1. **Control plane** -- room create / token / revoke / title are authorized by
   the tenant-wide ``tokenIssueSecret``.  Only ``ziggy-control`` holds it.
2. **Guest credential** -- :class:`RoomCredential` is minted as an ``nbrt_``
   token into two pools: a single-use WebSocket copy consumed at handshake, and
   a multi-use REST copy valid until TTL.
3. **Connection scope** -- after the handshake the credential is bound to the
   connection; a guest frame can address only ``credential.chat_id``.
4. **REST scope** -- a room credential authorizes exactly
   the room session named by the credential.  Never anything else.
5. **Liveness** -- every path re-reads session metadata: a revoked or expired
   room fails closed, and revocation drops both pools and closes sockets.
6. **Content boundary** -- ``SessionManager.shareable_messages`` decides what a
   guest may ever see.
7. **Turn isolation** -- inbound room frames carry ``shared_room`` plus
   ``INBOUND_META_ROOM_SCOPE``; the latter is what
   ``ToolRegistry.prepare_call`` gates on now that ``Tool.available()`` and the
   request-scoped session grant are gone (upstream ``6e9ae5bd``).
"""

from __future__ import annotations

import re
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from nanobot.bus.events import INBOUND_META_ROOM_SCOPE
from nanobot.webui.session_identity import webui_session_key

ROOM_ID_RE = re.compile(r"^room_[0-9a-f]{32}$")
PARTICIPANT_ID_RE = re.compile(r"^participant_[0-9a-f]{32}$")

ROOM_MODE_LEGACY = "legacy"
ROOM_MODE_COLLABORATIVE = "collaborative-v1"
ROOM_MODES = frozenset({ROOM_MODE_LEGACY, ROOM_MODE_COLLABORATIVE})

ROOM_INTENTS = frozenset({"discussion", "proposal", "ask_ziggy"})

MAX_ROOM_TOKENS = 10_000


@dataclass(frozen=True)
class RoomCredential:
    """One participant's authority over exactly one room session."""

    expires_at: float
    room_id: str
    chat_id: str
    participant_id: str
    display_name: str
    role: str

    @property
    def session_key(self) -> str:
        return webui_session_key(self.chat_id)

    def scope(self) -> dict[str, str]:
        """The inbound metadata value that gates cross-session tools."""
        return {
            "room_id": self.room_id,
            "chat_id": self.chat_id,
            "participant_id": self.participant_id,
            "role": self.role,
        }


def valid_room_id(value: Any) -> bool:
    return isinstance(value, str) and ROOM_ID_RE.fullmatch(value) is not None


def valid_participant_id(value: Any) -> bool:
    return isinstance(value, str) and PARTICIPANT_ID_RE.fullmatch(value) is not None


def valid_display_name(value: Any) -> bool:
    return isinstance(value, str) and 1 <= len(value.strip()) <= 64


def room_scope_metadata(credential: RoomCredential) -> dict[str, Any]:
    """Metadata a room turn must carry. Minted here, never accepted from a client."""
    return {
        "shared_room": True,
        "room_id": credential.room_id,
        "participant_id": credential.participant_id,
        "participant_display_name": credential.display_name,
        INBOUND_META_ROOM_SCOPE: credential.scope(),
    }


class SharedRoomStore:
    """Own room credentials, their two token pools, and room liveness."""

    def __init__(self, session_manager: Any, *, token_ttl_s: float) -> None:
        self._sessions = session_manager
        self._token_ttl_s = float(token_ttl_s)
        # A room token string is registered in both pools. The WebSocket copy is
        # consumed at handshake; the REST copy keeps validating until expiry.
        self._ws_tokens: dict[str, RoomCredential] = {}
        self._api_tokens: dict[str, RoomCredential] = {}
        self._connections: dict[Any, RoomCredential] = {}

    # -- metadata and liveness ---------------------------------------------

    def room_metadata(self, chat_id: str) -> dict[str, Any]:
        if self._sessions is None:
            return {}
        payload = self._sessions.read_session_file(webui_session_key(chat_id))
        metadata = payload.get("metadata") if isinstance(payload, dict) else None
        return metadata if isinstance(metadata, dict) else {}

    def is_shared_room(self, chat_id: str) -> bool:
        return self.room_metadata(chat_id).get("shared_room") is True

    def is_active(self, chat_id: str) -> bool:
        """A non-room chat is always 'active'; a room must be unrevoked and unexpired."""
        metadata = self.room_metadata(chat_id)
        if metadata.get("shared_room") is not True:
            return True
        if metadata.get("shared_room_revoked") is True:
            return False
        expiry = metadata.get("shared_room_expires_at")
        if expiry:
            try:
                parsed = datetime.fromisoformat(str(expiry).replace("Z", "+00:00"))
            except (ValueError, TypeError):
                # An unparseable expiry is a closed door, not an open one.
                return False
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            if parsed <= datetime.now(timezone.utc):
                return False
        return True

    def room_mode(self, chat_id: str) -> str:
        mode = self.room_metadata(chat_id).get("room_mode")
        return mode if mode in ROOM_MODES else ROOM_MODE_LEGACY

    def is_collaborative(self, chat_id: str) -> bool:
        return self.room_mode(chat_id) == ROOM_MODE_COLLABORATIVE

    def owner_credential(self, chat_id: str) -> RoomCredential | None:
        """The implicit credential the room owner holds over their own room."""
        metadata = self.room_metadata(chat_id)
        if metadata.get("shared_room") is not True:
            return None
        room_id = metadata.get("room_id")
        if not valid_room_id(room_id):
            return None
        display_name = str(metadata.get("owner_display_name") or "Owner").strip()
        return RoomCredential(
            expires_at=float("inf"),
            room_id=str(room_id),
            chat_id=chat_id,
            participant_id="owner",
            display_name=display_name[:64] or "Owner",
            role="owner",
        )

    # -- token lifecycle ----------------------------------------------------

    def _purge_expired(self) -> None:
        now = time.monotonic()
        for pool in (self._ws_tokens, self._api_tokens):
            for token, credential in list(pool.items()):
                if now > credential.expires_at:
                    pool.pop(token, None)

    def at_capacity(self) -> bool:
        self._purge_expired()
        return (
            len(self._ws_tokens) >= MAX_ROOM_TOKENS
            or len(self._api_tokens) >= MAX_ROOM_TOKENS
        )

    def mint(
        self,
        *,
        room_id: str,
        chat_id: str,
        participant_id: str,
        display_name: str,
        role: str,
    ) -> tuple[str, RoomCredential]:
        self._purge_expired()
        credential = RoomCredential(
            expires_at=time.monotonic() + self._token_ttl_s,
            room_id=room_id,
            chat_id=chat_id,
            participant_id=participant_id,
            display_name=display_name.strip()[:64],
            role=role,
        )
        token = f"nbrt_{secrets.token_urlsafe(32)}"
        self._ws_tokens[token] = credential
        self._api_tokens[token] = credential
        return token, credential

    def consume_ws_token(self, connection: Any, token: str | None) -> bool:
        """Bind a single-use room token to *connection* at handshake time."""
        if not token:
            return False
        self._purge_expired()
        credential = self._ws_tokens.pop(token, None)
        if credential is None or time.monotonic() > credential.expires_at:
            return False
        if not self.is_active(credential.chat_id):
            return False
        self._connections[connection] = credential
        return True

    def api_credential(self, token: str | None) -> RoomCredential | None:
        """Resolve a REST room token. Multi-use; never consumed."""
        if not token:
            return None
        self._purge_expired()
        credential = self._api_tokens.get(token)
        if credential is None or time.monotonic() > credential.expires_at:
            self._api_tokens.pop(token, None)
            return None
        if not self.is_active(credential.chat_id):
            return None
        return credential

    def connection_credential(self, connection: Any) -> RoomCredential | None:
        return self._connections.get(connection)

    def forget_connection(self, connection: Any) -> None:
        self._connections.pop(connection, None)

    def revoke(self, *, room_id: str, chat_id: str) -> tuple[int, list[Any]]:
        """Drop every token and connection for one room. Returns (tokens, connections)."""
        invalidated = 0
        for pool in (self._ws_tokens, self._api_tokens):
            for token, credential in list(pool.items()):
                if credential.room_id == room_id and credential.chat_id == chat_id:
                    pool.pop(token, None)
                    invalidated += 1
        connections = [
            connection
            for connection, credential in list(self._connections.items())
            if credential.room_id == room_id and credential.chat_id == chat_id
        ]
        for connection in connections:
            self._connections.pop(connection, None)
        return invalidated, connections

    def authorizes_session(self, credential: RoomCredential | None, session_key: str) -> bool:
        """A room credential authorizes exactly its own session. Nothing else."""
        return credential is not None and session_key == credential.session_key


__all__ = [
    "MAX_ROOM_TOKENS",
    "PARTICIPANT_ID_RE",
    "ROOM_ID_RE",
    "ROOM_INTENTS",
    "ROOM_MODES",
    "ROOM_MODE_COLLABORATIVE",
    "ROOM_MODE_LEGACY",
    "RoomCredential",
    "SharedRoomStore",
    "room_scope_metadata",
    "valid_display_name",
    "valid_participant_id",
    "valid_room_id",
]
