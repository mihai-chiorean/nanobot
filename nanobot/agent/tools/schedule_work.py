"""Tool for creating scheduled Work plans."""

from __future__ import annotations

import json
from contextvars import ContextVar
from datetime import datetime
from typing import Any

from loguru import logger

from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.schema import (
    ArraySchema,
    BooleanSchema,
    IntegerSchema,
    StringSchema,
    tool_parameters_schema,
)
from nanobot.cron.service import CronService
from nanobot.cron.types import CronSchedule
from nanobot.work.store import WorkStore

_PARAMETERS = tool_parameters_schema(
    title=StringSchema("Short title for the scheduled Work plan."),
    goal=StringSchema("One-sentence outcome the work should accomplish."),
    instructions=StringSchema(
        "Full instructions to run each time the schedule fires. Include expected sources, "
        "constraints, output format, and artifact names."
    ),
    schedule_kind=StringSchema("Schedule type.", enum=["at", "every", "cron"]),
    at=StringSchema(
        "ISO datetime for one-time execution, e.g. 2026-07-16T14:30:00. "
        "Naive values use the agent timezone."
    ),
    every_seconds=IntegerSchema(
        0, description="Interval in seconds when schedule_kind='every'.", minimum=1
    ),
    cron_expr=StringSchema("Cron expression when schedule_kind='cron', e.g. 0 9 * * *."),
    tz=StringSchema("Optional IANA timezone for cron schedules."),
    deliverable=StringSchema("Expected durable output, e.g. markdown_digest or report."),
    tools_needed=ArraySchema(
        StringSchema("Tool or connector likely needed."),
        description="Likely tools/connectors, e.g. web_search, gmail, or shell.",
    ),
    risk_level=StringSchema("Risk level for this scheduled work.", enum=["low", "medium", "high"]),
    risk_notes=StringSchema("Why this is safe or risky."),
    confirmed=BooleanSchema(
        description="Set true only after the user explicitly confirms work that requires it.",
        default=False,
    ),
    required=["title", "goal", "instructions", "schedule_kind", "deliverable", "risk_level"],
    description="Create a first-class scheduled Work plan and a cron job that runs it.",
)


