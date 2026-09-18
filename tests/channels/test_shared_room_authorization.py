"""Shared-room authorization (Ziggy-local, MIT-1010).

Upstream ``6e9ae5bd`` deleted ``Tool.available()`` and the request-scoped
session access grant, while 0.3.0 simultaneously ships ``search_sessions`` /
``read_session`` / ``list_sessions`` / ``send_session_message``, which read and
write *any* persisted session by key.  A shared-room turn is driven by a guest's
message, so without a replacement a guest could prompt the room agent into
reading the owner's private conversations.

The negative case -- ``test_guest_cannot_read_another_session`` and
``test_guest_cannot_search_other_sessions`` -- is the point of this file.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from nanobot.agent.tools.base import Tool, ToolResult
from nanobot.agent.tools.context import RequestContext, request_context
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.loader import ToolLoader
from nanobot.agent.tools.room_policy import (
    ROOM_ALLOWED_TOOLS,
    RoomPolicy,
    room_policy_for,
    room_scope,
    room_scope_session_key,
)
from nanobot.bus.events import INBOUND_META_ROOM_SCOPE
from nanobot.channels.websocket.rooms import (
    RoomCredential,
    SharedRoomStore,
    room_scope_metadata,
)
from nanobot.session.manager import SessionManager

OWNER_CHAT = "chat_owner"
ROOM_CHAT = "11111111-2222-3333-4444-555555555555"
ROOM_ID = "room_" + "a" * 32
PARTICIPANT_ID = "participant_" + "b" * 32


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def sessions(tmp_path: Path) -> SessionManager:
    return SessionManager(tmp_path)


@pytest.fixture
def owner_session(sessions: SessionManager):
    session = sessions.get_or_create(f"websocket:{OWNER_CHAT}")
    session.add_message("user", "my bank password is hunter2")
    session.add_message("assistant", "noted privately")
    session.metadata["title"] = "Private planning"
    sessions.save(session, fsync=True)
    return session


@pytest.fixture
def room(sessions: SessionManager, owner_session):
    sessions.clone_session_for_shared_room(
        f"websocket:{OWNER_CHAT}",
        f"websocket:{ROOM_CHAT}",
        metadata={
            "shared_room": True,
            "room_mode": "legacy",
            "room_id": ROOM_ID,
            "title": "Shared conversation",
            "owner_display_name": "Mihai",
            "shared_room_title_revision": 0,
        },
        owner_display_name="Mihai",
    )
    return sessions.get_or_create(f"websocket:{ROOM_CHAT}")


@pytest.fixture
def store(sessions: SessionManager, room) -> SharedRoomStore:
    return SharedRoomStore(sessions, token_ttl_s=300)


def guest_credential() -> RoomCredential:
    return RoomCredential(
        expires_at=time.monotonic() + 300,
        room_id=ROOM_ID,
        chat_id=ROOM_CHAT,
        participant_id=PARTICIPANT_ID,
        display_name="Guest",
        role="contributor",
    )


def guest_request_context() -> RequestContext:
    return RequestContext(
        channel="websocket",
        chat_id=ROOM_CHAT,
        session_key=f"websocket:{ROOM_CHAT}",
        metadata=room_scope_metadata(guest_credential()),
    )


class _StubTool(Tool):
    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return "stub"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> str:
        return "ran"


# --------------------------------------------------------------------------
# The tool gate (replaces the deleted Tool.available())
# --------------------------------------------------------------------------


def _live_builtin_tool_names() -> list[str]:
    """Every built-in tool the loader actually discovers, by its runtime name.

    Deliberately not a hand-written list: asserting against the policy dict was
    how ``long_task`` -- a name no tool has ever had -- sat in the policy for a
    release while the real tools (``create_goal`` / ``update_goal``) were open.
    """
    names: list[str] = []
    for cls in ToolLoader().discover():
        attr = cls.__dict__.get("name")
        if not isinstance(attr, property) or attr.fget is None:
            continue
        try:
            value = attr.fget(cls.__new__(cls))
        except Exception:
            continue
        if isinstance(value, str) and value:
            names.append(value)
    return sorted(set(names))


def test_the_loader_actually_yields_tool_names() -> None:
    """Guard the guard: if this returns nothing, every test below is vacuous."""
    names = _live_builtin_tool_names()
    assert len(names) >= 15
    # Spot-check a few that must be present for the suite to mean anything.
    for expected in ("read_session", "grep", "message", "spawn", "create_goal"):
        assert expected in names, expected


@pytest.mark.parametrize("name", _live_builtin_tool_names())
def test_every_live_builtin_is_denied_unless_explicitly_allowed(name: str) -> None:
    """The allow-list is the whole policy: unclassified means denied."""
    registry = ToolRegistry()
    registry.register(_StubTool(name))
    with request_context(guest_request_context()):
        _tool, _params, error = registry.prepare_call(name, {})
    if name in ROOM_ALLOWED_TOOLS:
        assert error is None, f"{name} is allow-listed but was denied"
    else:
        assert isinstance(error, ToolResult) and error.is_error, (
            f"{name} is not allow-listed but was ALLOWED in a room"
        )


@pytest.mark.parametrize("name", _live_builtin_tool_names())
def test_every_live_builtin_still_works_outside_a_room(name: str) -> None:
    registry = ToolRegistry()
    registry.register(_StubTool(name))
    ctx = RequestContext(channel="websocket", chat_id=OWNER_CHAT, metadata={})
    with request_context(ctx):
        _tool, _params, error = registry.prepare_call(name, {})
    assert error is None


@pytest.mark.parametrize(
    "name",
    [
        # The owner's shipped connector surface. None of these is a built-in, so
        # a deny-list could never have covered them.
        "mcp_ziggy_gmail_gmail_search",
        "mcp_ziggy_gmail_gmail_get_message",
        "mcp_ziggy_gmail_gmail_send_message",
        "mcp_ziggy_gmail_linkedin_read_page",
        "mcp_ziggy_connectors_work_app_submit_feedback",
        # And an arbitrary future tool.
        "some_tool_that_does_not_exist_yet",
    ],
)
def test_unclassified_and_mcp_tools_are_denied_in_a_room(name: str) -> None:
    registry = ToolRegistry()
    registry.register(_StubTool(name))
    with request_context(guest_request_context()):
        _tool, _params, error = registry.prepare_call(name, {})
    assert isinstance(error, ToolResult) and error.is_error
    assert room_policy_for(name) is RoomPolicy.DENIED


def test_the_allow_list_is_small_and_deliberate() -> None:
    """A large allow-list is the same bug in a different shape."""
    assert set(ROOM_ALLOWED_TOOLS) == {"web_search", "web_fetch", "report_progress"}
    assert all(reason.strip() for reason in ROOM_ALLOWED_TOOLS.values())


def test_the_exploited_tools_from_review_are_denied() -> None:
    """The three demonstrated exploits, pinned as regressions."""
    for name in (
        "mcp_ziggy_gmail_gmail_search",  # mailbox exfiltration
        "message",  # cross-chat delivery with file media
        "grep",  # workspace/memory/ read
        "read_file",
        "find_files",
        "create_goal",  # the real tool "long_task" never was
        "update_goal",
    ):
        assert room_policy_for(name) is RoomPolicy.DENIED, name


def test_a_half_populated_scope_denies_rather_than_falling_open() -> None:
    """A malformed scope is a minting bug; it must not read as 'no room'."""
    registry = ToolRegistry()
    registry.register(_StubTool("web_search"))
    ctx = RequestContext(
        channel="websocket",
        chat_id=ROOM_CHAT,
        metadata={INBOUND_META_ROOM_SCOPE: "not-a-mapping"},
    )
    with request_context(ctx):
        # Even an allow-listed tool is fine, but the scope must resolve to a
        # session key nothing can match.
        _tool, _params, error = registry.prepare_call("web_search", {})
    assert error is None
    assert room_scope(ctx.metadata) is not None
    assert room_scope_session_key(room_scope(ctx.metadata)) == "\0"


# --------------------------------------------------------------------------
# Defence in depth: the session reader itself
# --------------------------------------------------------------------------


def test_guest_cannot_read_another_session(sessions: SessionManager, room) -> None:
    """THE negative case: a guest turn must not resolve the owner's session."""
    from nanobot.webui.session_access import WebuiSessionAccess

    access = WebuiSessionAccess(sessions)
    with request_context(guest_request_context()):
        assert access.read(f"websocket:{OWNER_CHAT}", query="", limit=8) is None
        # Its own room session stays readable.
        assert access.read(f"websocket:{ROOM_CHAT}", query="", limit=8) is not None


