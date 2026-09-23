"""End-to-end AgentLoop coverage for the private Markdown publication tool.

MIT-1030 port from the 0.2.x ``feat/shared-rooms`` lineage. The contract that
needs teeth is the *binding lifecycle*: ``bind_publish_file_turn`` is installed
only around the runner call and must be released on every exit path — happy,
error, and cancellation — or the next turn in the same task could publish into
this conversation's slot.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.agent.tools.publish_file import current_publish_file_turn
from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import LLMResponse, LLMUsage, ToolCallRequest


def _make_loop(tmp_path: Path, provider: MagicMock) -> AgentLoop:
    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        model="test-model",
    )
    loop.auto_compact.prepare_session = MagicMock(  # type: ignore[method-assign]
        side_effect=lambda session, key: (session, None)
    )
    return loop


def _provider() -> MagicMock:
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = SimpleNamespace(max_tokens=4096)
    return provider


def _publish_turn_message(chat_id: str, content: str = "Make the report downloadable.") -> InboundMessage:
    return InboundMessage(
        channel="websocket",
        sender_id="owner",
        chat_id=chat_id,
        content=content,
    )


def _tool_call_response() -> LLMResponse:
    return LLMResponse(
        content="",
        tool_calls=[
            ToolCallRequest(
                id="publish-1",
                name="publish_file",
                arguments={"path": "report.md"},
            )
        ],
        usage=LLMUsage.reported(input_tokens=1, output_tokens=1),
    )


@pytest.mark.asyncio
async def test_websocket_turn_publishes_only_the_link_preserved_in_final_answer(
    tmp_path: Path,
) -> None:
    (tmp_path / "report.md").write_text("# immutable report\n", encoding="utf-8")
    provider = _provider()
    published_link: dict[str, str] = {}
    call_count = 0

    async def chat_stream_with_retry(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _tool_call_response()
        tool_result = next(
            message["content"]
            for message in kwargs["messages"]
            if message.get("name") == "publish_file"
        )
        match = re.search(r"(\[report\.md\]\(/api/sessions/[^)]+\))", tool_result)
        assert match is not None
        published_link["markdown"] = match.group(1)
        return LLMResponse(content=f"Your report: {match.group(1)}", tool_calls=[], usage=LLMUsage.reported(input_tokens=1, output_tokens=1))

    provider.chat_stream_with_retry = chat_stream_with_retry
    loop = _make_loop(tmp_path, provider)
    result = await loop._process_message(_publish_turn_message("publication-test"))

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
    # The binding must not survive the turn.
    assert current_publish_file_turn() is None


@pytest.mark.asyncio
async def test_publication_binding_is_released_on_the_error_path(
    tmp_path: Path,
) -> None:
    """A failing turn may never leak its publication binding into the task."""
    (tmp_path / "report.md").write_text("# broken\n", encoding="utf-8")
    provider = _provider()
    seen = 0

    async def chat_stream_with_retry(**kwargs):
        nonlocal seen
        seen += 1
        if seen == 1:
            return _tool_call_response()
        raise RuntimeError("provider exploded")

    provider.chat_stream_with_retry = chat_stream_with_retry
    loop = _make_loop(tmp_path, provider)
    # The provider failure unwinds through the run's ``finally`` — the same
    # path a cancellation takes — so the binding must be gone afterwards.
    with pytest.raises(RuntimeError):
        await loop._process_message(_publish_turn_message("error-path"))

    assert current_publish_file_turn() is None

    # A later turn on the same task context gets no inherited capability:
    # the tool refuses even though the previous conversation just published.
    async def refusing(**kwargs):
        return LLMResponse(content="second turn", tool_calls=[], usage=LLMUsage.reported(input_tokens=1, output_tokens=1))

    provider.chat_stream_with_retry = refusing
    second = await loop._process_message(_publish_turn_message("next-chat"))
    assert second is not None
    assert current_publish_file_turn() is None
    assert loop.sessions.read_published_file("websocket:next-chat", "a" * 32) is None


@pytest.mark.asyncio
async def test_publication_binding_is_released_on_cancellation(
    tmp_path: Path,
) -> None:
    """Cancellation unwinds through the same ``finally`` as success and error."""
    (tmp_path / "report.md").write_text("# cancelled\n", encoding="utf-8")
    provider = _provider()
    entered = asyncio.Event()

    async def hanging(**kwargs):
        entered.set()
        await asyncio.sleep(30)
        raise AssertionError("unreachable")

    provider.chat_stream_with_retry = hanging
    loop = _make_loop(tmp_path, provider)

    observed: dict[str, object] = {}

    async def run_turn() -> None:
        # The observer runs in the *same* context that the loop bound the
        # token in, so it can see a leaked binding if the finally is skipped.
        try:
            await loop._process_message(_publish_turn_message("cancelled-chat"))
        except asyncio.CancelledError:
            observed["binding"] = current_publish_file_turn()
            raise

    task = asyncio.create_task(run_turn())
    await asyncio.wait_for(entered.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert "binding" in observed
    assert observed["binding"] is None


@pytest.mark.asyncio
async def test_non_websocket_turn_gets_no_publication_capability(
    tmp_path: Path,
) -> None:
    (tmp_path / "report.md").write_text("# telegram\n", encoding="utf-8")
    provider = _provider()
    tool_results: list[str] = []

    async def chat_stream_with_retry(**kwargs):
        messages = kwargs["messages"]
        if any(message.get("name") == "publish_file" for message in messages):
            tool_results.append(
                next(
                    message["content"]
                    for message in messages
                    if message.get("name") == "publish_file"
                )
            )
            return LLMResponse(content="done", tool_calls=[], usage=LLMUsage.reported(input_tokens=1, output_tokens=1))
        return _tool_call_response()

    provider.chat_stream_with_retry = chat_stream_with_retry
    loop = _make_loop(tmp_path, provider)
    await loop._process_message(
        InboundMessage(
            channel="telegram",
            sender_id="owner",
            chat_id="chat-1",
            content="Publish my report.",
        )
    )

    assert tool_results == ["Error: File publication is unavailable in this conversation."]
    assert current_publish_file_turn() is None


@pytest.mark.asyncio
async def test_shared_room_turn_gets_no_publication_capability(
    tmp_path: Path,
) -> None:
    """A guest drives the room agent; it may not mint private downloads."""
    (tmp_path / "report.md").write_text("# private\n", encoding="utf-8")
    provider = _provider()
    tool_results: list[str] = []

    async def chat_stream_with_retry(**kwargs):
        messages = kwargs["messages"]
        if any(message.get("name") == "publish_file" for message in messages):
            tool_results.append(
                next(
                    message["content"]
                    for message in messages
                    if message.get("name") == "publish_file"
                )
            )
            return LLMResponse(content="done", tool_calls=[], usage=LLMUsage.reported(input_tokens=1, output_tokens=1))
        return _tool_call_response()

    provider.chat_stream_with_retry = chat_stream_with_retry
    loop = _make_loop(tmp_path, provider)
    await loop._process_message(
        InboundMessage(
            channel="websocket",
            sender_id="participant",
            chat_id="room",
            content="Can I publish a file?",
            metadata={"shared_room": True},
        )
    )

    assert tool_results == ["Error: File publication is unavailable in this conversation."]
    assert current_publish_file_turn() is None


@pytest.mark.asyncio
async def test_scheduled_work_turn_may_publish_to_its_own_session(
    tmp_path: Path,
) -> None:
    """Background Work opts in via ``work_mode`` metadata (MIT-1030 port)."""
    (tmp_path / "report.md").write_text("# scheduled report\n", encoding="utf-8")
    provider = _provider()
    link: dict[str, str] = {}

    async def chat_stream_with_retry(**kwargs):
        messages = kwargs["messages"]
        tool_result = next(
            (message["content"] for message in messages if message.get("name") == "publish_file"),
            None,
        )
        if tool_result is None:
            return _tool_call_response()
        match = re.search(r"(\[report\.md\]\(/api/sessions/[^)]+\))", tool_result)
        assert match is not None, tool_result
        link["markdown"] = match.group(1)
        return LLMResponse(content=f"Report: {match.group(1)}", tool_calls=[], usage=LLMUsage.reported(input_tokens=1, output_tokens=1))

    provider.chat_stream_with_retry = chat_stream_with_retry
    loop = _make_loop(tmp_path, provider)
    result = await loop._process_message(
        InboundMessage(
            channel="system",
            sender_id="work",
            chat_id="scheduled:job-1",
            content="Run the report job.",
            metadata={"work_mode": "scheduled", "work_task_id": "task-1"},
        )
    )

    assert result is not None
    assert link["markdown"] in (result.content or "")
    session = loop.sessions.get_or_create("scheduled:job-1")
    assert session.metadata.get("published_file_grants")
    assert current_publish_file_turn() is None


@pytest.mark.asyncio
async def test_invented_file_id_never_granted_from_model_copy(
    tmp_path: Path,
) -> None:
    """The model repeating a URL from context cannot fabricate a grant."""
    (tmp_path / "report.md").write_text("# legit\n", encoding="utf-8")
    provider = _provider()
    stolen = "b" * 32

    async def chat_stream_with_retry(**kwargs):
        messages = kwargs["messages"]
        if any(message.get("name") == "publish_file" for message in messages):
            # The final answer cites a *stolen* id the server never minted.
            stolen_url = f"/api/sessions/websocket%3Asteal/files/{stolen}"
            return LLMResponse(
                content=f"Here: [report.md]({stolen_url})",
                tool_calls=[],
                usage=LLMUsage.reported(input_tokens=1, output_tokens=1),
            )
        return _tool_call_response()

    provider.chat_stream_with_retry = chat_stream_with_retry
    loop = _make_loop(tmp_path, provider)
    await loop._process_message(_publish_turn_message("steal"))

    session = loop.sessions.get_or_create("websocket:steal")
    assert session.metadata.get("published_file_grants", {}) == {}
    assert loop.sessions.read_published_file(session.key, stolen) is None
    assert current_publish_file_turn() is None


def _seed_history(loop: AgentLoop, key: str, pairs: int) -> None:
    session = loop.sessions.get_or_create(key)
    for index in range(pairs):
        session.add_message("user", f"earlier question {index}")
        session.add_message("assistant", f"earlier answer {index}")
    loop.sessions.save(session)


async def _publish_while_rewriting_prefix(
    tmp_path: Path,
    chat_id: str,
    rewrite,
) -> tuple[AgentLoop, str]:
    """Run a publishing turn whose transcript prefix is rewritten mid-turn.

    ``rewrite`` receives the live session after the tool call and before the
    final answer, i.e. while the run is in flight.
    """
    (tmp_path / "report.md").write_text("# compacted turn\n", encoding="utf-8")
    provider = _provider()
    key = f"websocket:{chat_id}"
    link: dict[str, str] = {}
    loop = _make_loop(tmp_path, provider)
    _seed_history(loop, key, pairs=4)

    async def chat_stream_with_retry(**kwargs):
        tool_result = next(
            (m["content"] for m in kwargs["messages"] if m.get("name") == "publish_file"),
            None,
        )
        if tool_result is None:
            return _tool_call_response()
        match = re.search(r"(\[report\.md\]\(/api/sessions/[^)]+\))", tool_result)
        assert match is not None, tool_result
        link["markdown"] = match.group(1)
        rewrite(loop.sessions.get_or_create(key))
        return LLMResponse(
            content=f"Your report: {match.group(1)}",
            tool_calls=[],
            usage=LLMUsage.reported(input_tokens=1, output_tokens=1),
        )

    provider.chat_stream_with_retry = chat_stream_with_retry
    result = await loop._process_message(_publish_turn_message(chat_id))
    assert result is not None
    assert link["markdown"] in (result.content or "")
    return loop, key


def _assert_single_grant(loop: AgentLoop, key: str) -> None:
    session = loop.sessions.get_or_create(key)
    grants = session.metadata.get("published_file_grants", {})
    assert len(grants) == 1, session.messages
    file_id = next(iter(grants))
    assert loop.sessions.read_published_file(key, file_id) == (
        "report.md",
        b"# compacted turn\n",
    )
    # Provenance points at this run's final answer, not a prefix message.
    [record] = session.metadata["published_file_provenance"][file_id]
    stamped = [
        m for m in session.messages if m.get("_published_message_id") == record["message_id"]
    ]
    assert len(stamped) == 1
    assert stamped[0]["role"] == "assistant"
    assert stamped[0]["content"].startswith("Your report: ")
    assert current_publish_file_turn() is None


@pytest.mark.asyncio
async def test_grant_survives_prefix_compaction_during_publishing_turn(
    tmp_path: Path,
) -> None:
    """Compaction that drops archived prefix messages mid-turn shrinks the
    transcript below the index captured at turn start; the grant must still
    attach to this run's final answer."""

    def drop_prefix(session) -> None:
        del session.messages[:6]
        session.last_archived = 0

    loop, key = await _publish_while_rewriting_prefix(tmp_path, "compacted", drop_prefix)
    _assert_single_grant(loop, key)


@pytest.mark.asyncio
async def test_grant_survives_summary_checkpoint_inserted_before_turn(
    tmp_path: Path,
) -> None:
    """A summary checkpoint committed into the prefix mid-turn shifts every
    later index; the grant must still land exactly on this run's answer."""

    def checkpoint(session) -> None:
        session.commit_summary_checkpoint("summary of earlier turns", insert_at=4)

    loop, key = await _publish_while_rewriting_prefix(tmp_path, "checkpointed", checkpoint)
    _assert_single_grant(loop, key)
