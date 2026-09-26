"""``/webui-thread`` exposes the persisted ``client_message_id`` on user turns (MIT-1056).

The edge client overlays its own in-flight operations on the gateway
transcript returned by this route, and the overlay is keyed by
``client_message_id`` -- the same idempotency key ``/messages`` carries -- so
matching is by identity rather than by rendered text (the text matching that
preceded this field caused the repeated-text message-loss bugs fixed in PR #9).

The field is additive: a turn that has no stored id must not grow the key
(older clients keep working), and a malformed stored value (a room guest
controls its envelope field) is omitted rather than echoed back.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from nanobot.bus.queue import MessageBus
from nanobot.channels.websocket.runtime import WebSocketChannel, WebSocketConfig
from nanobot.channels.websocket.transport import TransportRequest
from nanobot.session.manager import SessionManager
from nanobot.webui.gateway_services import build_gateway_services
from nanobot.webui.transcript import (
    WebUITranscriptRecorder,
    append_transcript_object,
    read_transcript_lines,
    write_session_messages_as_transcript,
)

CHAT = "chat_client_id"
SESSION_KEY = f"websocket:{CHAT}"
CLIENT_MESSAGE_ID = "3f2a9c1e-58d4-4b6f-9e2c-7d0a1b3c5e6f"
OTHER_CLIENT_MESSAGE_ID = "9a8b7c6d-0e1f-4a2b-8c3d-5e6f7a8b9c0d"


class _Headers(dict):
    """Minimal case-insensitive headers view (real HTTP uses ``Headers``)."""

    def get(self, key: str, default: Any = None) -> Any:  # type: ignore[override]
        for name, value in self.items():
            if name.lower() == key.lower():
                return value
        return default


class _Connection:
    remote_address = ("127.0.0.1", 41000)

    def respond(self, status: int, text: str) -> Any:
        return (status, text)


def _config() -> WebSocketConfig:
    return WebSocketConfig.model_validate(
        {
            "enabled": True,
            "allowFrom": ["*"],
            "host": "127.0.0.1",
            "port": 18998,
            "path": "/ws",
            "websocketRequiresToken": False,
            "tokenIssueSecret": "client-id-issue-secret",
            "sharedRoomsEnabled": False,
        }
    )


def _build(sessions: SessionManager, workspace: Path) -> Any:
    bus = MessageBus()
    gateway = build_gateway_services(
        config=_config(),
        bus=bus,
        session_manager=sessions,
        static_dist_path=None,
        workspace_path=workspace,
        default_restrict_to_workspace=False,
        runtime_model_name=None,
        runtime_surface="browser",
        runtime_capabilities_overrides=None,
    )
    return WebSocketChannel(_config(), bus, gateway=gateway)


def _thread_message_types(items: list[dict[str, Any]]) -> list[str]:
    return [str(item.get("type", "")) for item in items]


async def _read_thread(client: Any) -> list[dict[str, Any]]:
    token = client.gateway.tokens.issue_api_token(60)
    response = await client._dispatch_http(
        _Connection(),
        TransportRequest(
            method="GET",
            path=f"/api/sessions/{SESSION_KEY.replace(':', '%3A')}/webui-thread",
            headers=_Headers({"Authorization": f"Bearer {token}"}),
            body=b"",
            raw_path=f"/api/sessions/{SESSION_KEY.replace(':', '%3A')}/webui-thread",
        ),
    )
    assert response is not None
    assert response.status_code == 200
    body = json.loads(bytes(response.body).decode())
    return list(body["messages"])


@pytest.fixture
def isolated_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data_dir = tmp_path / "data"
    monkeypatch.setattr("nanobot.config.paths.get_data_dir", lambda: data_dir)
    return data_dir


@pytest.fixture
async def webui_client(
    isolated_data_dir: Path, tmp_path: Path
) -> Any:
    workspace = isolated_data_dir / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    sessions = SessionManager(workspace, sessions_root=isolated_data_dir / "sessions")
    client = _build(sessions, workspace)
    yield client
    await client.stop()


# ---------------------------------------------------------------------------
# Storage: the write paths persist the validated field; non-conforming values
# are never journaled (a room guest controls its envelope field).
# ---------------------------------------------------------------------------


def test_webui_thread_recorder_journals_client_message_id(
    isolated_data_dir: Path,
) -> None:
    recorder = WebUITranscriptRecorder()
    assert recorder.append_user_message(
        CHAT,
        "pair the garage",
        metadata={"client_message_id": CLIENT_MESSAGE_ID},
    )
    lines = read_transcript_lines(SESSION_KEY)
    assert any(
        line.get("event") == "user"
        and line.get("client_message_id") == CLIENT_MESSAGE_ID
        for line in lines
    ), lines


def test_webui_thread_recorder_omits_absent_or_invalid_client_message_id(
    isolated_data_dir: Path,
) -> None:
    recorder = WebUITranscriptRecorder()
    assert recorder.append_user_message(CHAT, "no id here", metadata={})
    assert recorder.append_user_message(
        CHAT, "junk id", metadata={"client_message_id": "   "}
    )
    assert recorder.append_user_message(
        CHAT, "also junk", metadata={"client_message_id": 99}
    )
    lines = read_transcript_lines(SESSION_KEY)
    user_lines = [line for line in lines if line.get("event") == "user"]
    assert len(user_lines) == 3
    assert "client_message_id" not in user_lines[0]
    assert "client_message_id" not in user_lines[1]
    assert "client_message_id" not in user_lines[2]


def test_webui_thread_session_transcript_prefix_replays_id_from_start(
    isolated_data_dir: Path,
) -> None:
    # A transcript rewritten from the session store must keep the key on each
    # user row positionally -- the fallback route rebuilds history from it and
    # the client matcher pairs by occurrence, not by scan.
    write_session_messages_as_transcript(
        SESSION_KEY,
        [
            {"role": "user", "content": "first", "client_message_id": CLIENT_MESSAGE_ID},
            {"role": "assistant", "content": "first reply"},
            {"role": "user", "content": "second"},
            {"role": "assistant", "content": "second reply"},
        ],
    )
    lines = read_transcript_lines(SESSION_KEY)
    user_lines = [line for line in lines if line.get("event") == "user"]
    assert [line.get("client_message_id") for line in user_lines] == [
        CLIENT_MESSAGE_ID,
        None,
    ]
    assert "client_message_id" not in user_lines[1]


# ---------------------------------------------------------------------------
# Playback: the route surfaces the field per item; absence must degrade to no
# key, never a fabrication (the client would pair a turn with the wrong op).
# The assistant row never carries it, whatever the store holds.
# ---------------------------------------------------------------------------


def test_webui_thread_response_exposes_client_message_id_on_user_turns(
    isolated_data_dir: Path,
) -> None:
    from nanobot.webui.transcript import build_webui_thread_response

    append_transcript_object(
        SESSION_KEY,
        {
            "event": "user",
            "chat_id": CHAT,
            "text": "pair the garage",
            "client_message_id": CLIENT_MESSAGE_ID,
        },
    )
    append_transcript_object(
        SESSION_KEY,
        {"event": "message", "chat_id": CHAT, "text": "on it", "client_message_id": CLIENT_MESSAGE_ID},
    )
    response = build_webui_thread_response(SESSION_KEY)
    assert response is not None
    messages = response["messages"]
    user = next(item for item in messages if item.get("role") == "user")
    assistant = next(item for item in messages if item.get("role") == "assistant")
    assert user.get("client_message_id") == CLIENT_MESSAGE_ID
    assert "client_message_id" not in assistant


def test_webui_thread_response_backfills_client_message_id_from_store(
    isolated_data_dir: Path,
) -> None:
    from nanobot.webui.transcript import build_webui_thread_response

    # A turn whose journal line predated the field (or whose journal was
    # trimmed) still reports the id when the session store carries it.
    append_transcript_object(
        SESSION_KEY,
        {"event": "message", "chat_id": CHAT, "text": "noted"},
    )
    response = build_webui_thread_response(
        SESSION_KEY,
        session_messages=[
            {
                "role": "user",
                "content": "flag this",
                "client_message_id": OTHER_CLIENT_MESSAGE_ID,
            },
            {"role": "assistant", "content": "noted"},
        ],
    )
    assert response is not None
    user = next(item for item in response["messages"] if item.get("role") == "user")
    assert user.get("client_message_id") == OTHER_CLIENT_MESSAGE_ID


def test_webui_thread_response_omits_field_when_client_message_id_absent(
    isolated_data_dir: Path,
) -> None:
    from nanobot.webui.transcript import build_webui_thread_response

    append_transcript_object(
        SESSION_KEY,
        {"event": "user", "chat_id": CHAT, "text": "legacy turn, no id"},
    )
    append_transcript_object(
        SESSION_KEY,
        {"event": "message", "chat_id": CHAT, "text": "legacy reply"},
    )
    response = build_webui_thread_response(SESSION_KEY)
    assert response is not None
    for item in response["messages"]:
        assert "client_message_id" not in item, item


def test_webui_thread_response_never_leaks_ids_on_assistant_turns(
    isolated_data_dir: Path,
) -> None:
    from nanobot.webui.transcript import build_webui_thread_response

    append_transcript_object(
        SESSION_KEY,
        {
            "event": "user",
            "chat_id": CHAT,
            "text": "track this",
            "client_message_id": CLIENT_MESSAGE_ID,
        },
    )
    append_transcript_object(
        SESSION_KEY,
        {
            "event": "message",
            "chat_id": CHAT,
            "text": "done",
            "client_message_id": OTHER_CLIENT_MESSAGE_ID,
        },
    )
    response = build_webui_thread_response(SESSION_KEY)
    assert response is not None
    assistants = [item for item in response["messages"] if item.get("role") == "assistant"]
    assert assistants, response["messages"]
    for item in assistants:
        assert "client_message_id" not in item


def test_webui_thread_response_omits_invalid_client_message_id_values(
    isolated_data_dir: Path,
) -> None:
    from nanobot.webui.transcript import build_webui_thread_response

    for index, junk in enumerate(("", "   ", " padded ", "x" * 200, "NULL")):
        append_transcript_object(
            SESSION_KEY,
            {
                "event": "user",
                "chat_id": CHAT,
                "text": f"junk {index}",
                "client_message_id": junk,
            },
        )
    append_transcript_object(
        SESSION_KEY,
        {
            "event": "user",
            "chat_id": CHAT,
            "text": "valid after junk",
            "client_message_id": CLIENT_MESSAGE_ID,
        },
    )
    response = build_webui_thread_response(SESSION_KEY)
    assert response is not None
    users = [item for item in response["messages"] if item.get("role") == "user"]
    assert len(users) == 6, response["messages"]
    for item in users[:5]:
        assert "client_message_id" not in item, item
    assert users[5].get("client_message_id") == CLIENT_MESSAGE_ID


# ---------------------------------------------------------------------------
# Route level (real HTTP dispatch through the owner-protected app): the
# placeholder row a client renders for a still-running turn must not be
# stitched by an id-based match against the completed transcript -- the
# overlay keeps its in-flight row and a misidentified match replaces it.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_webui_thread_route_serves_client_message_id_to_clients(
    webui_client: Any,
) -> None:
    recorder = WebUITranscriptRecorder()
    assert recorder.append_user_message(
        CHAT,
        "remember the milk",
        metadata={"client_message_id": CLIENT_MESSAGE_ID},
    )
    items = await _read_thread(webui_client)
    assert items, await _read_thread(webui_client)
    users = [item for item in items if item.get("role") == "user"]
    assert users, items
    assert users[0].get("client_message_id") == CLIENT_MESSAGE_ID