def test_guest_cannot_search_other_sessions(sessions: SessionManager, room) -> None:
    from nanobot.webui.session_access import WebuiSessionAccess

    access = WebuiSessionAccess(sessions)
    with request_context(guest_request_context()):
        matches = access.search("hunter2", 5)
    assert all(match["session_key"] != f"websocket:{OWNER_CHAT}" for match in matches)


def test_owner_turn_outside_a_room_is_unrestricted(sessions: SessionManager, room) -> None:
    from nanobot.webui.session_access import WebuiSessionAccess

    access = WebuiSessionAccess(sessions)
    ctx = RequestContext(channel="websocket", chat_id=OWNER_CHAT, metadata={})
    with request_context(ctx):
        assert access.read(f"websocket:{ROOM_CHAT}", query="", limit=8) is not None


def test_guest_cannot_mention_another_session(sessions: SessionManager, room) -> None:
    from nanobot.webui.session_access import WebuiSessionAccess

    access = WebuiSessionAccess(sessions)
    raw = [{"session_key": f"websocket:{OWNER_CHAT}", "name": "planning"}]
    with request_context(guest_request_context()):
        assert access.normalize_mentions(raw) == []


# --------------------------------------------------------------------------
# Content boundary
# --------------------------------------------------------------------------


def test_shareable_messages_drops_everything_not_allowlisted() -> None:
    messages = [
        {
            "role": "user",
            "content": "hello",
            "timestamp": "t0",
            "client_message_id": "c1",
            "_private_tool_context": {"secret": 1},
            "reasoning": "chain of thought",
            "media": ["/home/mihai/private.png"],
        },
        {"role": "system", "content": "system instruction"},
        {"role": "tool", "content": "tool output"},
        {"role": "assistant", "content": "  "},
    ]
    shareable = SessionManager.shareable_messages(messages, "Mihai")
    assert len(shareable) == 1
    row = shareable[0]
    assert set(row) <= SessionManager.SHAREABLE_MESSAGE_FIELDS
    assert "_private_tool_context" not in row
    assert "reasoning" not in row
    assert "media" not in row
    assert row["participant_display_name"] == "Mihai"
    assert row["participant_id"] == "owner"


