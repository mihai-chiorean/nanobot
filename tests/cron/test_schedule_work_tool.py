from pathlib import Path
from unittest.mock import patch

import pytest

from nanobot.agent.tools.schedule_work import ScheduleWorkTool
from nanobot.cron.service import CronService
from nanobot.work.store import WorkStore


def _make_tool(tmp_path: Path) -> ScheduleWorkTool:
    cron = CronService(tmp_path / "cron" / "jobs.json")
    store = WorkStore(tmp_path)
    tool = ScheduleWorkTool(
        cron,
        store,
        default_timezone="America/Los_Angeles",
        model_name="test-model",
    )
    tool.set_context(
        "websocket",
        "chat-1",
        metadata={"request_id": "request-1"},
        session_key="websocket:chat-1",
    )
    return tool


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
        "confirmed": False,
    }
    workflow.update(overrides)
    return workflow


@pytest.mark.asyncio
async def test_schedule_work_requires_confirmation_for_recurring_job(
    tmp_path: Path,
) -> None:
    tool = _make_tool(tmp_path)

    result = await tool.execute(**_complete_workflow())

    assert result.startswith("Confirmation required")
    assert tool._cron.list_jobs() == []
    assert tool._work_store.list_tasks() == []


@pytest.mark.asyncio
async def test_schedule_work_creates_visible_plan_and_cron_job(tmp_path: Path) -> None:
    tool = _make_tool(tmp_path)

    result = await tool.execute(
        **_complete_workflow(
            tz="America/Los_Angeles",
            tools_needed=["shell", "web_search"],
            assumptions=["Backend status endpoints are available locally."],
            confirmed=True,
        )
    )

    assert "Scheduled Work plan 'Daily digest'" in result
    jobs = tool._cron.list_jobs()
    assert len(jobs) == 1
    job = jobs[0]
    assert job.payload.kind == "work_task"
    assert job.payload.channel_meta["work_plan_task_id"].startswith("work_")
    assert job.payload.channel_meta["work_title"] == "Daily digest"

    tasks = tool._work_store.list_tasks()
    assert len(tasks) == 1
    plan = tool._work_store.task_snapshot(tasks[0]["task_id"])
    assert plan is not None
    assert plan["status"] == "scheduled"
    assert plan["mode"] == "scheduled_plan"
    assert plan["artifact_count"] == 1
    assert plan["artifacts"][0]["name"] == "work-plan.md"
    plan_event = tool._work_store.list_events(plan["task_id"])[1]
    assert plan_event["payload"]["plan"]["context_confidence"] == 95
    assert plan_event["payload"]["plan"]["assumptions"] == [
        "Backend status endpoints are available locally."
    ]
    artifact = tool._work_store.artifact_path(plan["artifacts"][0]["artifact_id"])
    assert artifact is not None
    artifact_content = artifact[0].read_text(encoding="utf-8")
    assert "## Success Criteria" in artifact_content
    assert "## Context Confidence\n95%" in artifact_content
    assert [event["type"] for event in tool._work_store.list_events(plan["task_id"])] == [
        "task.created",
        "plan.created",
        "artifact.created",
        "schedule.created",
        "status.changed",
    ]


@pytest.mark.asyncio
async def test_schedule_failure_marks_plan_failed(tmp_path: Path) -> None:
    tool = _make_tool(tmp_path)

    with patch.object(tool._cron, "add_job", side_effect=OSError("disk full")):
        result = await tool.execute(
            **_complete_workflow(
                title="One-time digest",
                instructions="Check services and publish a digest.",
                schedule_kind="at",
                cron_expr=None,
                at="2027-01-01T08:00:00",
                confirmed=True,
            )
        )

    assert result == "Error: failed to create the scheduled Work job."
    task = tool._work_store.list_tasks()[0]
    assert task["status"] == "failed"
    assert task["error"] == "Scheduling failed: OSError"


@pytest.mark.asyncio
async def test_schedule_work_interviews_before_open_questions_are_resolved(
    tmp_path: Path,
) -> None:
    tool = _make_tool(tmp_path)

    result = await tool.execute(
        **_complete_workflow(
            open_questions=[
                "Which Gmail labels should be included?",
                "Should the digest include archived messages?",
            ],
            context_confidence=70,
        )
    )

    assert result.startswith("Workflow interview required")
    assert "Which Gmail labels should be included?" in result
    assert tool._cron.list_jobs() == []
    assert tool._work_store.list_tasks() == []


@pytest.mark.asyncio
async def test_schedule_work_interviews_when_context_confidence_is_low(
    tmp_path: Path,
) -> None:
    tool = _make_tool(tmp_path)

    result = await tool.execute(
        **_complete_workflow(context_confidence=79)
    )

    assert "context confidence is only 79 percent" in result
    assert "ask a concrete question" in result
    assert tool._cron.list_jobs() == []
    assert tool._work_store.list_tasks() == []


@pytest.mark.asyncio
async def test_schedule_work_requires_confirmation_for_assumptions(
    tmp_path: Path,
) -> None:
    tool = _make_tool(tmp_path)

    result = await tool.execute(
        **_complete_workflow(
            schedule_kind="at",
            cron_expr=None,
            at="2027-01-01T08:00:00",
            assumptions=["Use the primary Gmail account."],
        )
    )

    assert result.startswith("Confirmation required")
    assert "Assumptions: Use the primary Gmail account." in result
    assert "Context confidence: 95%" in result
    assert tool._cron.list_jobs() == []
    assert tool._work_store.list_tasks() == []


@pytest.mark.asyncio
async def test_schedule_work_rejects_empty_deliverable(tmp_path: Path) -> None:
    tool = _make_tool(tmp_path)

    result = await tool.execute(**_complete_workflow(deliverable=" "))

    assert "deliverable" in result
    assert tool._cron.list_jobs() == []
    assert tool._work_store.list_tasks() == []


def test_schedule_work_schema_requires_intake_evidence(tmp_path: Path) -> None:
    tool = _make_tool(tmp_path)

    assert {
        "success_criteria",
        "delivery",
        "assumptions",
        "open_questions",
        "context_confidence",
    }.issubset(tool.parameters["required"])
