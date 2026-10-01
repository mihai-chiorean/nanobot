"""Worker turns journal their ``user_message`` when a ``client_message_id`` is present (MIT-1622).

The ziggy-worker posts into a WebUI chat over the same ``message`` envelope
without the ``webui`` flag, carrying a ``client_message_id`` for exactly-once
delivery (MIT-1402). The transcript-journaling call in ``_dispatch_message``
was gated on ``is_webui`` alone, so a worker turn's ``user_message`` never
reached the transcript journal; it stayed visible only through the legacy
``/messages`` session-file read, which is what keeps the ``/webui-thread``
events path falling back to that route. The gate must widen to
``is_webui or client_message_id is not None`` -- and only there: a worker
frame without an id keeps the current (unjournaled) behavior, and an ordinary
webui turn journals exactly as before.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.channels.websocket.runtime import WebSocketChannel, WebSocketConfig
from nanobot.session.manager import SessionManager
from nanobot.webui.gateway_services import build_gateway_services
from nanobot.webui.transcript import read_transcript_lines

CHAT = "worker_journal_chat"
SESSION_KEY = f"websocket:{CHAT}"
CLIENT_MESSAGE_ID = "3f2a9c1e-58d4-4b6f-9e2c-7d0a1b3c5e6f"
WEBUI_CHAT = "owner_journal_chat"
WEBUI_SESSION_KEY = f"websocket:{WEBUI_CHAT}"


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data_dir = tmp_path / "data"
    monkeypatch.setattr("nanobot.config.paths.get_data_dir", lambda: data_dir)
    return data_dir


def _make_channel(workspace: Path) -> WebSocketChannel:
    bus = MagicMock()
    bus.publish_inbound = AsyncMock()
    bus.publish_outbound = AsyncMock()
    config = WebSocketConfig.model_validate({
        "enabled": True,
        "allowFrom": ["*"],
        "websocketRequiresToken": False,
    })
    gateway = build_gateway_services(
        config=config,
        bus=bus,
        session_manager=SessionManager(workspace),
        static_dist_path=None,
        workspace_path=workspace,
        default_restrict_to_workspace=False,
        runtime_model_name=None,
        runtime_surface="browser",
        runtime_capabilities_overrides=None,
    )
    return WebSocketChannel(config, bus, gateway=gateway)


def _frame(
    *,
    chat_id: str,
    content: str,
    client_message_id: str | None = None,
    webui: bool = False,
) -> dict[str, Any]:
    frame: dict[str, Any] = {"type": "message", "chat_id": chat_id, "content": content}
    if client_message_id is not None:
        frame["client_message_id"] = client_message_id
    if webui:
        frame["webui"] = True
    return frame


def _user_lines(session_key: str) -> list[dict[str, Any]]:
    return [line for line in read_transcript_lines(session_key) if line.get("event") == "user"]


@pytest.mark.asyncio
async def test_worker_turn_with_client_message_id_is_journaled(tmp_path: Path) -> None:
    """The MIT-1622 gap: worker frame (no ``webui`` flag) + id must land in the journal."""
    channel = _make_channel(tmp_path)

    await channel._dispatch_envelope(  # pyright: ignore[reportPrivateUsage]
        AsyncMock(),
        "worker-1",
        _frame(
            chat_id=CHAT,
            content="rebuild the index",
            client_message_id=CLIENT_MESSAGE_ID,
        ),
    )

    users = _user_lines(SESSION_KEY)
    assert users, (
        "worker turn's user_message never reached the transcript journal: "
        f"{read_transcript_lines(SESSION_KEY)}"
    )
    assert users[0].get("text") == "rebuild the index"
    assert users[0].get("client_message_id") == CLIENT_MESSAGE_ID


@pytest.mark.asyncio
async def test_worker_turn_without_client_message_id_is_not_journaled(tmp_path: Path) -> None:
    """Regression: the no-id worker frame keeps the pre-fix behavior (no journaling)."""
    channel = _make_channel(tmp_path)

    await channel._dispatch_envelope(  # pyright: ignore[reportPrivateUsage]
        AsyncMock(),
        "worker-1",
        _frame(chat_id=CHAT, content="untracked side note"),
    )

    channel.bus.publish_inbound.assert_awaited_once()
    assert _user_lines(SESSION_KEY) == []


@pytest.mark.asyncio
async def test_webui_turn_journals_with_client_message_id(tmp_path: Path) -> None:
    """Regression: an ordinary webui turn with an id journals as before."""
    channel = _make_channel(tmp_path)

    await channel._dispatch_envelope(  # pyright: ignore[reportPrivateUsage]
        AsyncMock(),
        "owner-1",
        _frame(
            chat_id=WEBUI_CHAT,
            content="pair the garage",
            client_message_id=CLIENT_MESSAGE_ID,
            webui=True,
        ),
    )

    users = _user_lines(WEBUI_SESSION_KEY)
    assert users, read_transcript_lines(WEBUI_SESSION_KEY)
    assert users[0].get("text") == "pair the garage"
    assert users[0].get("client_message_id") == CLIENT_MESSAGE_ID


@pytest.mark.asyncio
async def test_webui_turn_journals_without_client_message_id(tmp_path: Path) -> None:
    """Regression: an ordinary webui turn with no id journals as before, no id key."""
    channel = _make_channel(tmp_path)

    await channel._dispatch_envelope(  # pyright: ignore[reportPrivateUsage]
        AsyncMock(),
        "owner-1",
        _frame(chat_id=WEBUI_CHAT, content="remember the milk", webui=True),
    )

    users = _user_lines(WEBUI_SESSION_KEY)
    assert users, read_transcript_lines(WEBUI_SESSION_KEY)
    assert users[0].get("text") == "remember the milk"
    assert "client_message_id" not in users[0]
