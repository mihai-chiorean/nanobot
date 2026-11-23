"""SR-24 (MIT-1859): ``schedule_work`` creates schedules in ziggy-work.

ziggy-work is the only scheduler: ``schedule_work`` always forwards the
approved plan to the connector's ``work_schedule_create`` MCP tool and never
writes a runtime-local ``work_task`` cron job. Cover:

* connector called with the mapped schedule (cron / @every / RFC3339 ``at``);
* the turn's ``ziggy.dev/idempotency_key`` reaches the connector call (SR-11);
* missing connector tool -> clear error, nothing written;
* connector failure -> clear error, nothing written;
* intake still gates the connector call;
* ``cron`` rejects the legacy ``work_task`` call shape; reminders unchanged.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from mcp import types as mcp_types

from nanobot.agent.tools.base import Tool, ToolResult
from nanobot.agent.tools.context import RequestContext, request_context
from nanobot.agent.tools.cron import CronTool
from nanobot.agent.tools.mcp import MCPToolWrapper
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.schedule_work import (
    WORK_SCHEDULE_CONNECTOR_TOOL,
    ScheduleWorkTool,
)
from nanobot.cron.service import CronService
from nanobot.work.store import WorkStore

CONNECTOR_REGISTERED_NAME = "mcp_ziggy-work_work_schedule_create"
_IDEMPOTENCY_KEY_META = "ziggy.dev/idempotency_key"


#: The connector's real ``work_schedule_create`` input schema, copied verbatim
#: from mihai-chiorean/ziggy services/ziggy-connectors/internal/httpapi/
#: testdata/connector-tools.golden.json. Both fixtures below advertise it, so
#: the registry validates every call exactly as it would against the live
#: connector and an argument-shape mismatch fails these tests.
WORK_SCHEDULE_CREATE_INPUT_SCHEMA: dict[str, Any] = {
    "additionalProperties": False,
    "properties": {
        "at": {
            "description": "RFC 3339 timestamp for a one-shot schedule, such as 2026-10-09T07:00:00Z. Provide exactly one of cron or at.",
            "type": "string",
        },
        "content": {
            "description": "Prompt content delivered on each fire, from 1 to 8192 bytes.",
            "type": "string",
        },
        "cron": {
            "description": "Standard cron expression for a recurring schedule, such as '0 7 * * *'. Provide exactly one of cron or at.",
            "type": "string",
        },
        "paused": {
            "description": "Create the schedule paused so it never fires until work_schedule_resume.",
            "type": "boolean",
        },
        "skill": {
            "description": "Optional managed skill name to run on each fire, lowercase letters, digits, and hyphens, at most 64 characters.",
            "type": "string",
        },
        "timezone": {
            "description": "IANA time zone for the cron schedule, such as America/Los_Angeles. Defaults to UTC.",
            "type": "string",
        },
        "title": {
            "description": "Schedule title, from 1 to 200 characters.",
            "type": "string",
        },
    },
    "required": ["title", "content"],
    "type": "object",
}


class _FakeConnector(Tool):
    """Stand-in for the SR-23 connector MCP tool ``work_schedule_create``."""

    _plugin_discoverable = False

    def __init__(self, *, result: Any = "schedule created") -> None:
        self._original_name = WORK_SCHEDULE_CONNECTOR_TOOL
        self.calls: list[dict[str, Any]] = []
        self._result = result

    @property
    def name(self) -> str:
        return CONNECTOR_REGISTERED_NAME

    @property
    def description(self) -> str:
        return "Fake ziggy-work schedule connector."

    @property
    def parameters(self) -> dict[str, Any]:
        return WORK_SCHEDULE_CREATE_INPUT_SCHEMA

    async def execute(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


def _make_tool(
    tmp_path: Path,
    *,
    connector: Tool | None = None,
    registry: ToolRegistry | None = None,
) -> ScheduleWorkTool:
    cron = CronService(tmp_path / "cron" / "jobs.json")
    store = WorkStore(tmp_path)
    if registry is None:
        registry = ToolRegistry()
    if connector is not None:
        registry.register(connector)
    return ScheduleWorkTool(
        cron,
        store,
        default_timezone="America/Los_Angeles",
        model_name="test-model",
        tool_registry=registry,
    )


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


def _assert_unknown_outcome(result: Any) -> None:
    text = str(result)
    assert "outcome" in text and "unknown" in text, text
    assert "work_schedule_list" in text, text
    assert "Nothing was scheduled" not in text, text
    assert "rejected" not in text, text


# ---------------------------------------------------------------------------
# Connector call: argument mapping follows the ziggy-connectors MCP schema
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_schedule_work_calls_connector_and_skips_cron_store(tmp_path: Path) -> None:
    connector = _FakeConnector()
    tool = _make_tool(tmp_path, connector=connector)

    with _bind({"skill": "fare-watch"}):
        result = await tool.execute(**_complete_workflow(tz="America/Los_Angeles"))

    assert "Scheduled Work plan 'Daily digest' in ziggy-work" in str(result)
    (call,) = connector.calls
    assert call["title"] == "Daily digest"
    assert call["content"] == "Check services and publish backend-health-digest.md."
    assert call["cron"] == "0 8 * * *"
    assert call["timezone"] == "America/Los_Angeles"
    assert call["skill"] == "fare-watch"
    # ``at`` is the one-shot argument; the guessed ``fire_time`` never existed.
    assert "at" not in call
    assert "fire_time" not in call
    # The local cron store stays untouched: ziggy-work is the only scheduler.
    assert tool._cron.list_jobs() == []
    # The plan task is still recorded for visibility.
    tasks = tool._work_store.list_tasks()
    assert len(tasks) == 1
    assert tasks[0]["mode"] == "scheduled_plan"


@pytest.mark.asyncio
async def test_schedule_work_maps_every_to_at_every_seconds(tmp_path: Path) -> None:
    connector = _FakeConnector()
    tool = _make_tool(tmp_path, connector=connector)

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
    assert "at" not in call
    assert tool._cron.list_jobs() == []


@pytest.mark.asyncio
async def test_schedule_work_maps_at_to_rfc3339_one_shot(tmp_path: Path) -> None:
    connector = _FakeConnector()
    tool = _make_tool(tmp_path, connector=connector)

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
    # One-shot: ``at`` carries the RFC3339 fire time and ``cron`` is omitted
    # (the connector requires exactly one of the two).
    expected = datetime(2027, 1, 1, 16, 0, tzinfo=timezone.utc)
    assert datetime.fromisoformat(str(call["at"])) == expected
    assert str(call["at"]).endswith("+00:00")
    assert "cron" not in call
    assert "fire_time" not in call
    assert tool._cron.list_jobs() == []


@pytest.mark.asyncio
async def test_schedule_work_records_connector_event_on_plan_task(tmp_path: Path) -> None:
    connector = _FakeConnector()
    tool = _make_tool(tmp_path, connector=connector)

    with _bind():
        await tool.execute(**_complete_workflow())

    (task,) = tool._work_store.list_tasks()
    events = tool._work_store.list_events(str(task["task_id"]))
    created = [event for event in events if event.get("type") == "schedule.created"]
    assert created, events
    payload = created[-1]["payload"]
    assert payload["connector_tool"] == CONNECTOR_REGISTERED_NAME
    assert payload["schedule"]["kind"] == "cron"


# ---------------------------------------------------------------------------
# Missing / failing connector: clear error, nothing written anywhere
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_schedule_work_without_connector_tool_writes_nothing(tmp_path: Path) -> None:
    tool = _make_tool(tmp_path)

    with _bind():
        result = await tool.execute(**_complete_workflow())

    assert str(result).startswith("Error:")
    assert WORK_SCHEDULE_CONNECTOR_TOOL in str(result)
    assert "Nothing was scheduled" in str(result)
    assert tool._cron.list_jobs() == []
    assert tool._work_store.list_tasks() == []


@pytest.mark.asyncio
async def test_schedule_work_without_registry_writes_nothing(tmp_path: Path) -> None:
    tool = _make_tool(tmp_path, registry=None)

    with _bind():
        result = await tool.execute(**_complete_workflow())

    assert str(result).startswith("Error:")
    assert tool._cron.list_jobs() == []
    assert tool._work_store.list_tasks() == []


@pytest.mark.asyncio
async def test_schedule_work_connector_tool_result_error_writes_nothing(
    tmp_path: Path,
) -> None:
    """A connector failure surfaced as ``ToolResult`` (the registry path) fails."""
    connector = _FakeConnector(result=ToolResult.error("Error: ziggy-work refused"))
    tool = _make_tool(tmp_path, connector=connector)

    with _bind():
        result = await tool.execute(**_complete_workflow())

    assert str(result).startswith("Error:")
    assert "ziggy-work rejected the schedule" in str(result)
    assert tool._cron.list_jobs() == []
    assert tool._work_store.list_tasks() == []


@pytest.mark.asyncio
async def test_schedule_work_connector_error_string_writes_nothing(tmp_path: Path) -> None:
    """A connector that returns a bare error *ToolResult* is a failure even when
    the registry is not in the loop (direct tool.execute path)."""
    connector = _FakeConnector(
        result=ToolResult.error(
            "Work rejected the schedule: check the title, content, timezone, "
            "and exactly one of cron or at"
        )
    )
    tool = _make_tool(tmp_path, connector=connector)

    with _bind():
        result = await tool.execute(**_complete_workflow())

    assert str(result).startswith("Error:")
    assert "ziggy-work rejected the schedule" in str(result)
    assert "check the title, content, timezone" in str(result)
    assert "Nothing was scheduled" in str(result)
    assert tool._cron.list_jobs() == []
    assert tool._work_store.list_tasks() == []


@pytest.mark.asyncio
async def test_schedule_work_connector_exception_is_an_unknown_outcome(
    tmp_path: Path,
) -> None:
    """The registry wraps raised exceptions into an error ``ToolResult``; the
    call may have reached ziggy-work, so the outcome is unknown, not refused."""
    connector = _FakeConnector(result=RuntimeError("mcp server unreachable"))
    tool = _make_tool(tmp_path, connector=connector)

    with _bind():
        result = await tool.execute(**_complete_workflow())

    assert str(result).startswith("Error:")
    _assert_unknown_outcome(result)
    assert tool._cron.list_jobs() == []
    assert tool._work_store.list_tasks() == []


@pytest.mark.asyncio
async def test_schedule_work_success_text_starting_with_error_is_not_a_failure(
    tmp_path: Path,
) -> None:
    """Negative control: a success payload that starts with "Error" (e.g. an
    email body) must not be mistaken for a connector failure."""
    connector = _FakeConnector(result="Errors and omissions excepted; schedule created")
    tool = _make_tool(tmp_path, connector=connector)

    with _bind():
        result = await tool.execute(**_complete_workflow())

    assert "Scheduled Work plan 'Daily digest' in ziggy-work" in str(result)
    assert len(tool._work_store.list_tasks()) == 1


# ---------------------------------------------------------------------------
# SR-11: the connector call carries the turn idempotency key
# ---------------------------------------------------------------------------


class _RecordingSession:
    """Fake MCP ClientSession recording how call_tool was invoked."""

    def __init__(
        self,
        *,
        fail: bool = False,
        error_text: str = "Error: rejected",
        raises: BaseException | None = None,
    ) -> None:
        self.calls: list[dict] = []
        self._fail = fail
        self._error_text = error_text
        self._raises = raises

    async def call_tool(self, name, arguments=None, **kwargs):
        self.calls.append({"name": name, "arguments": arguments, **kwargs})
        if self._raises is not None:
            raise self._raises
        if self._fail:
            return SimpleNamespace(
                content=[mcp_types.TextContent(type="text", text=self._error_text)],
                isError=True,
            )
        return SimpleNamespace(
            content=[mcp_types.TextContent(type="text", text="schedule created")],
            isError=False,
        )


def _real_connector_wrapper(
    tmp_path: Path, session: _RecordingSession
) -> tuple[ToolRegistry, MCPToolWrapper]:
    """A registry holding one real ``MCPToolWrapper`` for ``work_schedule_create``."""
    tool_def = SimpleNamespace(
        name=WORK_SCHEDULE_CONNECTOR_TOOL,
        description="Create a schedule in ziggy-work.",
        inputSchema=WORK_SCHEDULE_CREATE_INPUT_SCHEMA,
    )
    wrapper = MCPToolWrapper(session, "ziggy-work", tool_def, tool_timeout=5)
    wrapper._original_name = WORK_SCHEDULE_CONNECTOR_TOOL
    registry = ToolRegistry()
    registry.register(wrapper)
    return registry, wrapper


@pytest.mark.asyncio
async def test_connector_call_carries_the_turn_idempotency_key(tmp_path: Path) -> None:
    """SR-11 + connector idempotency: the non-read-only connector call must
    carry ``ziggy.dev/idempotency_key`` so ziggy-work dedupes a replayed turn."""
    session = _RecordingSession()
    registry, _wrapper = _real_connector_wrapper(tmp_path, session)
    tool = _make_tool(tmp_path, registry=registry)

    with _bind(metadata={"client_message_id": "cm-42"}):
        result = await tool.execute(**_complete_workflow())

    assert "Scheduled Work plan 'Daily digest' in ziggy-work" in str(result)
    (call,) = session.calls
    assert call["name"] == WORK_SCHEDULE_CONNECTOR_TOOL
    assert call["arguments"]["cron"] == "0 8 * * *"
    meta = call["meta"]
    assert meta is not None
    key = meta[_IDEMPOTENCY_KEY_META]
    assert re.fullmatch(r"idem_[a-f0-9]{40}", key)
    # DEC-20: the key is a pure hash; no scope or chat text leaks.
    assert "cm-42" not in key and "chat-1" not in key


@pytest.mark.asyncio
async def test_connector_failure_through_real_wrapper_writes_nothing(
    tmp_path: Path,
) -> None:
    """The MCP wrapper renders an isError result as ``ToolResult.error``; the
    schedule must fail closed with nothing written."""
    session = _RecordingSession(fail=True)
    registry, _wrapper = _real_connector_wrapper(tmp_path, session)
    tool = _make_tool(tmp_path, registry=registry)

    with _bind(metadata={"client_message_id": "cm-42"}):
        result = await tool.execute(**_complete_workflow())

    assert str(result).startswith("Error:")
    assert "ziggy-work rejected the schedule" in str(result)
    assert tool._cron.list_jobs() == []
    assert tool._work_store.list_tasks() == []


@pytest.mark.asyncio
async def test_connector_call_without_a_turn_scope_still_schedules(tmp_path: Path) -> None:
    """No stable turn scope -> no idempotency key (pre-SR-11 behaviour), and
    the schedule is still created."""
    session = _RecordingSession()
    registry, _wrapper = _real_connector_wrapper(tmp_path, session)
    tool = _make_tool(tmp_path, registry=registry)

    with _bind(metadata={"unrelated": "x"}):
        result = await tool.execute(**_complete_workflow())

    assert "Scheduled Work plan 'Daily digest' in ziggy-work" in str(result)
    (call,) = session.calls
    assert _IDEMPOTENCY_KEY_META not in (call["meta"] or {})


@pytest.mark.asyncio
async def test_connector_call_without_a_skill_omits_skill(tmp_path: Path) -> None:
    """No request-metadata skill: the optional ``skill`` field is omitted. The
    connector declares it a string, so sending null is refused by the
    registry's schema validation before any MCP call."""
    session = _RecordingSession()
    registry, _wrapper = _real_connector_wrapper(tmp_path, session)
    tool = _make_tool(tmp_path, registry=registry)

    with _bind(metadata={"client_message_id": "cm-7"}):
        result = await tool.execute(**_complete_workflow())

    assert "Scheduled Work plan 'Daily digest' in ziggy-work" in str(result)
    (call,) = session.calls
    assert call["arguments"] == {
        "title": "Daily digest",
        "content": "Check services and publish backend-health-digest.md.",
        "cron": "0 8 * * *",
        "timezone": "America/Los_Angeles",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "session_kwargs",
    [
        pytest.param({"raises": ConnectionResetError("reset")}, id="connection-reset"),
        pytest.param({"raises": TimeoutError()}, id="timeout"),
        pytest.param(
            {
                "fail": True,
                "error_text": "schedule service is temporarily unavailable; retry shortly",
            },
            id="connector-unavailable",
        ),
    ],
)
async def test_connector_unknown_outcome_never_claims_nothing_was_scheduled(
    tmp_path: Path, session_kwargs: dict[str, Any]
) -> None:
    """The create may have committed in ziggy-work: report an unknown outcome
    and point at work_schedule_list instead of claiming nothing was scheduled."""
    session = _RecordingSession(**session_kwargs)
    registry, _wrapper = _real_connector_wrapper(tmp_path, session)
    tool = _make_tool(tmp_path, registry=registry)

    with _bind(metadata={"client_message_id": "cm-42"}):
        result = await tool.execute(**_complete_workflow())

    assert str(result).startswith("Error:")
    _assert_unknown_outcome(result)
    # A non-idempotent create is never retried automatically.
    assert len(session.calls) == 1
    assert tool._cron.list_jobs() == []
    assert tool._work_store.list_tasks() == []


