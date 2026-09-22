"""Gating and behaviour for the conversation-bound briefing tool (MIT-1028)."""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from nanobot.agent.tools.briefing import BriefingTool, BriefingToolsConfig
from nanobot.agent.tools.context import RequestContext, ToolContext, request_context
from nanobot.agent.tools.loader import ToolLoader
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.config.schema import Config, ToolsConfig
from nanobot.session.keys import UNIFIED_SESSION_KEY
from nanobot.session.manager import SessionManager
from nanobot.webui.session_identity import webui_session_key


class WorkAPI:
    def __init__(self):
        self.workflows = {}
        self.calls = []
        self.run_keys = set()
        self.fail_run_response = False
        self.conflict = False

    def handle(self, request):
        self.calls.append(request)
        path = request.url.path.removeprefix("/runtime/briefings")
        if path == "/capabilities":
            return httpx.Response(200, json={"editorial": True, "version": 1})
        if path == "/context":
            assert request.url.params["session_key"] == "chat-one"
            return httpx.Response(
                200, json={"workflows": list(self.workflows.values()), "tasks": []}
            )
        workflow_id = path.split("/")[2]
        payload = json.loads(request.content or "{}")
        if request.method == "PUT":
            if self.conflict:
                return httpx.Response(409, json={})
            previous = self.workflows.get(workflow_id, {})
            assert payload["expected_version"] == previous.get("version", 0)
            revisions = copy.deepcopy(previous.get("revisions", []))
            if (
                not revisions
                or revisions[-1]["instructions"] != payload["instructions"]
                or revisions[-1]["sources"] != payload["sources"]
            ):
                revisions.append(
                    {
                        "id": f"{workflow_id}:r{len(revisions) + 1}",
                        "instructions": payload["instructions"],
                        "sources": payload["sources"],
                    }
                )
            self.workflows[workflow_id] = {
                "id": workflow_id,
                "session_key": payload["session_key"],
                "version": previous.get("version", 0) + 1,
                "title": payload["title"],
                "revisions": revisions,
                "schedule": {**payload["schedule"], "next_run": "2026-09-11T15:00:00Z"},
                "editions": previous.get("editions", []),
            }
        elif path.endswith("/run"):
            key = request.headers["Idempotency-Key"]
            if key not in self.run_keys:
                self.run_keys.add(key)
                self.workflows[workflow_id]["editions"].append(
                    {
                        "id": "edition-" + str(len(self.run_keys)),
                        "source_edition_id": payload.get("edition_id"),
                    }
                )
            if self.fail_run_response:
                self.fail_run_response = False
                raise httpx.ReadTimeout("lost response", request=request)
        return httpx.Response(200, json=self.workflows[workflow_id])


def private_turn(
    chat_id: str = "chat-one",
    *,
    metadata: dict | None = None,
    message_id: str | None = None,
    session_key: str | None = "websocket:chat-one",
):
    """Bind the per-turn request snapshot the tool reads (0.3.0 contract)."""
    return request_context(
        RequestContext(
            channel="websocket",
            chat_id=chat_id,
            session_key=session_key,
            message_id=message_id,
            metadata=dict(metadata or {}),
        )
    )


@pytest.fixture
def setup(tmp_path):
    credential = tmp_path / "runtime-secret"
    credential.write_text("b" * 32)
    sessions = SessionManager(tmp_path / "workspace")
    api = WorkAPI()
    config = BriefingToolsConfig(
        enable=True,
        control_url="http://127.0.0.1:19787",
        user_id="owner",
        workspace_id="private-workspace",
        credential_file=str(credential),
    )
    tool = BriefingTool(config, sessions, transport=httpx.MockTransport(api.handle))
    return tool, api, sessions


CREATE = dict(
    title="Daily briefing",
    instructions="Summarize Swift news",
    sources=["https://swift.org/blog"],
    frequency="weekdays",
    hour=8,
    minute=0,
    time_zone="America/Los_Angeles",
)


def tool_context(config: ToolsConfig, sessions: SessionManager) -> ToolContext:
    return ToolContext(config=config, workspace=str(sessions.workspace), sessions=sessions)


def test_briefing_is_modelled_and_off_by_default():
    assert Config().tools.briefing.enable is False
    assert ToolsConfig().briefing.enable is False


