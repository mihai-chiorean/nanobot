"""Context variables used by Work tools and runner hooks."""

from __future__ import annotations

from contextvars import ContextVar
from pathlib import Path
from typing import Any

from nanobot.work.store import WorkStore

current_work_store: ContextVar[WorkStore | None] = ContextVar(
    "current_work_store", default=None
)
current_task_id: ContextVar[str | None] = ContextVar("current_task_id", default=None)
current_step_id: ContextVar[str | None] = ContextVar("current_step_id", default=None)
current_workspace: ContextVar[Path | None] = ContextVar("current_workspace", default=None)


def set_work_context(
    *, store: WorkStore | None, task_id: str | None, workspace: Path | None
) -> list[tuple[ContextVar[Any], Any]]:
    return [
        (current_work_store, current_work_store.set(store)),
        (current_task_id, current_task_id.set(task_id)),
        (current_step_id, current_step_id.set(None)),
        (current_workspace, current_workspace.set(workspace)),
    ]


def reset_work_context(tokens: list[tuple[ContextVar[Any], Any]]) -> None:
    for variable, token in reversed(tokens):
        variable.reset(token)