def test_room_clone_does_not_inherit_owner_metadata(
    sessions: SessionManager,
    owner_session,
) -> None:
    owner_session.metadata["_last_summary"] = "private summary"
    owner_session.metadata["published_file_grants"] = {"x": {"filename": "secret.md"}}
    sessions.save(owner_session, fsync=True)

    clone = sessions.clone_session_for_shared_room(
        f"websocket:{OWNER_CHAT}",
        "websocket:11111111-2222-3333-4444-666666666666",
        metadata={"shared_room": True, "room_id": ROOM_ID},
        owner_display_name="Mihai",
    )
    assert "_last_summary" not in clone.metadata
    assert "published_file_grants" not in clone.metadata
    assert clone.metadata == {"shared_room": True, "room_id": ROOM_ID}


def test_room_clone_rejects_a_changed_snapshot(
    sessions: SessionManager,
    owner_session,
) -> None:
    with pytest.raises(ValueError):
        sessions.clone_session_for_shared_room(
            f"websocket:{OWNER_CHAT}",
            "websocket:11111111-2222-3333-4444-777777777777",
            metadata={"shared_room": True},
            owner_display_name="Mihai",
            snapshot_message_count=2,
            snapshot_sha256="0" * 64,
        )


# --------------------------------------------------------------------------
# Credential lifecycle
# --------------------------------------------------------------------------


