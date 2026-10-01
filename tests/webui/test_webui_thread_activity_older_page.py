"""A crashed turn's recovered Activity row survives scrollback (MIT-1060).

MIT-1027 only projected recovered (never-journaled) tool activity on the
latest ``/webui-thread`` page, so once enough newer turns pushed a crashed
turn off that page its interrupted Activity row disappeared when the user
scrolled back to it. These tests page the route with a small ``limit`` so the
crashed turn lands on an older ``before=`` page and pin that its row is still
rendered under its turn — and nowhere else.
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

CHAT = "chat_activity_older_page"
SESSION_KEY = f"websocket:{CHAT}"
CRASHED_CALL_ID = "call_crashed_exec"
CRASHED_TEXT = "list the files"

T_FIRST_USER = 1_790_000_000_000
T_CRASHED_USER = T_FIRST_USER + 60_000
T_CRASHED_TOOL = T_CRASHED_USER + 1_500
TURN_GAP = 60_000
NEWER_TURNS = 4  # turns 3..6 complete after the crash


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
            "port": 18998,
            "path": "/ws",
            "websocketRequiresToken": False,
            "tokenIssueSecret": "activity-older-page-issue-secret",
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


def _append_closed_turn(key: str, idx: int, user_ms: int) -> None:
    append_transcript_object(
        key, {"event": "user", "chat_id": CHAT, "text": f"q{idx}", "created_at_ms": user_ms}
    )
    append_transcript_object(
        key,
        {
            "event": "message",
            "chat_id": CHAT,
            "text": f"a{idx}",
            "created_at_ms": user_ms + 1_000,
        },
    )
    append_transcript_object(
        key, {"event": "turn_end", "chat_id": CHAT, "created_at_ms": user_ms + 2_000}
    )


def _seed_crashed_session(sessions: SessionManager) -> None:
    """A completed turn, a turn that crashed mid-tool-call (only its user row
    journaled), then several completed turns pushing the crash off the latest
    page."""
    _append_closed_turn(SESSION_KEY, 1, T_FIRST_USER)
    append_transcript_object(
        SESSION_KEY,
        {
            "event": "user",
            "chat_id": CHAT,
            "text": CRASHED_TEXT,
            "turn_id": "turn-2",
            "created_at_ms": T_CRASHED_USER,
        },
    )
    for offset, idx in enumerate(range(3, 3 + NEWER_TURNS), start=1):
        _append_closed_turn(SESSION_KEY, idx, T_CRASHED_USER + TURN_GAP * offset)

    session = sessions.get_or_create(SESSION_KEY)
    session.messages = [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": CRASHED_TEXT},
    ]
    for idx in range(3, 3 + NEWER_TURNS):
        session.messages.append({"role": "user", "content": f"q{idx}"})
        session.messages.append({"role": "assistant", "content": f"a{idx}"})
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


async def _scroll_pages(client: Any, limit: int) -> list[list[dict[str, Any]]]:
    """Fetch pages newest-first via ``before=`` until the start is reached."""
    pages: list[list[dict[str, Any]]] = []
    before: str | None = None
    for _ in range(8):
        query = f"?limit={limit}" + (f"&before={before}" if before else "")
        body = await _get_thread(client, query)
        pages.append(body["messages"])
        page_info = body.get("page") or {}
        if not page_info.get("has_more_before"):
            return pages
        cursor = page_info.get("before_cursor")
        assert isinstance(cursor, str)
        before = cursor
    raise AssertionError("thread did not terminate within 8 pages")


def _has_crashed_user_row(page: list[dict[str, Any]]) -> bool:
    return any(row.get("role") == "user" and row.get("content") == CRASHED_TEXT for row in page)


def _recovered_indices(page: list[dict[str, Any]]) -> list[int]:
    return [
        index
        for index, row in enumerate(page)
        if any(
            tool_event.get("call_id") == CRASHED_CALL_ID
            for tool_event in (row.get("toolEvents") or [])
        )
    ]


def _user_index(page: list[dict[str, Any]], content: str) -> int:
    return next(
        index
        for index, row in enumerate(page)
        if row.get("role") == "user" and row.get("content") == content
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [6, 4], ids=["mid-page", "page-tail"])
async def test_crashed_turn_keeps_its_activity_row_on_an_older_page(
    isolated_data_dir: Path, client_and_sessions: Any, limit: int
) -> None:
    client, sessions = client_and_sessions
    _seed_crashed_session(sessions)

    pages = await _scroll_pages(client, limit)

    crashed_pages = [index for index, page in enumerate(pages) if _recovered_indices(page)]
    older_pages = [index for index, page in enumerate(pages) if _has_crashed_user_row(page)]

    # The setup is the issue's repro: the crashed turn is not on the latest page.
    assert older_pages and older_pages[0] >= 1, pages
    # Its recovered row appears exactly once, on the page hosting its turn.
    assert crashed_pages == older_pages
    page = pages[older_pages[0]]
    (recovered_index,) = _recovered_indices(page)
    assert recovered_index == _user_index(page, CRASHED_TEXT) + 1
    (row,) = [page[recovered_index]]
    assert row["role"] == "tool"
    (tool_event,) = row["toolEvents"]
    assert tool_event["status"] == "interrupted"
    assert tool_event["error"] == "Interrupted"
    assert tool_event["name"] == "exec"
    assert tool_event["arguments"] == {"command": "ls"}
    # The private recovery store never leaks into any page.
    assert KEY not in json.dumps(pages)
