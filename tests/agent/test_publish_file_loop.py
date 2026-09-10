"""End-to-end AgentLoop coverage for the private Markdown publication tool."""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import LLMResponse, ToolCallRequest


@pytest.mark.asyncio
async def test_websocket_turn_publishes_only_the_link_preserved_in_final_answer(
    tmp_path: Path,
) -> None:
    (tmp_path / "report.md").write_text("# immutable report\n", encoding="utf-8")
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    published_link: dict[str, str] = {}
    call_count = 0

    async def chat_with_retry(*, messages, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="publish-1",
                        name="publish_file",
                        arguments={"path": "report.md"},
                    )
                ],
                usage={},
            )
        tool_result = next(message["content"] for message in messages if message.get("name") == "publish_file")
        match = re.search(r"(\[report\.md\]\(/api/sessions/[^)]+\))", tool_result)
        assert match is not None
        published_link["markdown"] = match.group(1)
        return LLMResponse(content=f"Your report: {match.group(1)}", tool_calls=[], usage={})

    provider.chat_with_retry = chat_with_retry
    loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path, model="test-model")
    loop.consolidator.maybe_consolidate_by_tokens = AsyncMock(return_value=False)
    result = await loop._process_message(
        InboundMessage(
            channel="websocket",
            sender_id="owner",
            chat_id="publication-test",
            content="Make the report downloadable.",
        )
    )

    assert result is not None
    assert result.content == f"Your report: {published_link['markdown']}"
    session = loop.sessions.get_or_create("websocket:publication-test")
    grants = session.metadata["published_file_grants"]
    assert len(grants) == 1
    file_id = next(iter(grants))
    assert loop.sessions.read_published_file(session.key, file_id) == (
        "report.md",
        b"# immutable report\n",
    )


@pytest.mark.asyncio
async def test_shared_room_turn_does_not_receive_publish_file_tool(tmp_path: Path) -> None:
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    seen_definitions: list[dict] = []

    async def chat_with_retry(*, tools, **kwargs):
        seen_definitions.extend(tools)
        return LLMResponse(content="No private tools here.", tool_calls=[], usage={})

    provider.chat_with_retry = chat_with_retry
    loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path, model="test-model")
    loop.consolidator.maybe_consolidate_by_tokens = AsyncMock(return_value=False)
    result = await loop._process_message(
        InboundMessage(
            channel="websocket",
            sender_id="participant",
            chat_id="room",
            content="Can I publish a file?",
            metadata={"shared_room": True},
        )
    )

    assert result is not None
    assert seen_definitions == []