def test_credential_authorizes_only_its_own_session(store: SharedRoomStore) -> None:
    credential = guest_credential()
    assert store.authorizes_session(credential, f"websocket:{ROOM_CHAT}")
    assert not store.authorizes_session(credential, f"websocket:{OWNER_CHAT}")
    assert not store.authorizes_session(None, f"websocket:{ROOM_CHAT}")


def test_ws_token_is_single_use(store: SharedRoomStore) -> None:
    token, _ = store.mint(
        room_id=ROOM_ID,
        chat_id=ROOM_CHAT,
        participant_id=PARTICIPANT_ID,
        display_name="Guest",
        role="contributor",
    )
    conn = object()
    assert store.consume_ws_token(conn, token) is True
    assert store.consume_ws_token(object(), token) is False
    # The REST copy survives the handshake.
    assert store.api_credential(token) is not None
    assert store.connection_credential(conn) is not None


def test_revocation_invalidates_tokens_and_connections(
    sessions: SessionManager,
    store: SharedRoomStore,
) -> None:
    token, _ = store.mint(
        room_id=ROOM_ID,
        chat_id=ROOM_CHAT,
        participant_id=PARTICIPANT_ID,
        display_name="Guest",
        role="contributor",
    )
    conn = object()
    store.consume_ws_token(conn, token)
    invalidated, connections = store.revoke(room_id=ROOM_ID, chat_id=ROOM_CHAT)
    assert invalidated == 1  # the ws copy was already consumed
    assert connections == [conn]
    assert store.api_credential(token) is None
    # The credential is MARKED, not dropped: a revoked guest must never read as
    # "not a guest" while its socket is still closing, or it is promoted to
    # owner downstream.
    assert store.is_revoked(conn) is True
    assert store.connection_credential(conn) is not None
    store.forget_connection(conn)
    assert store.connection_credential(conn) is None
    assert store.is_revoked(conn) is False


def test_revoked_room_is_inactive(sessions: SessionManager, store: SharedRoomStore) -> None:
    assert store.is_active(ROOM_CHAT) is True
    session = sessions.get_or_create(f"websocket:{ROOM_CHAT}")
    session.metadata["shared_room_revoked"] = True
    sessions.save(session, fsync=True)
    sessions.invalidate(f"websocket:{ROOM_CHAT}")
    assert store.is_active(ROOM_CHAT) is False


def test_expired_room_is_inactive(sessions: SessionManager, store: SharedRoomStore) -> None:
    session = sessions.get_or_create(f"websocket:{ROOM_CHAT}")
    session.metadata["shared_room_expires_at"] = (
        datetime.now(timezone.utc) - timedelta(minutes=1)
    ).isoformat()
    sessions.save(session, fsync=True)
    assert store.is_active(ROOM_CHAT) is False


def test_unparseable_expiry_fails_closed(
    sessions: SessionManager,
    store: SharedRoomStore,
) -> None:
    session = sessions.get_or_create(f"websocket:{ROOM_CHAT}")
    session.metadata["shared_room_expires_at"] = "not-a-timestamp"
    sessions.save(session, fsync=True)
    assert store.is_active(ROOM_CHAT) is False


def test_expired_token_is_not_accepted(sessions: SessionManager, room) -> None:
    store = SharedRoomStore(sessions, token_ttl_s=-1)
    token, _ = store.mint(
        room_id=ROOM_ID,
        chat_id=ROOM_CHAT,
        participant_id=PARTICIPANT_ID,
        display_name="Guest",
        role="contributor",
    )
    assert store.api_credential(token) is None
    assert store.consume_ws_token(object(), token) is False


def test_non_room_chat_is_always_active(sessions: SessionManager, owner_session) -> None:
    store = SharedRoomStore(sessions, token_ttl_s=300)
    assert store.is_active(OWNER_CHAT) is True
    assert store.owner_credential(OWNER_CHAT) is None


def test_owner_credential_is_scoped_to_its_room(store: SharedRoomStore) -> None:
    credential = store.owner_credential(ROOM_CHAT)
    assert credential is not None
    assert credential.role == "owner"
    assert credential.participant_id == "owner"
    assert credential.chat_id == ROOM_CHAT
    assert credential.display_name == "Mihai"


# --------------------------------------------------------------------------
# Title revision protocol
# --------------------------------------------------------------------------