@pytest.mark.asyncio
async def test_connector_definite_refusal_says_nothing_was_scheduled(
    tmp_path: Path,
) -> None:
    """Negative control: a 400-style refusal is definite."""
    session = _RecordingSession(
        fail=True,
        error_text="Work rejected the schedule: check the title, content, timezone, "
        "and exactly one of cron or at",
    )
    registry, _wrapper = _real_connector_wrapper(tmp_path, session)
    tool = _make_tool(tmp_path, registry=registry)

    with _bind(metadata={"client_message_id": "cm-42"}):
        result = await tool.execute(**_complete_workflow())

    assert "ziggy-work rejected the schedule" in str(result)
    assert "Nothing was scheduled" in str(result)
    assert "unknown" not in str(result)


# ---------------------------------------------------------------------------
# Intake gates the connector call
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_schedule_work_still_enforces_intake_before_connecting(
    tmp_path: Path,
) -> None:
    connector = _FakeConnector()
    tool = _make_tool(tmp_path, connector=connector)

    with _bind({"skill": "fare-watch"}):
        result = await tool.execute(**_complete_workflow(context_confidence=50))

    assert str(result).startswith("Workflow interview required")
    assert connector.calls == []
    assert tool._cron.list_jobs() == []


# ---------------------------------------------------------------------------
# ``cron`` rejects the legacy work_task shape; reminders unchanged
# ---------------------------------------------------------------------------