def test_production_camel_case_shape_is_modelled():
    # The live tenant JSON is exactly this; an unmodelled key would be
    # silently ignored (Base does not forbid extras) and the tool would
    # never register without any error anywhere.
    config = Config.model_validate(
        {
            "tools": {
                "briefing": {
                    "enable": True,
                    "controlUrl": "http://127.0.0.1:8788",
                    "userId": "usr_abc",
                    "workspaceId": "wsp_abc",
                    "credentialFile": "/etc/ziggy/briefing-token",
                }
            }
        }
    )
    briefing = config.tools.briefing
    assert briefing.enable is True
    assert briefing.control_url == "http://127.0.0.1:8788"
    assert briefing.user_id == "usr_abc"
    assert briefing.workspace_id == "wsp_abc"
    assert briefing.credential_file == "/etc/ziggy/briefing-token"


def test_absent_or_unmodelled_config_does_not_half_enable(tmp_path):
    # No briefing attribute at all (old/foreign config objects) must read as
    # disabled, never raise and never register.
    sessions = SessionManager(tmp_path / "workspace")
    assert BriefingTool.enabled(SimpleNamespace(config=object())) is False
    assert BriefingTool.enabled(
        SimpleNamespace(config=SimpleNamespace(briefing=SimpleNamespace(enable=True)))
    ) is True
    registry = ToolRegistry()
    ToolLoader().load(tool_context(ToolsConfig(), sessions), registry)
    assert "briefing" not in registry.tool_names


def test_loader_registers_only_when_enabled(tmp_path):
    credential = tmp_path / "runtime-secret"
    credential.write_text("b" * 32)
    sessions = SessionManager(tmp_path / "workspace")
    config = ToolsConfig(
        briefing=BriefingToolsConfig(
            enable=True,
            control_url="http://127.0.0.1:8788",
            user_id="owner",
            workspace_id="workspace",
            credential_file=str(credential),
        )
    )
    registry = ToolRegistry()
    ToolLoader().load(tool_context(config, sessions), registry)
    assert "briefing" in registry.tool_names
    assert registry.get("briefing")._transport is None


def test_misconfigured_enabled_tool_never_registers(tmp_path):
    credential = tmp_path / "runtime-secret"
    credential.write_text("b" * 32)
    inside = tmp_path / "workspace" / "leaked-token"
    inside.parent.mkdir(parents=True, exist_ok=True)
    inside.write_text("a" * 32)
    sessions = SessionManager(tmp_path / "workspace")
    for bad in (
        # Remote HTTP origin.
        dict(control_url="http://briefing.example.com", credential_file=str(credential)),
        # Credential inside the model workspace.
        dict(control_url="http://127.0.0.1:8788", credential_file=str(inside)),
        # Missing runtime allocation.
        dict(
            control_url="https://briefing.example.com",
            credential_file=str(credential),
            user_id="",
        ),
    ):
        config = ToolsConfig(
            briefing=BriefingToolsConfig(
                **{"enable": True, "user_id": "owner", "workspace_id": "w", **bad}
            )
        )
        registry = ToolRegistry()
        ToolLoader().load(tool_context(config, sessions), registry)
        assert "briefing" not in registry.tool_names


def test_misconfigured_enabled_briefing_never_ships_the_intake_policy(tmp_path):
    # The loader deliberately swallows create() failures, so an enabled
    # section with a bad value registers nothing. The intake policy *names*
    # that tool, so it must not reach the system prompt either -- the flag
    # follows what registered, never the config alone.
    from nanobot.agent.loop import AgentLoop
    from nanobot.bus.queue import MessageBus
    from nanobot.config.loader import set_config_path

    set_config_path(tmp_path / "config.json")
    credential = tmp_path / "runtime-secret"
    credential.write_text("b" * 32)
    sessions = SessionManager(tmp_path / "workspace")
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = SimpleNamespace(max_tokens=4096, temperature=0.1, reasoning_effort=None)
    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=sessions.workspace,
        session_manager=sessions,
        model="test-model",
        tools_config=ToolsConfig(
            briefing=BriefingToolsConfig(
                enable=True,
                control_url="http://briefing.example.com",  # remote HTTP origin
                user_id="owner",
                workspace_id="w",
                credential_file=str(credential),
            )
        ),
    )
    assert loop.tools.get("briefing") is None
    assert loop.context.workflow_scheduling is False
    prompt = loop.context.build_system_prompt(channel="websocket")
    assert "Workflow Scheduling Policy" not in prompt