def test_owner_rename_refuses_a_shared_room(sessions: SessionManager, room) -> None:
    assert sessions.set_session_title(f"websocket:{ROOM_CHAT}", "New") == "shared"


def test_older_title_revision_loses(sessions: SessionManager, room) -> None:
    key = f"websocket:{ROOM_CHAT}"
    assert sessions.set_session_title(key, "v2", room_id=ROOM_ID, title_revision=2) == "updated"
    assert sessions.set_session_title(key, "v1", room_id=ROOM_ID, title_revision=1) == "older"
    assert sessions.get_or_create(key).metadata["title"] == "v2"


def test_wrong_room_id_cannot_rename(sessions: SessionManager, room) -> None:
    result = sessions.set_session_title(
        f"websocket:{ROOM_CHAT}",
        "hijack",
        room_id="room_" + "c" * 32,
        title_revision=9,
    )
    assert result == "missing"


# --------------------------------------------------------------------------
# Scope minting: the metadata must come from a validated credential
# --------------------------------------------------------------------------


class _FakeChannel:
    """The slice of ``WebSocketChannel`` the minting helpers use."""

    def __init__(self, store: SharedRoomStore) -> None:
        self.rooms = store

    room_credential = staticmethod(lambda connection: None)

    def effective_room_credential(self, connection: Any, chat_id: str):
        from nanobot.channels.websocket.runtime import WebSocketChannel

        return WebSocketChannel.effective_room_credential(self, connection, chat_id)

    def room_turn_metadata(self, connection: Any, chat_id: str) -> dict[str, Any]:
        from nanobot.channels.websocket.runtime import WebSocketChannel

        return WebSocketChannel.room_turn_metadata(self, connection, chat_id)

    _denied_room_metadata = staticmethod(
        __import__(
            "nanobot.channels.websocket.runtime", fromlist=["WebSocketChannel"]
        ).WebSocketChannel._denied_room_metadata
    )


def test_room_metadata_is_minted_for_an_owner_turn(store: SharedRoomStore) -> None:
    channel = _FakeChannel(store)
    metadata = channel.room_turn_metadata(object(), ROOM_CHAT)
    assert metadata["shared_room"] is True
    assert metadata["room_id"] == ROOM_ID
    assert metadata[INBOUND_META_ROOM_SCOPE]["chat_id"] == ROOM_CHAT
    assert metadata[INBOUND_META_ROOM_SCOPE]["role"] == "owner"


def test_room_metadata_is_minted_for_a_guest_turn(store: SharedRoomStore) -> None:
    token, _ = store.mint(
        room_id=ROOM_ID,
        chat_id=ROOM_CHAT,
        participant_id=PARTICIPANT_ID,
        display_name="Guest",
        role="contributor",
    )
    conn = object()
    assert store.consume_ws_token(conn, token)
    channel = _FakeChannel(store)
    channel.room_credential = store.connection_credential
    metadata = channel.room_turn_metadata(conn, ROOM_CHAT)
    assert metadata[INBOUND_META_ROOM_SCOPE]["participant_id"] == PARTICIPANT_ID
    assert metadata[INBOUND_META_ROOM_SCOPE]["role"] == "contributor"
    assert metadata["participant_display_name"] == "Guest"


def test_no_room_metadata_outside_a_room(
    sessions: SessionManager,
    owner_session,
) -> None:
    channel = _FakeChannel(SharedRoomStore(sessions, token_ttl_s=300))
    assert channel.room_turn_metadata(object(), OWNER_CHAT) == {}


def test_no_room_metadata_for_a_revoked_room(
    sessions: SessionManager,
    store: SharedRoomStore,
) -> None:
    session = sessions.get_or_create(f"websocket:{ROOM_CHAT}")
    session.metadata["shared_room_revoked"] = True
    sessions.save(session, fsync=True)
    channel = _FakeChannel(store)
    metadata = channel.room_turn_metadata(object(), ROOM_CHAT)
    # Fail closed, not open: a revoked room yields a deny-everything scope so
    # prepare_call still gates the turn.
    assert metadata["shared_room"] is True
    assert metadata[INBOUND_META_ROOM_SCOPE] == {
        "room_id": "",
        "chat_id": "",
        "participant_id": "",
        "role": "guest",
    }