def _cron_tool(tmp_path: Path) -> CronTool:
    service = CronService(tmp_path / "cron" / "jobs.json")
    return CronTool(service, default_timezone="UTC")


@pytest.mark.asyncio
async def test_cron_rejects_work_task_attempts(tmp_path: Path) -> None:
    tool = _cron_tool(tmp_path)

    with _bind():
        result = await tool.execute(
            action="add",
            message="Run the daily ingest pipeline",
            every_seconds=3600,
            as_work=True,
        )

    assert str(result).startswith("Error:")
    assert "schedule_work" in str(result)
    assert tool._cron.list_jobs() == []


@pytest.mark.asyncio
async def test_cron_rejects_string_true_work_task_attempt(tmp_path: Path) -> None:
    """Providers pass booleans as strings; the legacy flag must still fail."""
    tool = _cron_tool(tmp_path)

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
async def test_cron_reminders_still_work(tmp_path: Path) -> None:
    """Reminders (``agent_turn``) are unchanged: no as_work -> normal job."""
    tool = _cron_tool(tmp_path)

    with _bind():
        result = await tool.execute(
            action="add",
            message="Remind me to stretch",
            every_seconds=3600,
        )

    assert "Created job" in str(result)
    (job,) = tool._cron.list_jobs()
    assert job.payload.kind == "agent_turn"


@pytest.mark.asyncio
async def test_cron_rejects_as_work_without_a_schedule(tmp_path: Path) -> None:
    """Even an as_work call missing its schedule is rejected as Work, not as a
    malformed reminder."""
    tool = _cron_tool(tmp_path)

    with _bind():
        result = await tool.execute(
            action="add",
            message="Run the daily ingest pipeline",
            as_work="yes",
        )

    assert str(result).startswith("Error:")
    assert "schedule_work" in str(result)
    assert tool._cron.list_jobs() == []
