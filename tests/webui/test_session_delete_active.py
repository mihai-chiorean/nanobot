"""``session.delete`` with a running turn -> 409, session stays (MIT-1416).

0.3.0 parity. Production (``feat/shared-rooms`` 5cdea416) refuses the delete
while the conversation has a turn in flight: the route returns HTTP 409 with
body ``conversation is active`` and leaves the session file in place. The
0.3.0 delete handler deleted unconditionally, so an in-flight turn kept
writing transcript rows into a session that no longer existed.

The gate reads the gateway's WebUI turn registry -- the same
``websocket_turn_wall_started_at`` signal the sessions list reports as
``run_started_at`` and the messages route reports as ``active_turn_*``.
Turns are opened and closed through the registry's own entry points
(``register_queued_websocket_turn_if_idle`` /
``clear_websocket_turn_if_current``, the pair the WebSocket ingress and the
turn-finalisation path use), and the delete is issued the way the WebUI/iOS
client issues it after 5d733b1c moved mutations off plain HTTP: through the
authenticated-WS mutation dispatcher.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Callable
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.session.manager import SessionManager
from nanobot.session.webui_turns import (
    clear_websocket_turn_if_current,
    register_queued_websocket_turn_if_idle,
    websocket_turn_wall_started_at,
)


@pytest.fixture(autouse=True)
def _isolate_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("nanobot.config.paths.get_data_dir", lambda: tmp_path)


@pytest.fixture
def active_turn() -> Iterator[Callable[[str], tuple[Callable[[], None], ...]]]:
    """Open gateway-tracked turns for a chat id; return ``(stop,)``.

    The registry entry the UI shows as a running turn stays in place until
    ``clear_websocket_turn_if_current`` confirms the exact owner, mirroring the
    turn-finalisation path. ``stop()`` closes the turn through that entry
    point; anything left open is cleared (and asserted) at teardown.
    """
    opened: list[tuple[str, str]] = []

    def _open(chat_id: str) -> tuple[Callable[[], None], ...]:
        owner = register_queued_websocket_turn_if_idle(chat_id, f"turn-{chat_id}")
        assert owner is not None, f"a turn was already active for {chat_id}"
        opened.append((chat_id, owner))

        def _stop() -> None:
            assert clear_websocket_turn_if_current(chat_id, owner) is True
            opened.remove((chat_id, owner))

        return (_stop,)

    yield _open

    for chat_id, owner in opened:
        assert clear_websocket_turn_if_current(chat_id, owner) is True


def _chat_id() -> str:
    return f"del-{uuid.uuid4().hex[:12]}"


def _seed(session_manager: SessionManager, chat_id: str) -> str:
    key = f"websocket:{chat_id}"
    session = session_manager.get_or_create(key)
    session.add_message("user", "hello")
    session.add_message("assistant", "hi")
    session_manager.save(session)
    return key


def _channel(session_manager: SessionManager) -> Any:
    from nanobot.channels.websocket.tests.test_websocket_http_routes import _ch, _free_port

    bus = MagicMock()
    bus.publish_inbound = AsyncMock()
    return _ch(bus, session_manager=session_manager, port=_free_port())


async def _delete(channel: Any, key: str) -> Any:
    """Delete the way the product does: the authenticated-WS mutation route."""
    from nanobot.channels.websocket.tests.test_websocket_http_routes import _webui_mutate

    return await _webui_mutate(channel, "session.delete", {"key": key})


@pytest.mark.asyncio
async def test_delete_with_active_turn_returns_409_and_keeps_the_session(
    tmp_path: Path,
    active_turn: Callable[[str], tuple[Callable[[], None], ...]],
) -> None:
    """Case 1 (fails on pre-fix ``ziggy-main``: the handler deleted anyway).

    A gateway-tracked turn is in flight for the key; the delete must answer
    409 with production's exact body and leave the session file and its
    transcript rows untouched, so the running turn keeps writing into a
    session that still exists.
    """
    from nanobot.webui.transcript import append_transcript_object

    sessions = SessionManager(tmp_path / "ws")
    chat_id = _chat_id()
    key = _seed(sessions, chat_id)
    append_transcript_object(key, {"event": "user", "chat_id": chat_id, "text": "hello"})
    path = sessions._get_session_path(key)
    webui_path = tmp_path / "webui" / f"{SessionManager.safe_key(key)}.jsonl"
    assert path.is_file()
    assert webui_path.is_file()

    active_turn(chat_id)
    channel = _channel(sessions)
    server_task = asyncio.create_task(channel.start())
    try:
        response = await _delete(channel, key)

        assert response.status_code == 409, response.content
        assert response.content == b"conversation is active"
        assert path.is_file(), "the running turn's session must survive"
        assert webui_path.is_file(), "the running turn's transcript rows must survive"
        assert websocket_turn_wall_started_at(chat_id) is not None, (
            "the rejected delete must not cancel or drop the in-flight turn"
        )
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_delete_after_the_turn_finishes_succeeds(
    tmp_path: Path,
    active_turn: Callable[[str], tuple[Callable[[], None], ...]],
) -> None:
    """Case 2: once the registry clears, the same delete succeeds."""
    sessions = SessionManager(tmp_path / "ws")
    chat_id = _chat_id()
    key = _seed(sessions, chat_id)
    path = sessions._get_session_path(key)

    (stop,) = active_turn(chat_id)  # the gate sees a live turn from here on
    stop()  # the turn finalises through the same clear path the runtime uses

    channel = _channel(sessions)
    server_task = asyncio.create_task(channel.start())
    try:
        response = await _delete(channel, key)

        assert response.status_code == 200, response.content
        assert response.json() == {"deleted": True}
        assert not path.exists()
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_delete_idle_session_is_unchanged(
    tmp_path: Path,
) -> None:
    """Case 3: a never-registered (idle) conversation deletes as before."""
    sessions = SessionManager(tmp_path / "ws")
    key = _seed(sessions, _chat_id())
    path = sessions._get_session_path(key)

    channel = _channel(sessions)
    server_task = asyncio.create_task(channel.start())
    try:
        response = await _delete(channel, key)

        assert response.status_code == 200, response.content
        assert response.json() == {"deleted": True}
        assert not path.exists()
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_active_turn_on_another_conversation_does_not_block(
    tmp_path: Path,
    active_turn: Callable[[str], tuple[Callable[[], None], ...]],
) -> None:
    """Negative control: the gate is keyed on the deleted conversation only.

    A live turn in a different chat must not wedge the delete of an idle one
    (an over-broad gate would look identical to case 1 from the client).
    """
    sessions = SessionManager(tmp_path / "ws")
    busy_key = _seed(sessions, _chat_id())
    assert busy_key.startswith("websocket:")
    idle_key = _seed(sessions, _chat_id())
    path = sessions._get_session_path(idle_key)

    active_turn(busy_key.removeprefix("websocket:"))
    channel = _channel(sessions)
    server_task = asyncio.create_task(channel.start())
    try:
        response = await _delete(channel, idle_key)

        assert response.status_code == 200, response.content
        assert response.json() == {"deleted": True}
        assert not path.exists()
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_unauthenticated_delete_of_active_session_still_401(
    tmp_path: Path,
    active_turn: Callable[[str], tuple[Callable[[], None], ...]],
) -> None:
    """Ordering control: the owner-token check precedes the active-turn gate.

    The gate must not turn an unauthorized delete into a 409 information leak
    about which conversations are running; a request with no owner token is
    refused with 401 even while a turn is in flight for the key.
    """
    from nanobot.channels.websocket.transport import TransportRequest

    sessions = SessionManager(tmp_path / "ws")
    chat_id = _chat_id()
    key = _seed(sessions, chat_id)
    active_turn(chat_id)

    channel = _channel(sessions)
    server_task = asyncio.create_task(channel.start())
    try:
        path = f"/api/sessions/{key}/delete"
        anonymous = TransportRequest(method="GET", path=path, headers={}, body=b"", raw_path=path)
        response = channel.gateway.http._handle_session_delete(anonymous, key)

        assert response.status_code == 401, response.body
        assert sessions._get_session_path(key).is_file()
    finally:
        await channel.stop()
        await server_task
