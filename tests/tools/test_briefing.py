import copy
import json

import httpx
import pytest

from nanobot.agent.tools.briefing import BriefingTool
from nanobot.config.schema import BriefingToolsConfig
from nanobot.session.manager import SessionManager


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
    tool.set_context("websocket", "chat-one", metadata={"client_message_id": "message-one"})
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


@pytest.mark.asyncio
async def test_create_recovers_uncertain_first_run_without_duplicate(setup):
    tool, api, _ = setup
    api.fail_run_response = True
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
    first = json.loads(await tool.execute("create", **CREATE))["workflow"]
    tool.set_context("websocket", "chat-one", message_id="message-two")
    revised = json.loads(
        await tool.execute("update", workflow_id=first["id"], instructions="Only compiler news")
    )["workflow"]
    assert len(revised["revisions"]) == 2
    assert revised["schedule"] == first["schedule"]
    assert len(revised["editions"]) == 1
    await tool.execute("regenerate", workflow_id=first["id"], edition_id="edition-1")
    run = api.calls[-1]
    assert json.loads(run.content) == {"edition_id": "edition-1"}
    await tool.execute("pause", workflow_id=first["id"])
    assert not api.workflows[first["id"]]["schedule"]["enabled"]
    await tool.execute("resume", workflow_id=first["id"])
    assert api.workflows[first["id"]]["schedule"]["enabled"]


@pytest.mark.asyncio
async def test_revision_conflict_does_not_overwrite_newer_edit(setup):
    tool, api, _ = setup
    first = json.loads(await tool.execute("create", **CREATE))["workflow"]
    api.conflict = True
    result = await tool.execute("update", workflow_id=first["id"], instructions="Changed")
    assert "no newer edits were overwritten" in result
    assert api.workflows[first["id"]]["revisions"][-1]["instructions"] == CREATE["instructions"]


@pytest.mark.asyncio
async def test_shared_and_background_context_cannot_change_owner_work(setup):
    tool, api, sessions = setup
    sessions.get_or_create("websocket:chat-one").metadata["shared_room"] = True
    tool.set_context("websocket", "chat-one", message_id="message-one")
    assert "private owner conversation" in await tool.execute("create", **CREATE)
    sessions.get_or_create("websocket:chat-one").metadata.clear()
    tool.set_context(
        "websocket", "chat-one", metadata={"work_mode": "scheduled"}, message_id="run-one"
    )
    assert "private owner conversation" in await tool.execute("create", **CREATE)
    assert not api.calls


@pytest.mark.asyncio
async def test_unknown_workflow_and_missing_schedule_never_mutate(setup):
    tool, api, _ = setup
    assert "missing" in await tool.execute("create", title="Briefing")
    assert "this conversation" in await tool.execute(
        "update", workflow_id="wf_" + "a" * 32, instructions="Changed"
    )
    assert all(request.method == "GET" for request in api.calls)


@pytest.mark.asyncio
async def test_same_turn_cannot_silently_replace_saved_creation(setup):
    tool, api, _ = setup
    await tool.execute("create", **CREATE)
    assert "already created a different briefing" in await tool.execute(
        "create", **{**CREATE, "instructions": "Different"}
    )
    assert len(api.workflows) == 1


@pytest.mark.asyncio
async def test_real_agent_loop_binds_client_message_identity(setup, tmp_path):
    from unittest.mock import AsyncMock, MagicMock

    from nanobot.agent.loop import AgentLoop
    from nanobot.bus.events import InboundMessage
    from nanobot.bus.queue import MessageBus
    from nanobot.config.schema import ToolsConfig
    from nanobot.providers.base import LLMResponse, ToolCallRequest

    tool, api, sessions = setup
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    responses = [
        LLMResponse(
            content="",
            tool_calls=[
                ToolCallRequest(
                    id="create-briefing", name="briefing", arguments={"action": "create", **CREATE}
                )
            ],
            usage={},
        ),
        LLMResponse(content="Your first edition is queued.", tool_calls=[], usage={}),
    ]
    provider.chat_with_retry = AsyncMock(side_effect=responses)
    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=sessions.workspace,
        session_manager=sessions,
        model="test-model",
        tools_config=ToolsConfig(briefing=tool._config),
    )
    loop.tools.get("briefing")._transport = httpx.MockTransport(api.handle)
    loop.consolidator.maybe_consolidate_by_tokens = AsyncMock(return_value=False)
    result = await loop._process_message(
        InboundMessage(
            channel="websocket",
            sender_id="owner",
            chat_id="chat-one",
            content="Make a Swift briefing every weekday at 8am in Los Angeles.",
            metadata={"client_message_id": "durable-message-one"},
        )
    )
    assert result.content == "Your first edition is queued."
    assert len(api.workflows) == 1
    assert len(api.run_keys) == 1
