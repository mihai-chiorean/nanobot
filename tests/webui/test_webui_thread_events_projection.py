"""``/webui-thread?projection=events`` carries what Ziggy clients need (MIT-1486).

Upstream #5819 added an opt-in event projection next to the default
``messages`` response. Ziggy's clients depend on two fork-local additions to
the thread payload, and both must survive in the event form:

* ``client_message_id`` on user turns (MIT-1056), so a client pairs its
  optimistic send with the stored turn by identity rather than by text.
* Recovered tool activity (MIT-1027): a turn that crashed mid-tool-call never
  journaled its trace, so its activity is rebuilt from session metadata and
  shown as interrupted under the turn that ran it.

The default response (no ``projection``) must still carry ``messages``: the iOS
app reads nothing else until it moves to events.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from nanobot.bus.queue import MessageBus
from nanobot.channels.websocket.runtime import WebSocketChannel, WebSocketConfig
from nanobot.channels.websocket.transport import TransportRequest
from nanobot.session.manager import SessionManager
from nanobot.utils.activity_history import KEY
from nanobot.webui.gateway_services import build_gateway_services
from nanobot.webui.transcript import append_transcript_object

CHAT = "chat_events_projection"
SESSION_KEY = f"websocket:{CHAT}"
CLIENT_MESSAGE_ID = "3f2a9c1e-58d4-4b6f-9e2c-7d0a1b3c5e6f"
CRASHED_CALL_ID = "call_crashed_exec"

# A completed turn, then a turn whose tool call started and never came back.
T_FIRST_USER = 1_790_000_000_000
T_FIRST_END = T_FIRST_USER + 2_000
T_CRASHED_USER = T_FIRST_USER + 60_000
T_CRASHED_TOOL = T_CRASHED_USER + 1_500


class _Headers(dict):
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
            "port": 18997,
            "path": "/ws",
            "websocketRequiresToken": False,
            "tokenIssueSecret": "events-projection-issue-secret",
            "sharedRoomsEnabled": False,
        }
    )


@pytest.fixture
def isolated_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data_dir = tmp_path / "data"
    monkeypatch.setattr("nanobot.config.paths.get_data_dir", lambda: data_dir)
    return data_dir


@pytest.fixture
async def client_and_sessions(isolated_data_dir: Path) -> Any:
    workspace = isolated_data_dir / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    sessions = SessionManager(workspace, sessions_root=isolated_data_dir / "sessions")
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
    client = WebSocketChannel(_config(), bus, gateway=gateway)
    yield client, sessions
    await client.stop()


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def _seed_crashed_session(sessions: SessionManager) -> None:
    for record in (
        {"event": "user", "chat_id": CHAT, "text": "hello", "turn_id": "turn-1",
         "turn_phase": "user", "created_at_ms": T_FIRST_USER},
        {"event": "message", "chat_id": CHAT, "text": "hi there", "turn_id": "turn-1",
         "turn_phase": "answer", "created_at_ms": T_FIRST_USER + 1_000},
        {"event": "turn_end", "chat_id": CHAT, "turn_id": "turn-1",
         "turn_phase": "complete", "created_at_ms": T_FIRST_END},
        # The crashed turn journaled only its user row: the process died while
        # the exec call was running, before any trace or turn_end was written.
        {"event": "user", "chat_id": CHAT, "text": "list the files", "turn_id": "turn-2",
         "turn_phase": "user", "created_at_ms": T_CRASHED_USER,
         "client_message_id": CLIENT_MESSAGE_ID},
    ):
        append_transcript_object(SESSION_KEY, record)

    session = sessions.get_or_create(SESSION_KEY)
    session.messages = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there"},
        {"role": "user", "content": "list the files", "client_message_id": CLIENT_MESSAGE_ID},
    ]
    session.metadata[KEY] = [
        {
            "call_id": CRASHED_CALL_ID,
            "started_at": _iso(T_CRASHED_TOOL),
            "before_message_count": 3,
            "name": "exec",
            "summary": "Running ls",
            "status": "running",
            "text": json.dumps({"arguments": {"command": "ls"}, "error": None}),
        }
    ]
    sessions.save(session)


async def _get_thread(client: Any, query: str = "") -> dict[str, Any]:
    token = client.gateway.tokens.issue_api_token(60)
    path = f"/api/sessions/{SESSION_KEY.replace(':', '%3A')}/webui-thread{query}"
    response = await client._dispatch_http(
        _Connection(),
        TransportRequest(
            method="GET",
            path=path,
            headers=_Headers({"Authorization": f"Bearer {token}"}),
            body=b"",
            raw_path=path,
        ),
    )
    assert response is not None
    assert response.status_code == 200, bytes(response.body)
    return json.loads(bytes(response.body).decode())


def _recovered_tool_events(rows: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    return [
        tool_event
        for row in rows
        for tool_event in (row.get(key) or [])
        if tool_event.get("call_id") == CRASHED_CALL_ID
    ]


@pytest.mark.asyncio
async def test_events_projection_carries_client_message_id_and_recovered_activity(
    isolated_data_dir: Path, client_and_sessions: Any
) -> None:
    client, sessions = client_and_sessions
    _seed_crashed_session(sessions)

    body = await _get_thread(client, "?projection=events")

    assert body.get("projection") == "events"
    assert "messages" not in body
    events: list[dict[str, Any]] = body["events"]

    users = [event for event in events if event.get("event") == "user_message"]
    assert [user.get("client_message_id") for user in users] == [None, CLIENT_MESSAGE_ID]
    assert "client_message_id" not in users[0]

    recovered = [
        event
        for event in events
        if _recovered_tool_events([event], "tool_events")
    ]
    assert len(recovered) == 1, events
    activity = recovered[0]
    # Placed directly after the crashed turn's user event, joined to its turn.
    assert events.index(activity) == events.index(users[1]) + 1
    assert activity["event"] == "message"
    assert activity["kind"] == "progress"
    assert activity["turn_id"] == "turn-2"
    assert activity["turn_phase"] == "activity"
    assert activity["created_at_ms"] == T_CRASHED_TOOL
    (tool_event,) = activity["tool_events"]
    assert tool_event["status"] == "interrupted"
    assert tool_event["phase"] == "error"
    assert tool_event["name"] == "exec"
    assert tool_event["arguments"] == {"command": "ls"}
    # The private recovery store never leaks into the response.
    assert KEY not in json.dumps(body)


@pytest.mark.asyncio
async def test_default_thread_response_still_serves_messages(
    isolated_data_dir: Path, client_and_sessions: Any
) -> None:
    client, sessions = client_and_sessions
    _seed_crashed_session(sessions)

    body = await _get_thread(client)

    assert "events" not in body
    messages: list[dict[str, Any]] = body["messages"]
    users = [message for message in messages if message.get("role") == "user"]
    assert users[-1].get("client_message_id") == CLIENT_MESSAGE_ID
    (tool_event,) = _recovered_tool_events(messages, "toolEvents")
    assert tool_event["status"] == "interrupted"


@pytest.mark.asyncio
async def test_unknown_projection_is_rejected(
    isolated_data_dir: Path, client_and_sessions: Any
) -> None:
    client, sessions = client_and_sessions
    _seed_crashed_session(sessions)
    token = client.gateway.tokens.issue_api_token(60)
    path = f"/api/sessions/{SESSION_KEY.replace(':', '%3A')}/webui-thread?projection=nope"
    response = await client._dispatch_http(
        _Connection(),
        TransportRequest(
            method="GET",
            path=path,
            headers=_Headers({"Authorization": f"Bearer {token}"}),
            body=b"",
            raw_path=path,
        ),
    )
    assert response is not None
    assert response.status_code == 400
