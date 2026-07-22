from pathlib import Path

import pytest

from nanobot.agent.tools.work import PublishArtifactTool
from nanobot.work.context import reset_work_context, set_work_context
from nanobot.work.store import WorkStore


@pytest.mark.asyncio
async def test_publish_artifact_rejects_path_outside_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "private.txt"
    outside.write_text("private")
    store = WorkStore(workspace)
    task = store.create_task(
        session_key="websocket:chat-1",
        chat_id="chat-1",
        content="Publish the report",
    )
    tokens = set_work_context(store=store, task_id=task["task_id"], workspace=workspace)
    try:
        result = await PublishArtifactTool().execute(
            name="private.txt", kind="file", path=str(outside)
        )
    finally:
        reset_work_context(tokens)

    assert result.startswith("Error: artifact path must be inside the workspace")
    assert store.list_artifacts(task["task_id"]) == []


@pytest.mark.asyncio
async def test_publish_artifact_accepts_workspace_file(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    report = workspace / "report.md"
    report.write_text("# Report")
    store = WorkStore(workspace)
    task = store.create_task(
        session_key="websocket:chat-1",
        chat_id="chat-1",
        content="Publish the report",
    )
    tokens = set_work_context(store=store, task_id=task["task_id"], workspace=workspace)
    try:
        result = await PublishArtifactTool().execute(
            name="report.md", kind="markdown", path="report.md"
        )
    finally:
        reset_work_context(tokens)

    assert result.startswith("Published artifact report.md")
    assert store.list_artifacts(task["task_id"])[0]["sha256"]
