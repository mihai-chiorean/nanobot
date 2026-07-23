from pathlib import Path
from unittest.mock import MagicMock

import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.agent.tools.recall import IngestTool
from nanobot.bus.queue import MessageBus
from nanobot.config.schema import RAGToolsConfig, ToolsConfig


def _loop(tmp_path: Path, tools_config: ToolsConfig | None = None) -> AgentLoop:
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    return AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        model="test-model",
        tools_config=tools_config,
    )


def test_rag_tools_are_disabled_by_default(tmp_path: Path) -> None:
    loop = _loop(tmp_path)

    assert "recall" not in loop.tools.tool_names
    assert "ingest" not in loop.tools.tool_names


def test_rag_tools_can_be_enabled_explicitly(tmp_path: Path) -> None:
    config = ToolsConfig(rag=RAGToolsConfig(enable=True))
    loop = _loop(tmp_path, config)

    assert "recall" in loop.tools.tool_names
    assert "ingest" in loop.tools.tool_names


@pytest.mark.asyncio
async def test_ingest_rejects_paths_outside_workspace_before_opening_rag(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("private", encoding="utf-8")
    tool = IngestTool(workspace=workspace, allowed_dir=workspace)
    tool._get_rag = MagicMock(side_effect=AssertionError("RAG must stay unopened"))

    result = await tool.execute(path=str(outside))

    assert result.startswith("Error: path")
    assert "outside the allowed directory" in result
    tool._get_rag.assert_not_called()
