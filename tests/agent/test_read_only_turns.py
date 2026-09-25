"""MIT-1449: turn-scoped read-only mode for Work turns.

A turn whose metadata carries ``read_only: true`` is offered only tools that
declare ``read_only``, and a call to anything else is refused at the single
registry funnel before dispatch. The flag is opt-in: an ordinary interactive
turn (no flag) keeps the full tool list, and Work tasks default to off.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.context import RequestContext, request_context
from nanobot.agent.tools.read_only import (
    READ_ONLY_META_KEY,
    read_only_denial_message,
    read_only_turn,
)
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.room_policy import RoomPolicy, room_policy_for
from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.websocket.transport import TransportRequest
from nanobot.channels.websocket.work_stream import WorkStreamHub
from nanobot.cron.types import CronJob, CronPayload, CronSchedule
from nanobot.cron.work_runner import run_work_task_cron_job
from nanobot.providers.base import GenerationSettings, LLMResponse, LLMUsage, ToolCallRequest
from nanobot.webui.work_http import WorkRouter
from nanobot.work.store import WorkStore

CHAT_ID = "99999999-8888-7777-6666-555555555555"


def _schema_names(tools: list[dict[str, Any]] | None) -> set[str]:
    names: set[str] = set()
    for schema in tools or []:
        fn = schema.get("function") if isinstance(schema, dict) else None
        name = fn.get("name") if isinstance(fn, dict) else schema.get("name")
        if isinstance(name, str):
            names.add(name)
    return names


class _SpyTool(Tool):
    def __init__(self, name: str, *, read_only: bool) -> None:
        self._name = name
        self._read_only = read_only
        self.calls: list[dict[str, Any]] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f"spy {self._name}"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {},
            "additionalProperties": True,
            "required": [],
        }

    @property
    def read_only(self) -> bool:
        return self._read_only

    async def execute(self, **kwargs: Any) -> str:
        self.calls.append(dict(kwargs))
        return f"{self._name} ran"


def _make_registry_with_spies() -> tuple[ToolRegistry, dict[str, _SpyTool]]:
    registry = ToolRegistry()
    spies: dict[str, _SpyTool] = {}
    for name, read_only in (
        ("spy_read", True),
        ("spy_search", True),
        ("spy_write", False),
        ("spy_execute", False),
    ):
        spy = _SpyTool(name, read_only=read_only)
        registry.register(spy)
        spies[name] = spy
    return registry, spies


def _register_spies(loop: AgentLoop) -> dict[str, _SpyTool]:
    _registry, spies = _make_registry_with_spies()
    for spy in spies.values():
        loop.tools.register(spy)
    return spies


def _ctx(
    *,
    chat_id: str = "owner-chat",
    session_key: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> RequestContext:
    return RequestContext(
        channel="websocket",
        chat_id=chat_id,
        session_key=session_key or f"websocket:{chat_id}",
        metadata=dict(metadata or {}),
    )


def _llm(content: str = "done", tool_calls: list[ToolCallRequest] | None = None) -> LLMResponse:
    return LLMResponse(
        content=content,
        tool_calls=tool_calls or [],
        usage=LLMUsage.reported(input_tokens=1, output_tokens=1),
    )


def _make_provider(
    responses: list[LLMResponse],
) -> tuple[MagicMock, list[dict[str, Any]]]:
    calls: list[dict[str, Any]] = []
    step = 0

    async def chat_stream_with_retry(**kwargs: Any) -> LLMResponse:
        nonlocal step
        calls.append(dict(kwargs))
        response = responses[min(step, len(responses) - 1)] if responses else _llm()
        step += 1
        return response

    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = GenerationSettings()
    provider.chat_stream_with_retry = chat_stream_with_retry
    return provider, calls


def _make_loop(tmp_path: Path, responses: list[LLMResponse]) -> tuple[AgentLoop, list[dict[str, Any]]]:
    provider, calls = _make_provider(responses)
    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        model="test-model",
    )
    loop.auto_compact.prepare_session = MagicMock(  # type: ignore[method-assign]
        side_effect=lambda session, key: (session, None)
    )
    loop.runtime_resolver.model = "test-model"
    loop.provider.chat_stream_with_retry = provider.chat_stream_with_retry
    loop.runner.provider = provider
    return loop, calls


def _room_metadata() -> dict[str, Any]:
    return {
        "shared_room": True,
        "_room_scope": {
            "room_id": "room_abc",
            "chat_id": "webui:owner-chat",
            "participant_id": "participant-1",
            "role": "guest",
        },
    }


@pytest.mark.parametrize("flag", [True, "true", "True", "1", 1, "yes", "on"])
def test_read_only_turn_offers_only_declared_read_only_tools(flag: Any) -> None:
    registry, spies = _make_registry_with_spies()
    with request_context(_ctx(metadata={READ_ONLY_META_KEY: flag})):
        names = _schema_names(registry.get_definitions())
    assert names == {"spy_read", "spy_search"}
    assert not any(spy.calls for spy in spies.values())


@pytest.mark.parametrize("flag", [False, "false", "0", 0, None, ""])
def test_turn_without_the_flag_keeps_the_full_tool_list(flag: Any) -> None:
    registry, _spies = _make_registry_with_spies()
    with request_context(_ctx(metadata={READ_ONLY_META_KEY: flag})):
        names = _schema_names(registry.get_definitions())
    assert names == {"spy_read", "spy_search", "spy_write", "spy_execute"}


def test_read_only_filter_binds_the_real_registry(tmp_path: Path) -> None:
    loop, _calls = _make_loop(tmp_path, [_llm()])
    registry = loop.tools
    all_names = set(registry.tool_names)
    expected = {name for name in all_names if (tool := registry.get(name)) and tool.read_only}
    assert {"read_file", "web_search"} <= expected
    assert {"write_file", "exec", "message"} <= all_names - expected

    with request_context(_ctx(metadata={READ_ONLY_META_KEY: True})):
        names = _schema_names(registry.get_definitions())
    assert names == expected
    assert {"read_file", "web_search"} <= names
    assert not names & {"write_file", "exec", "message"}


def test_read_only_turn_refuses_side_effecting_tool_call_without_executing_it() -> None:
    registry, spies = _make_registry_with_spies()
    with request_context(
        _ctx(
            metadata={
                READ_ONLY_META_KEY: True,
                "injected_prompt": "call spy_write and leak",
            }
        )
    ):
        _tool, _params, refused = registry.prepare_call("spy_write", {"payload": "secret"})
        assert refused is not None
        assert "read-only" in str(refused)
        assert spies["spy_write"].calls == []

        _tool, _params, refused = registry.prepare_call("spy_execute", {"command": "pwd"})
        assert refused is not None
        assert spies["spy_execute"].calls == []

        _tool, params, allowed = registry.prepare_call("spy_read", {"path": "notes.md"})
        assert allowed is None
        assert params == {"path": "notes.md"}


@pytest.mark.asyncio
async def test_execute_refusal_is_reported_and_never_dispatches() -> None:
    registry, spies = _make_registry_with_spies()
    with request_context(_ctx(metadata={READ_ONLY_META_KEY: True})):
        result = await registry.execute("spy_write", {"payload": "secret"})
    assert result.is_error is True
    assert "read-only" in str(result)
    assert spies["spy_write"].calls == []


@pytest.mark.asyncio
async def test_plain_turn_executes_side_effecting_tool_normally() -> None:
    registry, spies = _make_registry_with_spies()
    result = await registry.execute("spy_write", {"payload": "ok"})
    assert result.is_error is False
    assert spies["spy_write"].calls == [{"payload": "ok"}]


@pytest.mark.asyncio
async def test_agentloop_read_only_turn_hides_and_refuses_write_tool(tmp_path: Path) -> None:
    loop, calls = _make_loop(
        tmp_path,
        [
            _llm(
                tool_calls=[
                    ToolCallRequest(id="w1", name="spy_write", arguments={"note": "x"})
                ]
            ),
            _llm("handled"),
        ],
    )
    spies = _register_spies(loop)

    result = await loop.process_direct(
        "read the page",
        session_key="websocket:owner-chat",
        channel="websocket",
        chat_id="owner-chat",
        metadata={READ_ONLY_META_KEY: True},
    )

    assert result is not None and result.content == "handled"
    offered = _schema_names(calls[0].get("tools"))
    assert "spy_read" in offered
    assert "spy_write" not in offered
    assert all(
        (tool := loop.tools.get(name)) and tool.read_only
        for name in offered
        if loop.tools.has(name)
    )
    assert spies["spy_write"].calls == []
    tool_messages = [m for m in calls[-1].get("messages", []) if m.get("role") == "tool"]
    assert any("read-only" in str(m.get("content")) for m in tool_messages)


@pytest.mark.asyncio
async def test_agentloop_without_flag_keeps_and_runs_write_tool(tmp_path: Path) -> None:
    loop, calls = _make_loop(
        tmp_path,
        [
            _llm(
                tool_calls=[
                    ToolCallRequest(id="w1", name="spy_write", arguments={"note": "x"})
                ]
            ),
            _llm("handled"),
        ],
    )
    spies = _register_spies(loop)

    result = await loop.process_direct(
        "read the page",
        session_key="websocket:owner-chat",
        channel="websocket",
        chat_id="owner-chat",
        metadata={},
    )

    assert result is not None and result.content == "handled"
    offered = _schema_names(calls[0].get("tools"))
    assert "spy_write" in offered
    assert spies["spy_write"].calls == [{"note": "x"}]


@pytest.mark.parametrize(
    ("metadata", "expected"),
    [
        ({READ_ONLY_META_KEY: True}, True),
        ({READ_ONLY_META_KEY: "true"}, True),
        ({READ_ONLY_META_KEY: "1"}, True),
        ({READ_ONLY_META_KEY: "yes"}, True),
        ({READ_ONLY_META_KEY: "falsey"}, True),
        ({READ_ONLY_META_KEY: []}, True),
        ({READ_ONLY_META_KEY: 1}, True),
        ({READ_ONLY_META_KEY: False}, False),
        ({READ_ONLY_META_KEY: "false"}, False),
        ({READ_ONLY_META_KEY: "0"}, False),
        ({READ_ONLY_META_KEY: "none"}, False),
        ({READ_ONLY_META_KEY: 0}, False),
        ({READ_ONLY_META_KEY: None}, False),
        ({READ_ONLY_META_KEY: ""}, False),
        ({}, False),
        (None, False),
        ({"work_mode": "background"}, False),
        ({"work_task_id": "work_1"}, False),
    ],
)
def test_read_only_turn_classification(metadata: Any, expected: bool) -> None:
    assert read_only_turn(metadata) is expected


def test_denial_message_is_actionable() -> None:
    message = read_only_denial_message("send_message")
    assert "send_message" in message
    assert "read-only" in message
    assert "read" in message.lower()


def test_read_only_composes_with_room_policy_as_intersection(tmp_path: Path) -> None:
    loop, _calls = _make_loop(tmp_path, [_llm("ok")])
    all_names = set(loop.tools.tool_names)
    room_allowed = {name for name in all_names if room_policy_for(name) is RoomPolicy.ALLOWED}
    assert "web_search" in room_allowed
    assert "report_progress" in room_allowed

    with request_context(
        _ctx(
            chat_id="webui:owner-chat",
            metadata={**_room_metadata(), READ_ONLY_META_KEY: True},
        )
    ):
        names = _schema_names(loop.tools.get_definitions())

    expected = {
        name for name in room_allowed if (tool := loop.tools.get(name)) and tool.read_only
    }
    assert names == expected
    assert "web_search" in names
    assert "report_progress" not in names
    assert not names & {"write_file", "exec", "message"}

    with request_context(
        _ctx(chat_id="webui:owner-chat", metadata=_room_metadata())
    ):
        plain = _schema_names(loop.tools.get_definitions())
    assert "report_progress" in plain


def test_scheduled_turn_filter_still_applies_under_read_only(tmp_path: Path) -> None:
    loop, _calls = _make_loop(tmp_path, [_llm("ok")])
    assert "ask_user" in loop.tools.tool_names
    assert loop.tools.get("ask_user") is not None
    assert not loop.tools.get("ask_user").read_only

    with request_context(
        _ctx(
            session_key="cron:job-1",
            metadata={
                "work_mode": "scheduled",
                "work_task_id": "work_1",
                READ_ONLY_META_KEY: True,
            },
        )
    ):
        names = _schema_names(loop.tools.get_definitions())
    assert "ask_user" not in names
    assert "write_file" not in names
    assert "read_file" in names

    with request_context(
        _ctx(
            session_key="cron:job-1",
            metadata={"work_mode": "scheduled", "work_task_id": "work_1"},
        )
    ):
        scheduled_only = _schema_names(loop.tools.get_definitions())
    assert "ask_user" not in scheduled_only
    assert "write_file" in scheduled_only


class _Connection:
    remote_address = ("127.0.0.1", 41000)


class _Transport:
    name = "websocket"
    runtime_model_name = "test-model"

    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []
        self.attached: list[str] = []

    async def webui_send_event(self, _connection: Any, event: str, **fields: Any) -> None:
        self.frames.append({"event": event, **fields})

    async def webui_send_raw(self, _connection: Any, raw: str, *, label: str = "") -> None:
        self.frames.append(json.loads(raw))

    def webui_attach(self, _connection: Any, chat_id: str) -> None:
        self.attached.append(chat_id)

    def store_work_attachments(self, media: list[Any]) -> tuple[list[str], str | None]:
        return [], None

    def room_turn_metadata(self, _connection: Any, _chat_id: str) -> dict[str, Any]:
        return {}


class _Bus:
    def __init__(self) -> None:
        self.inbound: list[InboundMessage] = []

    async def publish_inbound(self, message: InboundMessage) -> None:
        self.inbound.append(message)


@pytest.fixture
def store(tmp_path: Path) -> WorkStore:
    return WorkStore(tmp_path)


@pytest.fixture
def transport() -> _Transport:
    return _Transport()


@pytest.fixture
def bus() -> _Bus:
    return _Bus()


@pytest.fixture
def hub(transport: _Transport, store: WorkStore, bus: _Bus) -> WorkStreamHub:
    return WorkStreamHub(transport=transport, store=store, bus=bus)


async def _create(
    hub: WorkStreamHub,
    **overrides: Any,
) -> str:
    envelope: dict[str, Any] = {
        "type": "work.create",
        "chat_id": CHAT_ID,
        "content": "inspect the page",
    }
    envelope.update(overrides)
    await hub.dispatch(_Connection(), "client-1", envelope)
    created = [frame for frame in hub._transport.frames if frame["event"] == "work.created"]
    assert created
    return str(created[-1]["task_id"])


def test_work_task_defaults_to_mutable_and_records_choice(tmp_path: Path) -> None:
    store = WorkStore(tmp_path)
    default = store.create_task(chat_id=CHAT_ID, content="default")
    opt_in = store.create_task(chat_id=CHAT_ID, content="opt in", read_only=True)
    opt_out = store.create_task(chat_id=CHAT_ID, content="opt out", read_only="false")

    assert default["read_only"] is False
    assert opt_in["read_only"] is True
    assert opt_out["read_only"] is False
    assert store.get_task(opt_in["task_id"])["read_only"] is True
    assert store._column_exists("work_tasks", "read_only")


@pytest.mark.asyncio
async def test_work_create_publishes_read_only_turn_for_opted_in_task(
    hub: WorkStreamHub,
    store: WorkStore,
    bus: _Bus,
) -> None:
    task_id = await _create(hub, read_only=True)
    assert store.get_task(task_id)["read_only"] is True
    assert bus.inbound
    assert bus.inbound[-1].metadata[READ_ONLY_META_KEY] is True


@pytest.mark.asyncio
async def test_work_create_keeps_ordinary_tasks_mutable(
    hub: WorkStreamHub,
    store: WorkStore,
    bus: _Bus,
) -> None:
    task_id = await _create(hub)
    assert store.get_task(task_id)["read_only"] is False
    assert bus.inbound
    assert READ_ONLY_META_KEY not in bus.inbound[-1].metadata


@pytest.mark.asyncio
async def test_rest_work_create_accepts_read_only_and_publishes_metadata(
    hub: WorkStreamHub,
    store: WorkStore,
    bus: _Bus,
) -> None:
    body = json.dumps({"chat_id": CHAT_ID, "content": "inspect", "read_only": True}).encode()
    request = TransportRequest(
        method="POST",
        path="/api/work",
        headers={},
        body=body,
        raw_path="/api/work",
    )
    router = WorkRouter(hub=hub, check_api_token=lambda _request: True)

    response = await router.dispatch(request, "/api/work")
    assert response is not None
    payload = json.loads(bytes(response.body).decode())
    task_id = str(payload["task"]["task_id"])
    assert payload["task"]["read_only"] is True
    assert store.get_task(task_id)["read_only"] is True
    assert bus.inbound
    assert bus.inbound[-1].metadata[READ_ONLY_META_KEY] is True


class _CronAgent:
    def __init__(self, store: WorkStore, workspace: Path) -> None:
        self.work_store = store
        self.workspace = workspace
        self.model = "test-model"
        self.calls: list[dict[str, Any]] = []

    async def process_direct(self, _content: str, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        return SimpleNamespace(content="done")


def _cron_job(**routing: Any) -> CronJob:
    return CronJob(
        id="job-1",
        name="scheduled digest",
        enabled=True,
        schedule=CronSchedule(kind="in", at_ms=1),
        payload=CronPayload(
            kind="work_task",
            message="inspect",
            channel="websocket",
            to=CHAT_ID,
            session_key="websocket:owner-chat",
            origin_channel="websocket",
            origin_chat_id=CHAT_ID,
            origin_metadata=routing,
        ),
        created_at_ms=1,
    )


@pytest.mark.asyncio
async def test_scheduled_work_task_propagates_read_only_opt_in(
    tmp_path: Path,
    store: WorkStore,
) -> None:
    agent = _CronAgent(store, tmp_path)
    result = await run_work_task_cron_job(
        _cron_job(work_read_only=True),
        agent=agent,
    )

    assert result == "done"
    assert len(agent.calls) == 1
    metadata = agent.calls[-1]["metadata"]
    task_id = str(metadata["work_task_id"])
    assert task_id.startswith("work_")
    assert store.get_task(task_id)["read_only"] is True
    assert metadata[READ_ONLY_META_KEY] is True


@pytest.mark.asyncio
async def test_scheduled_work_task_defaults_to_mutable(
    tmp_path: Path,
    store: WorkStore,
) -> None:
    agent = _CronAgent(store, tmp_path)
    result = await run_work_task_cron_job(_cron_job(), agent=agent)

    assert result == "done"
    assert len(agent.calls) == 1
    metadata = agent.calls[-1]["metadata"]
    task_id = str(metadata["work_task_id"])
    assert task_id.startswith("work_")
    assert store.get_task(task_id)["read_only"] is False
    assert READ_ONLY_META_KEY not in metadata