@pytest.mark.asyncio
async def test_create_recovers_uncertain_first_run_without_duplicate(setup):
    tool, api, _ = setup
    api.fail_run_response = True
    with private_turn(metadata={"client_message_id": "message-one"}):
        assert "not confirmed" in await tool.execute("create", **CREATE)
        result = json.loads(await tool.execute("create", **CREATE))
    assert result["status"] == "first_edition_requested"
    assert len(api.workflows) == len(api.run_keys) == 1
    assert len(result["workflow"]["editions"]) == 1
    assert len([r for r in api.calls if r.method == "PUT"]) == 1
    for request in api.calls:
        assert request.headers["X-Ziggy-Runtime-User"] == "owner"


@pytest.mark.asyncio
async def test_edit_preserves_schedule_and_regeneration_targets_original_edition(setup):
    tool, api, _ = setup
    with private_turn(metadata={"client_message_id": "message-one"}):
        first = json.loads(await tool.execute("create", **CREATE))["workflow"]
    with private_turn(message_id="message-two"):
        revised = json.loads(
            await tool.execute("update", workflow_id=first["id"], instructions="Only compiler news")
        )["workflow"]
    assert len(revised["revisions"]) == 2
    assert revised["schedule"] == first["schedule"]
    assert len(revised["editions"]) == 1
    with private_turn(message_id="message-two"):
        await tool.execute("regenerate", workflow_id=first["id"], edition_id="edition-1")
    run = api.calls[-1]
    assert json.loads(run.content) == {"edition_id": "edition-1"}
    with private_turn(message_id="message-two"):
        await tool.execute("pause", workflow_id=first["id"])
        assert not api.workflows[first["id"]]["schedule"]["enabled"]
        await tool.execute("resume", workflow_id=first["id"])
        assert api.workflows[first["id"]]["schedule"]["enabled"]


@pytest.mark.asyncio
async def test_revision_conflict_does_not_overwrite_newer_edit(setup):
    tool, api, _ = setup
    with private_turn(metadata={"client_message_id": "message-one"}):
        first = json.loads(await tool.execute("create", **CREATE))["workflow"]
        api.conflict = True
        result = await tool.execute("update", workflow_id=first["id"], instructions="Changed")
    assert "no newer edits were overwritten" in result
    assert api.workflows[first["id"]]["revisions"][-1]["instructions"] == CREATE["instructions"]


@pytest.mark.asyncio
async def test_shared_and_background_context_cannot_change_owner_work(setup):
    tool, api, sessions = setup
    sessions.get_or_create("websocket:chat-one").metadata["shared_room"] = True
    with private_turn(message_id="message-one"):
        assert "private owner conversation" in await tool.execute("create", **CREATE)
    sessions.get_or_create("websocket:chat-one").metadata.clear()
    with private_turn(metadata={"work_mode": "scheduled"}, message_id="run-one"):
        assert "private owner conversation" in await tool.execute("create", **CREATE)
    with private_turn(metadata={"shared_room": True}, message_id="room-one"):
        assert "private owner conversation" in await tool.execute("create", **CREATE)
    assert not api.calls


@pytest.mark.asyncio
async def test_unified_turn_probes_the_real_session_and_creates_nothing(setup):
    # In unified-session mode the turn runs in UNIFIED_SESSION_KEY, so the
    # shared_room probe must read *that* session -- and must never mint the
    # fallback per-chat session it does not run in.
    tool, api, sessions = setup
    with private_turn(session_key=UNIFIED_SESSION_KEY, message_id="message-one"):
        result = json.loads(await tool.execute("create", **CREATE))
    assert result["status"] == "first_edition_requested"
    assert sessions.get_cached(webui_session_key("chat-one")) is None
    assert sessions.read_session_metadata(webui_session_key("chat-one")) is None
    # A shared flag on the session this turn actually runs in denies it,
    # even though the never-created fallback key would have read as private.
    sessions.get_or_create(UNIFIED_SESSION_KEY).metadata["shared_room"] = True
    api.calls.clear()
    with private_turn(session_key=UNIFIED_SESSION_KEY, message_id="message-two"):
        assert "private owner conversation" in await tool.execute("inspect")
    assert not api.calls


@pytest.mark.asyncio
async def test_turn_without_bound_request_context_is_inert(setup):
    tool, api, _ = setup
    assert "private owner conversation" in await tool.execute("inspect")
    assert not api.calls


