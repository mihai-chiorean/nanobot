"""Execution of ``work_task`` cron jobs (Ziggy-local, MIT-1010).

A ``work_task`` job creates a durable Work task and then runs its prompt in a
**dedicated** ``cron:<job_id>`` session.  The job is still session-bound in the
upstream sense -- ``payload.session_key`` names the session it was created from
and is recorded on the Work row -- but execution deliberately does not happen
inside that session: a scheduled run can take tens of minutes, and running it in
the owner's live chat would serialize against interactive turns and inject
scheduled history into the conversation.

Routing that the pre-0.3.0 payload carried in ``channel_meta`` (``work_chat_id``,
``work_title``, ``work_plan_task_id``, ``work_deliverable``) is read from
``payload.origin_metadata`` after ``_normalize_agent_turn_job`` migrates it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from loguru import logger

from nanobot.agent.skill_script import (
    ScriptResult,
    SkillScriptError,
    SkillScriptInjectionHook,
    build_skill_script_messages,
    run_skill_script,
)
from nanobot.agent.skills import SkillsLoader
from nanobot.agent.tools.allowed_tools import ALLOWED_TOOLS_META_KEY
from nanobot.agent.tools.read_only import READ_ONLY_META_KEY, read_only_value
from nanobot.cron.types import CronJob
from nanobot.runtime_context import RUNTIME_CONTEXT_INPUT_META
from nanobot.work.context import reset_work_context, set_work_context

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pathlib import Path

    from nanobot.agent.hook import AgentHook
    from nanobot.agent.tools.registry import ToolRegistry
    from nanobot.work.store import WorkStore

WORK_TASK_META_TASK_ID = "work_task_id"
WORK_TASK_META_MODE = "work_mode"
WORK_TASK_META_CRON_JOB_ID = "cron_job_id"
WORK_TASK_META_READ_ONLY = READ_ONLY_META_KEY
WORK_TASK_ROUTING_READ_ONLY = "work_read_only"
# SR-18: a run whose skill_script result was injected only interprets output,
# so it needs far fewer tool iterations than an open-ended scheduled turn.
# Runs with no script (none configured, or exec disabled) stay open-ended.
SKILL_RUN_MAX_ITERATIONS = 6
EXEC_TOOL_NAME = "exec"


class WorkCronAgent(Protocol):
    """The slice of ``AgentLoop`` a ``work_task`` run needs."""

    work_store: WorkStore
    workspace: Path
    model: str
    # SR-17/SR-18: registry membership of ``exec`` is the runtime truth for
    # ``tools.exec.enable`` (the loader skips disabled tools).
    tools: ToolRegistry

    async def process_direct(self, content: str, **kwargs: Any) -> Any: ...


def work_routing(job: CronJob) -> dict[str, Any]:
    """Return the ``work_*`` routing hints for *job*, whichever field holds them.

    Reads ``origin_metadata`` first (post-migration shape) and falls back to the
    legacy ``channel_meta`` so a store written by the 0.2.x runtime still works
    before it has been normalized and saved back.
    """
    payload = job.payload
    merged: dict[str, Any] = {}
    merged.update(payload.channel_meta or {})
    merged.update(payload.origin_metadata or {})
    return merged


def work_session_key(job: CronJob) -> str:
    """The dedicated execution session for *job*. Never the bound session."""
    return f"cron:{job.id}"


def _plan_task_id(routing: dict[str, Any]) -> str | None:
    value = routing.get("work_plan_task_id")
    return value if isinstance(value, str) and value.startswith("work_") else None


@dataclass(slots=True)
class _SkillTurnSetup:
    """How a skill changes the scheduled turn (SR-17 scope + SR-18 script run)."""

    content: str
    metadata: dict[str, Any] = field(default_factory=dict)
    hooks: list[AgentHook] = field(default_factory=list)
    max_iterations: int | None = None


async def _prepare_skill_turn(
    agent: WorkCronAgent,
    job: CronJob,
    task_id: str,
) -> _SkillTurnSetup | None:
    """Resolve the job's skill into turn scoping and the pre-run script result.

    Returns ``None`` (plain unscoped turn) when the job names no skill or the
    skill cannot be loaded -- a renamed or deleted skill must not kill the
    owner's digest outright, it degrades to the pre-SR-17 behaviour with a
    warning. When a script is configured it runs exactly once here and is
    injected into the turn as a synthetic ``skill_script`` tool result.
    """
    skill_name = job.payload.skill
    if not skill_name:
        return None
    loader = SkillsLoader(agent.workspace)
    block = loader.build_skill_runtime_context(skill_name)
    if block is None:
        logger.warning(
            "Cron: scheduled Work task {} names unknown skill '{}'; running unscoped",
            task_id,
            skill_name,
        )
        return None

    setup = _SkillTurnSetup(
        content=job.payload.message,
        metadata={RUNTIME_CONTEXT_INPUT_META: [block]},
    )
    allowed = loader.skill_allowed_tools(skill_name)
    if allowed is not None:
        setup.metadata[ALLOWED_TOOLS_META_KEY] = allowed

    spec = loader.skill_script_spec(skill_name)
    if spec is None:
        return setup

    # Running a script is equivalent to an exec call: it may only run where
    # the exec tool is registered, i.e. where tools.exec.enable is true.
    registry = getattr(agent, "tools", None)
    exec_tool = registry.get(EXEC_TOOL_NAME) if registry is not None else None
    if exec_tool is None:
        logger.info(
            "Cron: skill_script_skipped reason=exec_disabled skill={} job={}",
            skill_name,
            job.id,
        )
        return setup

    skill_dir = loader.skill_dir(skill_name)
    if skill_dir is None:  # pragma: no cover - the block above proved it loads
        return setup
    try:
        result = await run_skill_script(
            skill_dir,
            spec.rel_path,
            spec.timeout_s,
            exec_tool.build_subprocess_env(),
        )
    except SkillScriptError as exc:
        logger.warning(
            "Cron: skill_script_refused skill={} job={} error={}", skill_name, job.id, exc
        )
        result = ScriptResult(
            exit_code=None,
            stdout_tail="",
            stderr_tail=str(exc),
            duration_s=0.0,
            timed_out=False,
        )
    logger.info(
        "Cron: skill_script_ran skill={} job={} exit_code={} timed_out={} duration_s={:.1f}",
        skill_name,
        job.id,
        result.exit_code,
        result.timed_out,
        result.duration_s,
    )
    if result.failed:
        setup.content += (
            "\n\nThe skill script failed: report the failure described in the "
            "skill_script output as the result of this run and stop; do not "
            "redo the job by hand."
        )
    # The cap belongs to the script-injected turn only: an agent that ignores
    # the interpret-and-stop result must not spin into the 105-exec failure
    # mode. A skipped script leaves an open-ended job, which keeps the
    # loop's default budget.
    setup.max_iterations = SKILL_RUN_MAX_ITERATIONS
    setup.hooks.append(
        SkillScriptInjectionHook(
            build_skill_script_messages(
                skill_name,
                spec.rel_path,
                result,
                call_id=f"skill_script_{task_id}",
            )
        )
    )
    return setup


async def run_work_task_cron_job(
    job: CronJob,
    *,
    agent: WorkCronAgent,
    deliver: Callable[[Any], Awaitable[None]] | None = None,
) -> str | None:
    """Create the Work task for *job*, run it, deliver it, and record the status.

    ``deliver`` publishes the response to the originating channel. It is not
    optional in practice: all four owner jobs are ``deliver: false`` at the cron
    layer and ``process_direct`` does not publish, so without it a scheduled run
    burns tokens, writes a Work row, and posts nothing.
    """
    store = agent.work_store
    routing = work_routing(job)
    session_key = work_session_key(job)
    chat_id = str(
        routing.get("work_chat_id")
        or job.payload.origin_chat_id
        or job.payload.to
        or f"scheduled:{job.id}"
    )
    channel = job.payload.origin_channel or job.payload.channel or "websocket"
    title = str(routing.get("work_title") or job.name or "Scheduled work")
    plan_task_id = _plan_task_id(routing)
    read_only = read_only_value(routing.get(WORK_TASK_ROUTING_READ_ONLY, False))

    task = await store.run_io(
        store.create_task,
        session_key=session_key,
        chat_id=chat_id,
        content=job.payload.message,
        mode="scheduled",
        title=title,
        model=agent.model,
        read_only=read_only,
    )
    task_id = str(task["task_id"])
    logger.info(
        "Cron: created scheduled Work task {} for job '{}' ({}) bound to {}",
        task_id,
        job.name,
        job.id,
        job.payload.session_key,
    )
    if plan_task_id:
        await store.run_io(
            store.append_event,
            plan_task_id,
            "scheduled_run.created",
            {
                "cron_job_id": job.id,
                "run_task_id": task_id,
                "title": title,
                "bound_session_key": job.payload.session_key,
            },
            actor="scheduler",
        )

    async def _silent(*_args: Any, **_kwargs: Any) -> None:
        return None

    tokens = set_work_context(
        store=store,
        task_id=task_id,
        workspace=agent.workspace,
    )
    try:
        turn_metadata: dict[str, Any] = {
            WORK_TASK_META_TASK_ID: task_id,
            WORK_TASK_META_MODE: "scheduled",
            WORK_TASK_META_CRON_JOB_ID: job.id,
        }
        if read_only_value(task.get("read_only", False)):
            turn_metadata[WORK_TASK_META_READ_ONLY] = True
        skill_setup = await _prepare_skill_turn(agent, job, task_id)
        content = job.payload.message
        run_kwargs: dict[str, Any] = {}
        if skill_setup is not None:
            turn_metadata.update(skill_setup.metadata)
            content = skill_setup.content
            if skill_setup.max_iterations is not None:
                run_kwargs["max_iterations"] = skill_setup.max_iterations
            if skill_setup.hooks:
                run_kwargs["hooks"] = skill_setup.hooks
        response = await agent.process_direct(
            content,
            session_key=session_key,
            channel=channel,
            chat_id=chat_id,
            metadata=turn_metadata,
            on_progress=_silent,
            **run_kwargs,
        )
    except asyncio.CancelledError:
        await store.run_io(
            store.update_status,
            task_id,
            "interrupted",
            error="Scheduled work was interrupted.",
        )
        if plan_task_id:
            await store.run_io(
                store.append_event,
                plan_task_id,
                "scheduled_run.interrupted",
                {"cron_job_id": job.id, "run_task_id": task_id},
                actor="scheduler",
            )
        raise
    except Exception as exc:
        logger.exception("Scheduled Work task {} failed", task_id)
        await store.run_io(
            store.update_status,
            task_id,
            "failed",
            error=f"Scheduled work failed: {type(exc).__name__}",
        )
        if plan_task_id:
            await store.run_io(
                store.append_event,
                plan_task_id,
                "scheduled_run.failed",
                {
                    "cron_job_id": job.id,
                    "run_task_id": task_id,
                    "error_type": type(exc).__name__,
                },
                actor="scheduler",
            )
        raise
    finally:
        reset_work_context(tokens)

    # Deliver before recording terminal status, mirroring the snapshot: the
    # owner sees the digest even if the bookkeeping write below fails.
    if response is not None and deliver is not None and channel == "websocket":
        await deliver(response)

    content = response.content if response is not None else None
    await store.run_io(
        store.update_status,
        task_id,
        "succeeded",
        result_summary=(content or "")[:2000] or None,
    )
    if plan_task_id:
        await store.run_io(
            store.append_event,
            plan_task_id,
            "scheduled_run.completed",
            {"cron_job_id": job.id, "run_task_id": task_id},
            actor="scheduler",
        )
        if job.delete_after_run:
            await store.run_io(
                store.update_status,
                plan_task_id,
                "succeeded",
                result_summary=f"One-time scheduled work ran as {task_id}.",
            )
    return content