@tool_parameters(_PARAMETERS)
class ScheduleWorkTool(Tool):
    def __init__(
        self,
        cron_service: CronService,
        work_store: WorkStore,
        default_timezone: str = "UTC",
        model_name: str = "",
    ) -> None:
        self._cron = cron_service
        self._work_store = work_store
        self._default_timezone = default_timezone
        self._model_name = model_name
        self._channel: ContextVar[str] = ContextVar("schedule_work_channel", default="")
        self._chat_id: ContextVar[str] = ContextVar("schedule_work_chat_id", default="")
        self._metadata: ContextVar[dict] = ContextVar("schedule_work_metadata", default={})
        self._session_key: ContextVar[str] = ContextVar("schedule_work_session_key", default="")

    def set_context(
        self,
        channel: str,
        chat_id: str,
        metadata: dict | None = None,
        session_key: str | None = None,
    ) -> None:
        self._channel.set(channel)
        self._chat_id.set(chat_id)
        self._metadata.set(metadata or {})
        self._session_key.set(session_key or f"{channel}:{chat_id}")

    @property
    def name(self) -> str:
        return "schedule_work"

    @property
    def description(self) -> str:
        return (
            "Create a visible scheduled Work plan and schedule it to run in the background. "
            "Ask the user for confirmation when the work is recurring, risky, ambiguous, "
            "externally visible, uses private data, or may spend money."
        )

    async def execute(
        self,
        title: str,
        goal: str,
        instructions: str,
        schedule_kind: str,
        deliverable: str,
        risk_level: str,
        at: str | None = None,
        every_seconds: int | None = None,
        cron_expr: str | None = None,
        tz: str | None = None,
        tools_needed: list[str] | None = None,
        risk_notes: str | None = None,
        confirmed: bool = False,
        **_: Any,
    ) -> str:
        title = self._clean(title, 96)
        goal = self._clean(goal, 300)
        instructions = self._clean(instructions, 4000)
        deliverable = self._clean(deliverable, 120)
        risk_level = (risk_level or "").strip().lower()
        risk_notes = self._clean(risk_notes or "", 500)
        tools = [self._clean(str(item), 64) for item in (tools_needed or []) if str(item).strip()]
        if not title or not goal or not instructions:
            return "Error: title, goal, and instructions are required."
        if risk_level not in {"low", "medium", "high"}:
            return "Error: risk_level must be low, medium, or high."

        schedule, label, delete_after_run, error = self._build_schedule(
            schedule_kind=schedule_kind,
            at=at,
            every_seconds=every_seconds,
            cron_expr=cron_expr,
            tz=tz,
        )
        if error:
            return error
        assert schedule is not None
        if (
            self._requires_confirmation(
                schedule_kind=schedule.kind,
                risk_level=risk_level,
                tools_needed=tools,
                risk_notes=risk_notes,
            )
            and not confirmed
        ):
            return (
                "Confirmation required before scheduling this Work plan. "
                "Use ask_user with this summary, then call schedule_work again with confirmed=true.\n\n"
                + self._confirmation_summary(
                    title=title,
                    goal=goal,
                    schedule_label=label,
                    deliverable=deliverable,
                    risk_level=risk_level,
                    risk_notes=risk_notes,
                    tools_needed=tools,
                )
            )

        channel = self._channel.get()
        chat_id = self._chat_id.get()
        if not channel or not chat_id:
            return "Error: no session context (channel/chat_id)"
        session_key = self._session_key.get() or f"{channel}:{chat_id}"
        plan = {
            "title": title,
            "goal": goal,
            "instructions": instructions,
            "schedule": {
                "kind": schedule.kind,
                "label": label,
                "at": at,
                "every_seconds": every_seconds,
                "cron_expr": cron_expr,
                "timezone": tz or self._default_timezone,
            },
            "deliverable": deliverable,
            "tools_needed": tools,
            "risk_level": risk_level,
            "risk_notes": risk_notes,
            "confirmed": bool(confirmed),
        }
        plan_task = await self._work_store.run_io(
            self._work_store.create_task,
            session_key=session_key,
            chat_id=chat_id,
            content=instructions,
            mode="scheduled_plan",
            title=title,
            model=self._model_name,
            status="scheduled",
        )
        plan_task_id = str(plan_task["task_id"])
        await self._work_store.run_io(
            self._work_store.append_event,
            plan_task_id,
            "plan.created",
            {"plan": plan},
            actor="planner",
        )
        await self._work_store.run_io(
            self._work_store.add_artifact,
            plan_task_id,
            name="work-plan.md",
            kind="markdown",
            content=self._format_plan_markdown(plan),
            summary=f"{label}; {deliverable}",
        )
        channel_meta = dict(self._metadata.get() or {})
        channel_meta.update(
            {
                "work_title": title,
                "work_chat_id": chat_id,
                "work_plan_task_id": plan_task_id,
                "work_deliverable": deliverable,
            }
        )
        try:
            job = self._cron.add_job(
                name=title,
                schedule=schedule,
                message=instructions,
                payload_kind="work_task",
                deliver=False,
                channel=channel,
                to=chat_id,
                delete_after_run=delete_after_run,
                channel_meta=channel_meta,
                session_key=session_key,
            )
        except Exception as exc:
            logger.exception("Failed to schedule Work plan {}", plan_task_id)
            await self._work_store.run_io(
                self._work_store.update_status,
                plan_task_id,
                "failed",
                error=f"Scheduling failed: {type(exc).__name__}",
            )
            return "Error: failed to create the scheduled Work job."
        await self._work_store.run_io(
            self._work_store.append_event,
            plan_task_id,
            "schedule.created",
            {"cron_job_id": job.id, "schedule": plan["schedule"]},
            actor="planner",
        )
        await self._work_store.run_io(
            self._work_store.update_status,
            plan_task_id,
            "scheduled",
            result_summary=f"Scheduled: {label}. Deliverable: {deliverable}.",
        )
        return (
            f"Scheduled Work plan '{title}' as job {job.id}. "
            f"Plan task: {plan_task_id}. Schedule: {label}."
        )

    @staticmethod
    def _clean(value: str | None, limit: int) -> str:
        return " ".join((value or "").split())[:limit]

    def _build_schedule(
        self,
        *,
        schedule_kind: str,
        at: str | None,
        every_seconds: int | None,
        cron_expr: str | None,
        tz: str | None,
    ) -> tuple[CronSchedule | None, str, bool, str | None]:
        kind = (schedule_kind or "").strip().lower()
        if kind == "every":
            if not every_seconds or every_seconds <= 0:
                return (
                    None,
                    "",
                    False,
                    "Error: every_seconds is required for schedule_kind='every'.",
                )
            return (
                CronSchedule(kind="every", every_ms=every_seconds * 1000),
                self._format_every(every_seconds),
                False,
                None,
            )
        if kind == "cron":
            if not cron_expr:
                return None, "", False, "Error: cron_expr is required for schedule_kind='cron'."
            effective_tz = tz or self._default_timezone
            if error := self._validate_timezone(effective_tz):
                return None, "", False, error
            return (
                CronSchedule(kind="cron", expr=cron_expr, tz=effective_tz),
                f"cron {cron_expr} ({effective_tz})",
                False,
                None,
            )
        if kind == "at":
            if not at:
                return None, "", False, "Error: at is required for schedule_kind='at'."
            try:
                value = datetime.fromisoformat(at)
            except ValueError:
                return (
                    None,
                    "",
                    False,
                    f"Error: invalid ISO datetime format '{at}'. Expected YYYY-MM-DDTHH:MM:SS.",
                )
            if value.tzinfo is None:
                from zoneinfo import ZoneInfo

                if error := self._validate_timezone(self._default_timezone):
                    return None, "", False, error
                value = value.replace(tzinfo=ZoneInfo(self._default_timezone))
            return (
                CronSchedule(kind="at", at_ms=int(value.timestamp() * 1000)),
                f"at {value.isoformat()}",
                True,
                None,
            )
        return None, "", False, "Error: schedule_kind must be at, every, or cron."

    @staticmethod
    def _format_every(seconds: int) -> str:
        if seconds % 3600 == 0:
            return f"every {seconds // 3600}h"
        if seconds % 60 == 0:
            return f"every {seconds // 60}m"
        return f"every {seconds}s"

    @staticmethod
    def _validate_timezone(timezone_name: str) -> str | None:
        from zoneinfo import ZoneInfo

        try:
            ZoneInfo(timezone_name)
        except Exception:
            return f"Error: unknown timezone '{timezone_name}'"
        return None

    @staticmethod
    def _requires_confirmation(
        *, schedule_kind: str, risk_level: str, tools_needed: list[str], risk_notes: str
    ) -> bool:
        if schedule_kind in {"cron", "every"} or risk_level in {"medium", "high"}:
            return True
        joined = " ".join([*tools_needed, risk_notes]).lower()
        markers = (
            "gmail",
            "email",
            "private",
            "secret",
            "delete",
            "write",
            "send",
            "post",
            "payment",
            "paid",
            "external",
            "customer",
        )
        return any(marker in joined for marker in markers)

    @staticmethod
    def _confirmation_summary(
        *,
        title: str,
        goal: str,
        schedule_label: str,
        deliverable: str,
        risk_level: str,
        risk_notes: str,
        tools_needed: list[str],
    ) -> str:
        tools = ", ".join(tools_needed) if tools_needed else "none declared"
        notes = risk_notes or "No extra risk notes provided."
        return (
            f"Title: {title}\nGoal: {goal}\nSchedule: {schedule_label}\n"
            f"Deliverable: {deliverable}\nTools: {tools}\nRisk: {risk_level} - {notes}"
        )

    @staticmethod
    def _format_plan_markdown(plan: dict[str, Any]) -> str:
        tools = ", ".join(plan.get("tools_needed") or []) or "None declared"
        return (
            f"# {plan['title']}\n\n## Goal\n{plan['goal']}\n\n"
            f"## Schedule\n{plan['schedule']['label']}\n\n"
            f"## Deliverable\n{plan['deliverable']}\n\n## Tools\n{tools}\n\n"
            f"## Risk\n{plan['risk_level']}: {plan.get('risk_notes') or 'No notes'}\n\n"
            f"## Instructions\n{plan['instructions']}\n\n## JSON\n```json\n"
            f"{json.dumps(plan, indent=2, ensure_ascii=False)}\n```\n"
        )
