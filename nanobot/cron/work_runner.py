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
from typing import TYPE_CHECKING, Any, Protocol

from loguru import logger

from nanobot.cron.types import CronJob
from nanobot.work.context import reset_work_context, set_work_context

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pathlib import Path

    from nanobot.work.store import WorkStore

WORK_TASK_META_TASK_ID = "work_task_id"
WORK_TASK_META_MODE = "work_mode"
WORK_TASK_META_CRON_JOB_ID = "cron_job_id"


class WorkCronAgent(Protocol):
    """The slice of ``AgentLoop`` a ``work_task`` run needs."""

    work_store: WorkStore
    workspace: Path
    model: str

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

    task = await store.run_io(
        store.create_task,
        session_key=session_key,
        chat_id=chat_id,
        content=job.payload.message,
        mode="scheduled",
        title=title,
        model=agent.model,
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
        response = await agent.process_direct(
            job.payload.message,
            session_key=session_key,
            channel=channel,
            chat_id=chat_id,
            metadata={
                WORK_TASK_META_TASK_ID: task_id,
                WORK_TASK_META_MODE: "scheduled",
                WORK_TASK_META_CRON_JOB_ID: job.id,
            },
            on_progress=_silent,
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