@pytest.mark.asyncio
async def test_unknown_workflow_and_missing_schedule_never_mutate(setup):
    tool, api, _ = setup
    with private_turn(message_id="message-one"):
        assert "missing" in await tool.execute("create", title="Briefing")
        assert "this conversation" in await tool.execute(
            "update", workflow_id="wf_" + "a" * 32, instructions="Changed"
        )
    assert all(request.method == "GET" for request in api.calls)


@pytest.mark.asyncio
async def test_same_turn_cannot_silently_replace_saved_creation(setup):
    tool, api, _ = setup
    with private_turn(metadata={"client_message_id": "message-one"}):
        await tool.execute("create", **CREATE)
        assert "already created a different briefing" in await tool.execute(
            "create", **{**CREATE, "instructions": "Different"}
        )
    assert len(api.workflows) == 1


def _turn_driving_provider(tool_calls):
    from nanobot.providers.base import LLMResponse, ToolCallRequest

    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = SimpleNamespace(max_tokens=4096, temperature=0.1, reasoning_effort=None)
    responses = [
        LLMResponse(
            content="",
            finish_reason="tool_calls",
            tool_calls=[
                ToolCallRequest(id=call_id, name=name, arguments=arguments)
                for call_id, name, arguments in tool_calls
            ],
        ),
        LLMResponse(content="Your first edition is queued.", finish_reason="stop"),
    ]
    provider.chat_stream_with_retry = AsyncMock(side_effect=responses)
    return provider


@pytest.mark.asyncio
async def test_enabled_config_registers_the_tool_and_reaches_the_control_url(
    tmp_path, setup, monkeypatch
):
    from nanobot.agent.loop import AgentLoop
    from nanobot.bus.events import InboundMessage
    from nanobot.bus.queue import MessageBus
    from nanobot.config.loader import set_config_path

    set_config_path(tmp_path / "config.json")
    tool, api, sessions = setup
    provider = _turn_driving_provider(
        [("create-briefing", "briefing", {"action": "create", **CREATE})]
    )
    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=sessions.workspace,
        session_manager=sessions,
        model="test-model",
        tools_config=ToolsConfig(briefing=tool._config),
    )
    registered = loop.tools.get("briefing")
    assert registered is not None
    # Seam the HTTP boundary instead of reaching into the loader-created
    # tool's private transport attribute: default-set the transport on every
    # client the tool constructs (repo convention -- see the slack/whatsapp
    # transport monkeypatches).
    real_client = httpx.AsyncClient
    mock_transport = httpx.MockTransport(api.handle)
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *args, transport=None, **kwargs: real_client(
            *args, transport=transport or mock_transport, **kwargs
        ),
    )
    system_prompt = loop.context.build_system_prompt(channel="websocket")
    assert "Workflow Scheduling Policy" in system_prompt

    result = await loop._process_message(
        InboundMessage(
            channel="websocket",
            sender_id="owner",
            chat_id="chat-one",
            content="Make a Swift briefing every weekday at 8am in Los Angeles.",
            metadata={"client_message_id": "durable-message-one"},
        )
    )
    assert result is not None
    assert result.content == "Your first edition is queued."
    assert len(api.workflows) == 1
    assert len(api.run_keys) == 1
    assert all(request.headers["X-Ziggy-Runtime-User"] == "owner" for request in api.calls)


@pytest.mark.asyncio
async def test_disabled_config_registers_nothing_and_leaves_the_turn_unchanged(
    tmp_path,
):
    from nanobot.agent.loop import AgentLoop
    from nanobot.bus.events import InboundMessage
    from nanobot.bus.queue import MessageBus
    from nanobot.config.loader import set_config_path

    set_config_path(tmp_path / "config.json")
    sessions = SessionManager(tmp_path / "workspace")
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = SimpleNamespace(max_tokens=4096, temperature=0.1, reasoning_effort=None)
    from nanobot.providers.base import LLMResponse

    provider.chat_stream_with_retry = AsyncMock(
        side_effect=[LLMResponse(content="plain answer", finish_reason="stop")]
    )
    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=sessions.workspace,
        session_manager=sessions,
        model="test-model",
        tools_config=ToolsConfig(),
    )
    assert loop.tools.get("briefing") is None
    assert "Workflow Scheduling Policy" not in loop.context.build_system_prompt(
        channel="websocket"
    )
    result = await loop._process_message(
        InboundMessage(
            channel="websocket",
            sender_id="owner",
            chat_id="chat-one",
            content="hello",
        )
    )
    assert result is not None
    assert result.content == "plain answer"
    assert provider.chat_stream_with_retry.await_count == 1