def test_a_guest_credential_cannot_mint_scope_for_another_chat(
    store: SharedRoomStore,
) -> None:
    token, _ = store.mint(
        room_id=ROOM_ID,
        chat_id=ROOM_CHAT,
        participant_id=PARTICIPANT_ID,
        display_name="Guest",
        role="contributor",
    )
    conn = object()
    store.consume_ws_token(conn, token)
    channel = _FakeChannel(store)
    channel.room_credential = store.connection_credential
    metadata = channel.room_turn_metadata(conn, OWNER_CHAT)
    assert metadata[INBOUND_META_ROOM_SCOPE]["chat_id"] == ""


# --------------------------------------------------------------------------
# Room intents: owners use the same path as guests
# --------------------------------------------------------------------------


class _IntentChannel(_FakeChannel):
    """Enough channel surface for ``handle_room_intent``."""

    def __init__(self, store: SharedRoomStore, sessions: SessionManager) -> None:
        super().__init__(store)
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.broadcasts: list[tuple[str, str, dict[str, Any]]] = []

        class _Gateway:
            session_manager = sessions

        self.gateway = _Gateway()

    async def _send_event(self, connection: Any, event: str, **fields: Any) -> None:
        self.events.append((event, fields))

    async def broadcast_room_event(self, chat_id: str, event: str, **fields: Any) -> None:
        self.broadcasts.append((chat_id, event, fields))

    def webui_subscribers(self, chat_id: str):
        return ()

    def room_work_store(self):
        raise AssertionError("not used by these tests")


def _collaborative(sessions: SessionManager) -> None:
    session = sessions.get_or_create(f"websocket:{ROOM_CHAT}")
    session.metadata["room_mode"] = "collaborative-v1"
    sessions.save(session, fsync=True)


@pytest.mark.asyncio
async def test_owner_discussion_is_recorded_and_broadcast(
    sessions: SessionManager,
    store: SharedRoomStore,
) -> None:
    """The snapshot routes owner frames through the intent path too."""
    from nanobot.channels.websocket.room_editorial import handle_room_intent

    _collaborative(sessions)
    channel = _IntentChannel(store, sessions)
    owner = store.owner_credential(ROOM_CHAT)
    assert owner is not None

    handled = await handle_room_intent(
        channel,
        object(),
        owner,
        {"room_intent": "discussion", "client_message_id": "c1", "content": "hi all"},
    )
    assert handled is True
    assert [e for e, _ in channel.events] == ["message.ack"]
    assert channel.events[0][1]["status"] == "accepted"
    assert [name for _c, name, _f in channel.broadcasts] == ["participant.message"]

    stored = sessions.get_or_create(f"websocket:{ROOM_CHAT}").messages[-1]
    assert stored["content"] == "hi all"
    assert stored["participant_id"] == "owner"
    assert stored["room_intent"] == "discussion"


@pytest.mark.asyncio
async def test_ask_ziggy_falls_through_to_a_normal_turn(
    sessions: SessionManager,
    store: SharedRoomStore,
) -> None:
    from nanobot.channels.websocket.room_editorial import handle_room_intent

    _collaborative(sessions)
    channel = _IntentChannel(store, sessions)
    handled = await handle_room_intent(
        channel,
        object(),
        guest_credential(),
        {"room_intent": "ask_ziggy", "client_message_id": "c2", "content": "what now?"},
    )
    assert handled is False
    assert channel.events == []


@pytest.mark.asyncio
async def test_replayed_id_with_different_content_is_rejected(
    sessions: SessionManager,
    store: SharedRoomStore,
) -> None:
    from nanobot.channels.websocket.room_editorial import handle_room_intent

    _collaborative(sessions)
    channel = _IntentChannel(store, sessions)
    credential = guest_credential()
    envelope = {"room_intent": "discussion", "client_message_id": "c3", "content": "first"}
    await handle_room_intent(channel, object(), credential, envelope)

    channel.events.clear()
    await handle_room_intent(channel, object(), credential, dict(envelope))
    assert channel.events[0][1]["status"] == "duplicate"

    channel.events.clear()
    await handle_room_intent(
        channel,
        object(),
        credential,
        {**envelope, "content": "rewritten"},
    )
    assert channel.events[0][1]["status"] == "rejected"


