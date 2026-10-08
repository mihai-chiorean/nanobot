"""TP-10 (MIT-1873): ``turn_id``/``used``/``other_steps`` on the final assistant row.

TP-09 put ``used`` and ``other_steps`` on the live ``turn_end`` frame. When a
chat is reopened the apps rebuild it from
``GET /api/sessions/{key}/webui-thread?projection=events``, so the final
assistant row of the turn must carry the same fields plus the effective
``turn_id`` (design ``docs/design/turn-provenance.md`` sections 7-8).

These tests run a real turn through the agent loop (fake provider, fake Gmail
MCP tool), persist it, materialize the WebUI transcript from the session
messages, and read the row back through the production route. Pre-change
transcript rows (fixture, written without any of these keys) must read back
byte-for-byte unchanged: the response never adds keys a row does not have.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.agent.tools.base import Tool
from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.websocket.runtime import WebSocketChannel, WebSocketConfig
from nanobot.channels.websocket.transport import TransportRequest
from nanobot.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from nanobot.session.manager import SessionManager
from nanobot.webui.gateway_services import build_gateway_services
from nanobot.webui.transcript import (
    append_transcript_object,
    write_session_messages_as_transcript,
)

GMAIL_TOOL = "mcp_ziggy_gmail_gmail_search"
GMAIL_ENTRY = {
    "family": "gmail",
    "label": "Gmail",
    "private": True,
    "calls": 1,
    "errors": 0,
}
WIRE_TURN_ID = "3f2a9c1e-58d4-4b6f-9e2c-7d0a1b3c5e6f"
FIXTURE_PATH = Path(__file__).parent / "fixtures" / "webui_thread_pre_tp10.json"


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


def _config(port: int) -> WebSocketConfig:
    return WebSocketConfig.model_validate(
        {
            "enabled": True,
            "allowFrom": ["*"],
            "host": "127.0.0.1",
            "port": port,
            "path": "/ws",
            "websocketRequiresToken": False,
            "tokenIssueSecret": "webui-thread-used-issue-secret",
            "sharedRoomsEnabled": False,
        }
    )


class _FakeGmailTool(Tool):
    """Stand-in for an MCP wrapper: keeps ``_server_name``/``_original_name``."""

    def __init__(self) -> None:
        self._server_name = "ziggy_gmail"
        self._original_name = "gmail_search"

    @property
    def name(self) -> str:
        return GMAIL_TOOL

    @property
    def description(self) -> str:
        return "test gmail search"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs):
        return "1 result"


def _provider(responses: list[LLMResponse]) -> MagicMock:
    provider = MagicMock(spec=LLMProvider)
    provider.get_default_model.return_value = "test-model"
    provider.generation = SimpleNamespace(
        max_tokens=4096,
        temperature=0.1,
        reasoning_effort=None,
    )
    provider.estimate_prompt_tokens = MagicMock(return_value=(10_000, "test"))
    remaining = list(responses)

    async def chat_stream_with_retry(**kwargs):
        return remaining.pop(0)

    provider.chat_stream_with_retry = chat_stream_with_retry
    return provider


def _make_loop(tmp_path: Path, sessions: SessionManager, bus: MessageBus, provider: MagicMock) -> AgentLoop:
    loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=tmp_path / "workspace",
        model="test-model",
        session_manager=sessions,
        memory_index_enabled=False,
    )
    return loop


async def _run_turn(loop: AgentLoop, bus: MessageBus, chat_id: str, content: str) -> None:
    await loop._dispatch_one(
        InboundMessage(
            channel="websocket",
            sender_id="u1",
            chat_id=chat_id,
            content=content,
            metadata={"webui_turn_id": WIRE_TURN_ID},
        ),
        asyncio.Queue(),
    )
    while bus.outbound_size > 0:
        await bus.consume_outbound()


@pytest.fixture
def isolated_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data_dir = tmp_path / "data"
    monkeypatch.setattr("nanobot.config.paths.get_data_dir", lambda: data_dir)
    return data_dir


@pytest.fixture
def harness(isolated_data_dir: Path) -> Any:
    """(sessions, bus, channel_factory) sharing one data dir, like production."""
    workspace = isolated_data_dir / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    sessions = SessionManager(workspace, sessions_root=isolated_data_dir / "sessions")
    bus = MessageBus()

    def build_channel(chat_id: str) -> WebSocketChannel:
        gateway = build_gateway_services(
            config=_config(18999),
            bus=bus,
            session_manager=sessions,
            static_dist_path=None,
            workspace_path=workspace,
            default_restrict_to_workspace=False,
            runtime_model_name=None,
            runtime_surface="browser",
            runtime_capabilities_overrides=None,
        )
        return WebSocketChannel(_config(18999), bus, gateway=gateway)

    return SimpleNamespace(
        data_dir=isolated_data_dir,
        workspace=workspace,
        sessions=sessions,
        bus=bus,
        build_channel=build_channel,
    )


async def _get_thread(client: WebSocketChannel, session_key: str, query: str = "") -> dict[str, Any]:
    token = client.gateway.tokens.issue_api_token(60)
    path = f"/api/sessions/{session_key.replace(':', '%3A')}/webui-thread{query}"
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


def _session_messages(sessions: SessionManager, session_key: str) -> list[dict[str, Any]]:
    data = sessions.read_session_file(session_key)
    assert data is not None
    messages = data.get("messages")
    assert isinstance(messages, list)
    return [m for m in messages if isinstance(m, dict)]


def _answer_rows(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        event
        for event in events
        if event.get("event") == "message"
        and event.get("kind") not in {"tool_hint", "progress", "reasoning"}
    ]


@pytest.mark.asyncio
async def test_gmail_turn_reopens_with_used_and_turn_id_on_final_assistant_row(harness: Any) -> None:
    chat_id = "chat_used_gmail"
    session_key = f"websocket:{chat_id}"
    provider = _provider(
        [
            LLMResponse(
                content="",
                tool_calls=[ToolCallRequest(id="tc1", name=GMAIL_TOOL, arguments={})],
            ),
            LLMResponse(content="One unread message.", tool_calls=[]),
        ]
    )
    loop = _make_loop(harness.workspace, harness.sessions, harness.bus, provider)
    loop.tools.register(_FakeGmailTool())

    await _run_turn(loop, harness.bus, chat_id, "any new e-mail from Anna?")

    # The persisted session message carries the fields (change 1).
    messages = _session_messages(harness.sessions, session_key)
    final_assistant = [m for m in messages if m.get("role") == "assistant" and m.get("content")][-1]
    assert final_assistant["turn_id"] == WIRE_TURN_ID
    assert final_assistant["used"] == [GMAIL_ENTRY]
    assert "other_steps" not in final_assistant

    # The reopen path: transcript rows materialized from the session and read
    # back through the production route (changes 2 and 3).
    write_session_messages_as_transcript(session_key, messages)
    client = harness.build_channel(chat_id)
    try:
        body = await _get_thread(client, session_key, "?projection=events")
    finally:
        await client.stop()

    assert body.get("projection") == "events"
    events: list[dict[str, Any]] = body["events"]
    answers = _answer_rows(events)
    assert len(answers) == 1, events
    (answer,) = answers
    assert answer["used"] == [GMAIL_ENTRY]
    assert answer["turn_id"] == WIRE_TURN_ID

    # Nowhere else: no other row carries the TP-10 keys.
    for event in events:
        if event is answer:
            continue
        assert "used" not in event, event
        assert "other_steps" not in event, event
        assert "turn_id" not in event, event


@pytest.mark.asyncio
async def test_tool_free_turn_has_no_used_key(harness: Any) -> None:
    chat_id = "chat_used_none"
    session_key = f"websocket:{chat_id}"
    provider = _provider([LLMResponse(content="Paris.", tool_calls=[])])
    loop = _make_loop(harness.workspace, harness.sessions, harness.bus, provider)

    await _run_turn(loop, harness.bus, chat_id, "what is the capital of France")

    messages = _session_messages(harness.sessions, session_key)
    final_assistant = [m for m in messages if m.get("role") == "assistant" and m.get("content")][-1]
    assert final_assistant["turn_id"] == WIRE_TURN_ID
    assert "used" not in final_assistant
    assert "other_steps" not in final_assistant

    write_session_messages_as_transcript(session_key, messages)
    client = harness.build_channel(chat_id)
    try:
        body = await _get_thread(client, session_key, "?projection=events")
    finally:
        await client.stop()

    events: list[dict[str, Any]] = body["events"]
    answers = _answer_rows(events)
    assert len(answers) == 1, events
    (answer,) = answers
    assert answer["turn_id"] == WIRE_TURN_ID
    assert "used" not in answer
    assert "other_steps" not in answer


@pytest.mark.asyncio
async def test_pre_change_fixture_reads_back_unchanged(harness: Any) -> None:
    fixture = json.loads(FIXTURE_PATH.read_text())
    chat_id = str(fixture["lines"][0]["chat_id"])
    session_key = f"websocket:{chat_id}"
    for line in fixture["lines"]:
        append_transcript_object(session_key, dict(line))

    client = harness.build_channel(chat_id)
    try:
        body = await _get_thread(client, session_key, "?projection=events")
    finally:
        await client.stop()

    assert body.get("projection") == "events"
    events = [dict(event) for event in body["events"]]
    for event in events:
        event.pop("projection_id", None)
    assert events == fixture["expected"]
