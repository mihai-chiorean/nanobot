"""Tools for publishing visible Work progress and artifacts."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.schema import StringSchema, tool_parameters_schema
from nanobot.work.context import (
    current_step_id,
    current_task_id,
    current_work_store,
    current_workspace,
)


@tool_parameters(
    tool_parameters_schema(
        title=StringSchema("Short visible progress title"),
        summary=StringSchema("Optional one-sentence summary of the current work"),
        required=["title"],
    )
)
class ReportProgressTool(Tool):
    @property
    def name(self) -> str:
        return "report_progress"

    @property
    def description(self) -> str:
        return (
            "Publish a concise visible progress step for the Work/Tasks UI. "
            "Use this before starting a meaningful phase of background work."
        )

    async def execute(self, title: str, summary: str | None = None, **kwargs: Any) -> str:
        store = current_work_store.get()
        task_id = current_task_id.get()
        if store is None or not task_id:
            return "No active Work task."
        previous = current_step_id.get()
        if previous:
            store.finish_step(task_id, previous, summary=summary or None)
        step_id = store.start_step(task_id, title)
        current_step_id.set(step_id)
        return f"Progress recorded: {title}"


@tool_parameters(
    tool_parameters_schema(
        name=StringSchema("Artifact filename, e.g. weekly-report.md"),
        kind=StringSchema(
            "Artifact kind",
            enum=["markdown", "file", "report", "patch", "command_log"],
        ),
        content=StringSchema("Artifact text content. Use for markdown/report artifacts."),
        path=StringSchema("Path to an existing local file to copy into the task artifacts."),
        summary=StringSchema("Optional short artifact summary"),
        required=["name", "kind"],
    )
)
class PublishArtifactTool(Tool):
    @property
    def name(self) -> str:
        return "publish_artifact"

    @property
    def description(self) -> str:
        return (
            "Publish a durable artifact for the Work/Tasks UI. Provide either "
            "content for text artifacts or path for an existing local file."
        )

    async def execute(
        self,
        name: str,
        kind: str,
        content: str | None = None,
        path: str | None = None,
        summary: str | None = None,
        **kwargs: Any,
    ) -> str:
        store = current_work_store.get()
        task_id = current_task_id.get()
        if store is None or not task_id:
            return "No active Work task."
        source_path: Path | None = None
        if path:
            workspace = current_workspace.get()
            if workspace is None:
                return "Error: no active workspace."
            candidate = Path(path).expanduser()
            if not candidate.is_absolute():
                candidate = workspace / candidate
            try:
                candidate = candidate.resolve(strict=True)
                candidate.relative_to(workspace.resolve(strict=True))
            except (OSError, ValueError):
                return f"Error: artifact path must be inside the workspace: {path}"
            if not candidate.is_file():
                return f"Error: artifact path not found: {path}"
            source_path = candidate
        if source_path is None and content is None:
            return "Error: provide either content or path."
        artifact = store.add_artifact(
            task_id,
            name=name,
            kind=kind,
            content=content,
            source_path=source_path,
            summary=summary,
            step_id=current_step_id.get(),
        )
        return f"Published artifact {artifact['name']} ({artifact['artifact_id']})."