@pytest.mark.asyncio
async def test_revoked_room_refuses_an_intent(
    sessions: SessionManager,
    store: SharedRoomStore,
) -> None:
    from nanobot.channels.websocket.room_editorial import handle_room_intent

    _collaborative(sessions)
    session = sessions.get_or_create(f"websocket:{ROOM_CHAT}")
    session.metadata["shared_room_revoked"] = True
    sessions.save(session, fsync=True)

    channel = _IntentChannel(store, sessions)
    handled = await handle_room_intent(
        channel,
        object(),
        guest_credential(),
        {"room_intent": "discussion", "client_message_id": "c4", "content": "hello"},
    )
    assert handled is True
    assert channel.events[0][0] == "error"
    assert channel.events[0][1]["detail"] == "Room expired or revoked"


@pytest.mark.asyncio
async def test_unsupported_intent_is_refused(
    sessions: SessionManager,
    store: SharedRoomStore,
) -> None:
    from nanobot.channels.websocket.room_editorial import handle_room_intent

    _collaborative(sessions)
    channel = _IntentChannel(store, sessions)
    handled = await handle_room_intent(
        channel,
        object(),
        guest_credential(),
        {"room_intent": "delete_everything", "client_message_id": "c5", "content": "x"},
    )
    assert handled is True
    assert channel.events[0][1]["detail"] == "Unsupported room intent"


@pytest.mark.asyncio
async def test_legacy_room_does_not_use_the_intent_path(
    sessions: SessionManager,
    store: SharedRoomStore,
) -> None:
    """A legacy (non-collaborative) room posts as a normal turn."""
    from nanobot.channels.websocket.room_editorial import handle_room_intent

    channel = _IntentChannel(store, sessions)
    handled = await handle_room_intent(
        channel,
        object(),
        guest_credential(),
        {"room_intent": "discussion", "client_message_id": "c6", "content": "hi"},
    )
    assert handled is False


# --------------------------------------------------------------------------
# Subagents must not escape the room
# --------------------------------------------------------------------------


def test_a_subagent_inherits_the_rooms_scope() -> None:
    """Without this a guest escapes the gate by spawning: the subagent's
    RequestContext is built fresh and would carry no room scope at all."""
    from nanobot.agent.subagent import _inherited_room_scope

    with request_context(guest_request_context()):
        inherited = _inherited_room_scope()
    assert inherited is not None
    assert inherited["chat_id"] == ROOM_CHAT
    assert inherited["room_id"] == ROOM_ID


def test_no_room_scope_is_inherited_outside_a_room() -> None:
    from nanobot.agent.subagent import _inherited_room_scope

    with request_context(RequestContext(channel="websocket", chat_id=OWNER_CHAT)):
        assert _inherited_room_scope() is None
    assert _inherited_room_scope() is None


def test_an_inherited_scope_still_blocks_denied_tools() -> None:
    """End-to-end: the scope a subagent inherits gates its tools too."""
    from nanobot.agent.subagent import _inherited_room_scope

    with request_context(guest_request_context()):
        inherited = _inherited_room_scope()

    registry = ToolRegistry()
    registry.register(_StubTool("read_session"))
    subagent_ctx = RequestContext(
        channel="websocket",
        chat_id=ROOM_CHAT,
        metadata={INBOUND_META_ROOM_SCOPE: inherited},
    )
    with request_context(subagent_ctx):
        _tool, _params, error = registry.prepare_call("read_session", {})
    assert isinstance(error, ToolResult) and error.is_error


def test_fan_out_tools_are_denied_in_a_room() -> None:
    registry = ToolRegistry()
    for name in ("spawn", "long_task"):
        registry.register(_StubTool(name))
    with request_context(guest_request_context()):
        for name in ("spawn", "long_task"):
            _tool, _params, error = registry.prepare_call(name, {})
            assert isinstance(error, ToolResult) and error.is_error, name
