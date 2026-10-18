"""SR-24: ``work.scheduleOwner`` decides who owns new Work plans.

* owner ``nanobot`` (default) -> today's behaviour: a runtime-local
  ``work_task`` cron job is created and the connector tool is not touched;
* owner ``ziggy-work`` -> the connector's ``work_schedule_create`` MCP tool is
  called with the mapped schedule and the local cron store stays untouched;
* missing connector tool -> clear error, nothing written;
* ``cron`` rejects the legacy ``work_task`` call shape under ``ziggy-work``
  and leaves reminders (``agent_turn``) alone.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from nanobot.agent.tools.base import Tool, ToolResult
from nanobot.agent.tools.context import RequestContext, request_context
from nanobot.agent.tools.cron import CronTool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.schedule_work import ScheduleWorkTool
from nanobot.cron.service import CronService
from nanobot.cron.work_runner import work_routing
from nanobot.work.store import WorkStore

CONNECTOR_REGISTERED_NAME = "mcp_ziggy-work_work_schedule_create"


class _FakeConnector(Tool):
    """Stand-in for the SR-23 connector MCP tool ``work_schedule_create``."""

    _plugin_discoverable = False

    def __init__(self, *, fail: bool = False) -> None:
        self._original_name = "work_schedule_create"
        self.calls: list[dict[str, Any]] = []
        self._fail = fail

    @property
    def name(self) -> str:
        return CONNECTOR_REGISTERED_NAME

    @property
    def description(self) -> str:
        return "Fake ziggy-work schedule connector."

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self._fail:
            return ToolResult.error("Error: ziggy-work refused the schedule")
        return "schedule created"


def _make_tool(
    tmp_path: Path,
    *,
    schedule_owner: str = "nanobot",
    connector: _FakeConnector | None = None,
) -> tuple[ScheduleWorkTool, ToolRegistry]:
    cron = CronService(tmp_path / "cron" / "jobs.json")
    store = WorkStore(tmp_path)
    registry = ToolRegistry()
    if connector is not None:
        registry.register(connector)
    tool = ScheduleWorkTool(
        cron,
        store,
        default_timezone="America/Los_Angeles",
        model_name="test-model",
        schedule_owner=schedule_owner,
        tool_registry=registry,
    )
    return tool, registry


def _bind(metadata: dict[str, Any] | None = None):
    return request_context(
        RequestContext(
            channel="websocket",
            chat_id="chat-1",
            session_key="websocket:chat-1",
            metadata={"request_id": "request-1", **(metadata or {})},
        )
    )


def _complete_workflow(**overrides: object) -> dict[str, object]:
    workflow: dict[str, object] = {
        "title": "Daily digest",
        "goal": "Summarize backend health.",
        "instructions": "Check services and publish backend-health-digest.md.",
        "schedule_kind": "cron",
        "cron_expr": "0 8 * * *",
        "deliverable": "markdown_digest",
        "success_criteria": "The digest reports every unhealthy backend service.",
        "delivery": "Publish in the Work tab.",
        "tools_needed": ["shell"],
        "assumptions": [],
        "open_questions": [],
        "context_confidence": 95,
        "risk_level": "low",
        "risk_notes": "Local read/report job.",
        "confirmed": True,
    }
    workflow.update(overrides)
    return workflow


# ---------------------------------------------------------------------------
# Owner "nanobot" -> today's behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_nanobot_owner_writes_local_work_task_cron_job(tmp_path: Path) -> None:
    connector = _FakeConnector()
    tool, _ = _make_tool(tmp_path, schedule_owner="nanobot", connector=connector)

    with _bind():
        result = await tool.execute(**_complete_workflow(tz="America/Los_Angeles"))

    assert "Scheduled Work plan 'Daily digest'" in result
    jobs = tool._cron.list_jobs()
    assert len(jobs) == 1
    assert jobs[0].payload.kind == "work_task"
    # Loading normalizes the payload; the work_* hints survive in origin_metadata.
    assert work_routing(jobs[0])["work_title"] == "Daily digest"
    # The connector is never contacted under the nanobot owner.
    assert connector.calls == []


@pytest.mark.asyncio
async def test_nanobot_owner_is_the_default(tmp_path: Path) -> None:
    tool, _ = _make_tool(tmp_path)

    with _bind():
        result = await tool.execute(**_complete_workflow())

    assert "as job" in result
    assert len(tool._cron.list_jobs()) == 1


# ---------------------------------------------------------------------------
# Owner "ziggy-work" -> connector call, no local cron write
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ziggy_work_owner_calls_connector_and_skips_cron_store(tmp_path: Path) -> None:
    connector = _FakeConnector()
    tool, _ = _make_tool(
        tmp_path,
        schedule_owner="ziggy-work",
        connector=connector,
    )

    with _bind({"skill": "fare-watch"}):
        result = await tool.execute(**_complete_workflow(tz="America/Los_Angeles"))

    assert "Scheduled Work plan 'Daily digest' in ziggy-work" in result
    (call,) = connector.calls
    assert call["title"] == "Daily digest"
    assert call["content"] == "Check services and publish backend-health-digest.md."
    assert call["cron"] == "0 8 * * *"
    assert call["timezone"] == "America/Los_Angeles"
    assert call["skill"] == "fare-watch"
    assert "fire_time" not in call
    # The local cron store stays untouched.
    assert tool._cron.list_jobs() == []
    # The plan task is still recorded for visibility.
    tasks = tool._work_store.list_tasks()
    assert len(tasks) == 1
    assert tasks[0]["mode"] == "scheduled_plan"


@pytest.mark.asyncio
async def test_ziggy_work_maps_every_to_at_every_seconds(tmp_path: Path) -> None:
    connector = _FakeConnector()
    tool, _ = _make_tool(tmp_path, schedule_owner="ziggy-work", connector=connector)

    with _bind():
        await tool.execute(
            **_complete_workflow(
                schedule_kind="every",
                cron_expr=None,
                every_seconds=3600,
            )
        )

    (call,) = connector.calls
    assert call["cron"] == "@every 3600s"
    assert "fire_time" not in call
    assert tool._cron.list_jobs() == []


@pytest.mark.asyncio
async def test_ziggy_work_maps_at_to_one_shot_with_fire_time(tmp_path: Path) -> None:
    connector = _FakeConnector()
    tool, _ = _make_tool(tmp_path, schedule_owner="ziggy-work", connector=connector)

    with _bind():
        await tool.execute(
            **_complete_workflow(
                title="One-time digest",
                schedule_kind="at",
                cron_expr=None,
                at="2027-01-01T08:00:00-08:00",
            )
        )

    (call,) = connector.calls
    assert call["cron"] is None  # one-shot: cron NULL with a fire time
    expected = datetime(2027, 1, 1, 16, 0, tzinfo=timezone.utc)
    assert datetime.fromisoformat(str(call["fire_time"])) == expected
    assert tool._cron.list_jobs() == []


@pytest.mark.asyncio
async def test_ziggy_work_owner_without_connector_tool_writes_nothing(
    tmp_path: Path,
) -> None:
    tool, registry = _make_tool(tmp_path, schedule_owner="ziggy-work")

    with _bind():
        result = await tool.execute(**_complete_workflow())

    assert str(result).startswith("Error:")
    assert "work_schedule_create" in str(result)
    assert "Nothing was scheduled" in str(result)
    assert tool._cron.list_jobs() == []
    assert tool._work_store.list_tasks() == []
    assert registry.tool_names == []


@pytest.mark.asyncio
async def test_ziggy_work_connector_failure_writes_nothing(tmp_path: Path) -> None:
    connector = _FakeConnector(fail=True)
    tool, _ = _make_tool(tmp_path, schedule_owner="ziggy-work", connector=connector)

    with _bind():
        result = await tool.execute(**_complete_workflow())

    assert str(result).startswith("Error:")
    assert "ziggy-work rejected the schedule" in str(result)
    assert tool._cron.list_jobs() == []
    assert tool._work_store.list_tasks() == []


@pytest.mark.asyncio
async def test_ziggy_work_still_enforces_intake_before_connecting(
    tmp_path: Path,
) -> None:
    connector = _FakeConnector()
    tool, _ = _make_tool(
        tmp_path, schedule_owner="ziggy-work", connector=connector,
    )

    with _bind({"skill": "fare-watch"}):
        result = await tool.execute(**_complete_workflow(context_confidence=50))

    assert result.startswith("Workflow interview required")
    assert connector.calls == []
    assert tool._cron.list_jobs() == []


# ---------------------------------------------------------------------------
# ``cron`` rejects work_task under ziggy-work; reminders unchanged
# ---------------------------------------------------------------------------


def _cron_tool(tmp_path: Path, schedule_owner: str) -> CronTool:
    service = CronService(tmp_path / "cron" / "jobs.json")
    return CronTool(service, default_timezone="UTC", schedule_owner=schedule_owner)


@pytest.mark.asyncio
async def test_cron_rejects_work_task_attempts_under_ziggy_work(
    tmp_path: Path,
) -> None:
    tool = _cron_tool(tmp_path, "ziggy-work")

    with _bind():
        result = await tool.execute(
            action="add",
            message="Run the daily ingest pipeline",
            every_seconds=3600,
            as_work=True,
        )

    assert str(result).startswith("Error:")
    assert "schedule_work" in str(result)
    assert "ziggy-work" in str(result)
    assert tool._cron.list_jobs() == []


@pytest.mark.asyncio
async def test_cron_rejects_string_true_work_task_attempt(tmp_path: Path) -> None:
    tool = _cron_tool(tmp_path, "ziggy-work")

    with _bind():
        result = await tool.execute(
            action="add",
            message="Run the daily ingest pipeline",
            every_seconds=3600,
            as_work="true",
        )

    assert str(result).startswith("Error:")
    assert "schedule_work" in str(result)
    assert tool._cron.list_jobs() == []


@pytest.mark.asyncio
async def test_cron_reminders_still_work_under_ziggy_work(tmp_path: Path) -> None:
    """Reminders (``agent_turn``) are unchanged by the scheduler switch."""
    tool = _cron_tool(tmp_path, "ziggy-work")

    with _bind():
        result = await tool.execute(
            action="add",
            message="Remind me to stretch",
            every_seconds=3600,
        )

    assert "Created job" in result
    (job,) = tool._cron.list_jobs()
    assert job.payload.kind == "agent_turn"


@pytest.mark.asyncio
async def test_cron_rejects_work_task_attempts_under_nanobot_owner(
    tmp_path: Path,
) -> None:
    """Background Work always goes through schedule_work: the legacy as_work
    call shape may never create a job, whatever the owner."""
    tool = _cron_tool(tmp_path, "nanobot")

    with _bind():
        result = await tool.execute(
            action="add",
            message="Run the daily ingest pipeline",
            every_seconds=3600,
            as_work=True,
        )

    assert str(result).startswith("Error:")
    assert "schedule_work" in str(result)
    # Negative control: the nanobot owner keeps the plain error without the
    # ziggy-work forwarding hint.
    assert "ziggy-work" not in str(result)
    assert tool._cron.list_jobs() == []


# ---------------------------------------------------------------------------
# Config -> AgentLoop -> tool wiring
# ---------------------------------------------------------------------------


def _agent_loop(tmp_path: Path, schedule_owner: str | None):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from nanobot.agent.loop import AgentLoop
    from nanobot.bus.queue import MessageBus
    from nanobot.config.schema import Config

    provider = MagicMock(name="test-model")
    provider.get_default_model.return_value = "test-model"
    provider.generation = SimpleNamespace(
        max_tokens=8192, temperature=0.1, reasoning_effort=None
    )
    payload: dict[str, Any] = {"agents": {"defaults": {"workspace": str(tmp_path)}}}
    if schedule_owner is not None:
        payload["work"] = {"scheduleOwner": schedule_owner}
    config = Config.model_validate(payload)
    return AgentLoop.from_config(
        config,
        bus=MessageBus(),
        tool_registry=ToolRegistry(),
        provider=provider,
        cron_service=CronService(tmp_path / "cron" / "jobs.json"),
    )


def test_from_config_propagates_schedule_owner(tmp_path: Path) -> None:
    loop = _agent_loop(tmp_path, "ziggy-work")

    schedule_tool = loop.tools.get("schedule_work")
    cron_tool = loop.tools.get("cron")
    assert schedule_tool is not None and cron_tool is not None
    assert schedule_tool._schedule_owner == "ziggy-work"
    assert cron_tool._schedule_owner == "ziggy-work"
    # The tool sees the live registry it was registered into.
    assert schedule_tool._tool_registry is loop.tools


def test_from_config_defaults_to_nanobot_owner(tmp_path: Path) -> None:
    loop = _agent_loop(tmp_path, None)

    schedule_tool = loop.tools.get("schedule_work")
    cron_tool = loop.tools.get("cron")
    assert schedule_tool is not None and cron_tool is not None
    assert schedule_tool._schedule_owner == "nanobot"
    assert cron_tool._schedule_owner == "nanobot"
