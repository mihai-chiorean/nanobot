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


@pytest.mark.asyncio
async def test_schedule_work_requires_confirmation_for_recurring_job(
    tmp_path: Path,
) -> None:
    tool = _make_tool(tmp_path)

    result = await tool.execute(
        title="Daily digest",
        goal="Summarize backend health.",
        instructions="Check services and publish backend-health-digest.md.",
        schedule_kind="cron",
        cron_expr="0 8 * * *",
        deliverable="markdown_digest",
        tools_needed=["shell"],
        risk_level="low",
        risk_notes="Local read/report job.",
        confirmed=False,
    )

    assert result.startswith("Confirmation required")
    assert tool._cron.list_jobs() == []
    assert tool._work_store.list_tasks() == []


@pytest.mark.asyncio
async def test_schedule_work_creates_visible_plan_and_cron_job(tmp_path: Path) -> None:
    tool = _make_tool(tmp_path)

    result = await tool.execute(
        title="Daily digest",
        goal="Summarize backend health.",
        instructions="Check services and publish backend-health-digest.md.",
        schedule_kind="cron",
        cron_expr="0 8 * * *",
        tz="America/Los_Angeles",
        deliverable="markdown_digest",
        tools_needed=["shell", "web_search"],
        risk_level="low",
        risk_notes="Local read/report job.",
        confirmed=True,
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
            title="One-time digest",
            goal="Summarize backend health.",
            instructions="Check services and publish a digest.",
            schedule_kind="at",
            at="2027-01-01T08:00:00",
            deliverable="markdown_digest",
            risk_level="low",
            confirmed=True,
        )

    assert result == "Error: failed to create the scheduled Work job."
    task = tool._work_store.list_tasks()[0]
    assert task["status"] == "failed"
    assert task["error"] == "Scheduling failed: OSError"
