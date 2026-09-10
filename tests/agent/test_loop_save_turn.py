import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.agent.context import ContextBuilder
from nanobot.agent.loop import _MAX_PERSISTED_REASONING_CHARS, AgentLoop
from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.session.manager import Session


def _mk_loop() -> AgentLoop:
    loop = AgentLoop.__new__(AgentLoop)
    from nanobot.config.schema import AgentDefaults

    loop.max_tool_result_chars = AgentDefaults().max_tool_result_chars
    return loop


def _make_full_loop(tmp_path: Path) -> AgentLoop:
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    return AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path, model="test-model")


@pytest.mark.asyncio
@pytest.mark.parametrize("shared_room", [False, True])
async def test_reply_after_consecutive_user_messages_survives_history_reload(
    tmp_path: Path, shared_room: bool,
) -> None:
    loop = _make_full_loop(tmp_path)
    loop.consolidator.maybe_consolidate_by_tokens = AsyncMock(return_value=False)
    session = loop.sessions.get_or_create("websocket:discussion")
    session.add_message("user", "Let's discuss font safety.")
    loop.sessions.save(session)

    async def answer(initial_messages, **kwargs):
        # Use the real context builder: adjacent user messages are merged for
        # providers that reject consecutive messages with the same role.
        return (
            "Swift can make font parsing safer.", [],
            [*initial_messages, {"role": "assistant", "content": "Swift can make font parsing safer."}],
            "completed", False,
        )

    loop._run_agent_loop = answer
    await loop._process_message(InboundMessage(
        channel="websocket", sender_id="guest", chat_id="discussion",
        content="Explain why.", metadata={
            "shared_room": shared_room, "participant_display_name": "Guest",
            "client_message_id": "ask-after-discussion",
        },
    ))

    persisted = loop.sessions.read_session_file("websocket:discussion")
    assert [(message["role"], message["content"]) for message in persisted["messages"]] == [
        ("user", "Let's discuss font safety."),
        ("user", "Explain why."),
        ("assistant", "Swift can make font parsing safer."),
    ]


def test_agent_loop_scopes_audit_log_to_workspace(tmp_path: Path) -> None:
    loop = _make_full_loop(tmp_path)

    assert loop._audit_logger._log_path == tmp_path / "audit.jsonl"


def test_save_turn_skips_multimodal_user_when_only_runtime_context() -> None:
    loop = _mk_loop()
    session = Session(key="test:runtime-only")
    runtime = ContextBuilder._RUNTIME_CONTEXT_TAG + "\nCurrent Time: now (UTC)"

    loop._save_turn(
        session,
        [{"role": "user", "content": [{"type": "text", "text": runtime}]}],
        skip=0,
    )
    assert session.messages == []


def test_save_turn_keeps_image_placeholder_with_path_after_runtime_strip() -> None:
    loop = _mk_loop()
    session = Session(key="test:image")
    runtime = ContextBuilder._RUNTIME_CONTEXT_TAG + "\nCurrent Time: now (UTC)"

    loop._save_turn(
        session,
        [{
            "role": "user",
            "content": [
                {"type": "text", "text": runtime},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}, "_meta": {"path": "/media/feishu/photo.jpg"}},
            ],
        }],
        skip=0,
    )
    assert session.messages[0]["content"] == [{"type": "text", "text": "[image: /media/feishu/photo.jpg]"}]


def test_save_turn_keeps_image_placeholder_without_meta() -> None:
    loop = _mk_loop()
    session = Session(key="test:image-no-meta")
    runtime = ContextBuilder._RUNTIME_CONTEXT_TAG + "\nCurrent Time: now (UTC)"

    loop._save_turn(
        session,
        [{
            "role": "user",
            "content": [
                {"type": "text", "text": runtime},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
            ],
        }],
        skip=0,
    )
    assert session.messages[0]["content"] == [{"type": "text", "text": "[image]"}]


def test_save_turn_keeps_tool_results_under_16k() -> None:
    loop = _mk_loop()
    session = Session(key="test:tool-result")
    content = "x" * 12_000

    loop._save_turn(
        session,
        [{"role": "tool", "tool_call_id": "call_1", "name": "read_file", "content": content}],
        skip=0,
    )

    assert session.messages[0]["content"] == content


def test_save_turn_bounds_cross_turn_reasoning_history() -> None:
    loop = _mk_loop()
    session = Session(key="test:reasoning-cap")
    reasoning = "start-marker" + ("x" * _MAX_PERSISTED_REASONING_CHARS) + "end-marker"

    loop._save_turn(
        session,
        [{"role": "assistant", "content": "done", "reasoning_content": reasoning}],
        skip=0,
    )

    persisted = session.messages[0]["reasoning_content"]
    assert persisted.startswith("[Earlier reasoning omitted from persisted history.]\n")
    assert "start-marker" not in persisted
    assert persisted.endswith("end-marker")
    assert len(persisted) <= _MAX_PERSISTED_REASONING_CHARS + 64


def test_restore_runtime_checkpoint_rehydrates_completed_and_pending_tools() -> None:
    loop = _mk_loop()
    session = Session(
        key="test:checkpoint",
        metadata={
            AgentLoop._RUNTIME_CHECKPOINT_KEY: {
                "assistant_message": {
                    "role": "assistant",
                    "content": "working",
                    "tool_calls": [
                        {
                            "id": "call_done",
                            "type": "function",
                            "function": {"name": "read_file", "arguments": "{}"},
                        },
                        {
                            "id": "call_pending",
                            "type": "function",
                            "function": {"name": "exec", "arguments": "{}"},
                        },
                    ],
                },
                "completed_tool_results": [
                    {
                        "role": "tool",
                        "tool_call_id": "call_done",
                        "name": "read_file",
                        "content": "ok",
                    }
                ],
                "pending_tool_calls": [
                    {
                        "id": "call_pending",
                        "type": "function",
                        "function": {"name": "exec", "arguments": "{}"},
                    }
                ],
            }
        },
    )

    restored = loop._restore_runtime_checkpoint(session)

    assert restored is True
    assert session.metadata.get(AgentLoop._RUNTIME_CHECKPOINT_KEY) is None
    assert session.messages[0]["role"] == "assistant"
    assert session.messages[1]["tool_call_id"] == "call_done"
    assert session.messages[2]["tool_call_id"] == "call_pending"
    assert "interrupted before this tool finished" in session.messages[2]["content"].lower()


def test_restore_runtime_checkpoint_dedupes_overlapping_tail() -> None:
    loop = _mk_loop()
    session = Session(
        key="test:checkpoint-overlap",
        messages=[
            {
                "role": "assistant",
                "content": "working",
                "tool_calls": [
                    {
                        "id": "call_done",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": "{}"},
                    },
                    {
                        "id": "call_pending",
                        "type": "function",
                        "function": {"name": "exec", "arguments": "{}"},
                    },
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_done",
                "name": "read_file",
                "content": "ok",
            },
        ],
        metadata={
            AgentLoop._RUNTIME_CHECKPOINT_KEY: {
                "assistant_message": {
                    "role": "assistant",
                    "content": "working",
                    "tool_calls": [
                        {
                            "id": "call_done",
                            "type": "function",
                            "function": {"name": "read_file", "arguments": "{}"},
                        },
                        {
                            "id": "call_pending",
                            "type": "function",
                            "function": {"name": "exec", "arguments": "{}"},
                        },
                    ],
                },
                "completed_tool_results": [
                    {
                        "role": "tool",
                        "tool_call_id": "call_done",
                        "name": "read_file",
                        "content": "ok",
                    }
                ],
                "pending_tool_calls": [
                    {
                        "id": "call_pending",
                        "type": "function",
                        "function": {"name": "exec", "arguments": "{}"},
                    }
                ],
            }
        },
    )

    restored = loop._restore_runtime_checkpoint(session)

    assert restored is True
    assert session.metadata.get(AgentLoop._RUNTIME_CHECKPOINT_KEY) is None
    assert len(session.messages) == 3
    assert session.messages[0]["role"] == "assistant"
    assert session.messages[1]["tool_call_id"] == "call_done"
    assert session.messages[2]["tool_call_id"] == "call_pending"


@pytest.mark.asyncio
async def test_process_message_persists_user_message_before_turn_completes(tmp_path: Path) -> None:
    loop = _make_full_loop(tmp_path)
    loop.consolidator.maybe_consolidate_by_tokens = AsyncMock(return_value=False)  # type: ignore[method-assign]
    loop._run_agent_loop = AsyncMock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]

    msg = InboundMessage(channel="feishu", sender_id="u1", chat_id="c1", content="persist me")
    with pytest.raises(RuntimeError, match="boom"):
        await loop._process_message(msg)

    loop.sessions.invalidate("feishu:c1")
    persisted = loop.sessions.get_or_create("feishu:c1")
    assert [m["role"] for m in persisted.messages] == ["user"]
    assert persisted.messages[0]["content"] == "persist me"
    assert persisted.metadata.get(AgentLoop._PENDING_USER_TURN_KEY) is True
    assert persisted.updated_at >= persisted.created_at


@pytest.mark.asyncio
async def test_process_message_retries_pending_client_message_after_crash(
    tmp_path: Path,
) -> None:
    loop = _make_full_loop(tmp_path)
    loop.consolidator.maybe_consolidate_by_tokens = AsyncMock(return_value=False)  # type: ignore[method-assign]
    loop._run_agent_loop = AsyncMock(side_effect=RuntimeError("interrupt"))  # type: ignore[method-assign]
    client_message_id = "c5e597cc-1adb-4aa8-bde0-ac0edb92c5f5"
    msg = InboundMessage(
        channel="websocket",
        sender_id="u1",
        chat_id="dedupe",
        content="persist once",
        metadata={"client_message_id": client_message_id},
    )
    await loop.chat_inbox.accept(msg, client_message_id)
    await loop.chat_inbox.mark_enqueued(msg.chat_id, client_message_id)

    with pytest.raises(RuntimeError, match="interrupt"):
        await loop._process_message(msg)

    persisted = loop.sessions.get_or_create("websocket:dedupe")
    assert persisted.messages[0]["client_message_id"] == client_message_id
    assert persisted.metadata[AgentLoop._PENDING_USER_TURN_KEY] == {
        "client_message_ids": [client_message_id]
    }
    assert [record.client_message_id for record in await loop.chat_inbox.recoverable()] == [
        client_message_id
    ]

    async def complete(initial_messages, **_kwargs):
        return (
            "done",
            [],
            [*initial_messages, {"role": "assistant", "content": "done"}],
            "completed",
            False,
        )

    loop._run_agent_loop = complete  # type: ignore[method-assign]
    result = await loop._process_message(msg)

    assert result is not None
    assert result.content == "done"
    loop.sessions.invalidate("websocket:dedupe")
    completed = loop.sessions.get_or_create("websocket:dedupe")
    assert [
        (message["role"], message["content"])
        for message in completed.messages
    ] == [
        ("user", "persist once"),
        ("assistant", "done"),
    ]
    assert await loop.chat_inbox.recoverable() == []


@pytest.mark.asyncio
async def test_checkpointed_tool_turn_is_not_replayed_after_crash(
    tmp_path: Path,
) -> None:
    loop = _make_full_loop(tmp_path)
    loop.consolidator.maybe_consolidate_by_tokens = AsyncMock(return_value=False)  # type: ignore[method-assign]
    client_message_id = "c5e597cc-1adb-4aa8-bde0-ac0edb92c5f5"
    msg = InboundMessage(
        channel="websocket",
        sender_id="u1",
        chat_id="tool-crash",
        content="change something",
        metadata={"client_message_id": client_message_id},
    )
    await loop.chat_inbox.accept(msg, client_message_id)
    await loop.chat_inbox.mark_enqueued(msg.chat_id, client_message_id)

    async def crash_after_checkpoint(_initial_messages, *, session, **_kwargs):
        loop._set_runtime_checkpoint(
            session,
            {
                "assistant_message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "exec", "arguments": "{}"},
                    }],
                },
                "completed_tool_results": [],
                "pending_tool_calls": [{
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "exec", "arguments": "{}"},
                }],
            },
        )
        raise RuntimeError("crash")

    loop._run_agent_loop = crash_after_checkpoint  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="crash"):
        await loop._process_message(msg)

    loop._run_agent_loop = AsyncMock()  # type: ignore[method-assign]
    result = await loop._process_message(msg)

    assert result is not None
    assert "did not repeat" in result.content
    loop._run_agent_loop.assert_not_awaited()
    assert await loop.chat_inbox.recoverable() == []


@pytest.mark.asyncio
async def test_command_receipt_is_completed_before_non_idempotent_dispatch(
    tmp_path: Path,
) -> None:
    loop = _make_full_loop(tmp_path)
    client_message_id = "c5e597cc-1adb-4aa8-bde0-ac0edb92c5f5"
    msg = InboundMessage(
        channel="websocket",
        sender_id="u1",
        chat_id="command-crash",
        content="/dream-restore deadbeef",
        metadata={"client_message_id": client_message_id},
    )
    await loop.chat_inbox.accept(msg, client_message_id)
    await loop.chat_inbox.mark_enqueued(msg.chat_id, client_message_id)

    async def crash_during_command(_ctx):
        raise RuntimeError("command crashed")

    await loop._dispatch_command_inline(
        msg,
        msg.session_key,
        msg.content,
        crash_during_command,
    )

    assert await loop.chat_inbox.recoverable() == []
    response = await loop.bus.consume_outbound()
    assert response.content == loop._command_failure_message()
    loop.sessions.invalidate(msg.session_key)
    session = loop.sessions.get_or_create(msg.session_key)
    assert session.messages[-1]["content"] == loop._command_failure_message()


@pytest.mark.asyncio
async def test_interrupted_command_is_not_replayed_and_is_visible_after_restart(
    tmp_path: Path,
) -> None:
    loop = _make_full_loop(tmp_path)
    client_message_id = "c5e597cc-1adb-4aa8-bde0-ac0edb92c5f5"
    msg = InboundMessage(
        channel="websocket",
        sender_id="u1",
        chat_id="command-crash",
        content="/dream-restore deadbeef",
        metadata={"client_message_id": client_message_id},
    )
    await loop.chat_inbox.accept(msg, client_message_id)
    await loop.chat_inbox.mark_enqueued(msg.chat_id, client_message_id)
    assert await loop.chat_inbox.mark_command_started(
        msg.chat_id,
        client_message_id,
    )

    restarted = _make_full_loop(tmp_path)
    await restarted._recover_interrupted_commands()
    await restarted._recover_interrupted_commands()

    restarted.sessions.invalidate(msg.session_key)
    session = restarted.sessions.get_or_create(msg.session_key)
    assert [(item["role"], item["content"]) for item in session.messages] == [
        ("user", msg.content),
        ("assistant", restarted._command_failure_message()),
    ]
    assert await restarted.chat_inbox.recoverable() == []
    assert await restarted.chat_inbox.interrupted_commands() == []


@pytest.mark.asyncio
async def test_command_does_not_run_when_started_marker_cannot_be_persisted(
    tmp_path: Path,
) -> None:
    loop = _make_full_loop(tmp_path)
    msg = InboundMessage(
        channel="websocket",
        sender_id="u1",
        chat_id="command-no-marker",
        content="/dream-restore deadbeef",
        metadata={"client_message_id": "c5e597cc-1adb-4aa8-bde0-ac0edb92c5f5"},
    )
    dispatch = AsyncMock()
    loop.chat_inbox.mark_command_started = AsyncMock(  # type: ignore[method-assign]
        side_effect=OSError("disk unavailable")
    )

    await loop._dispatch_command_inline(
        msg,
        msg.session_key,
        msg.content,
        dispatch,
    )

    dispatch.assert_not_awaited()
    response = await loop.bus.consume_outbound()
    assert response.content == loop._command_not_started_message()


@pytest.mark.asyncio
async def test_cancelled_command_persists_uncertain_outcome_immediately(
    tmp_path: Path,
) -> None:
    loop = _make_full_loop(tmp_path)
    client_message_id = "c5e597cc-1adb-4aa8-bde0-ac0edb92c5f5"
    msg = InboundMessage(
        channel="websocket",
        sender_id="u1",
        chat_id="command-cancelled",
        content="/dream-restore deadbeef",
        metadata={
            "client_message_id": client_message_id,
            "_command_at_most_once": True,
        },
    )
    await loop.chat_inbox.accept(msg, client_message_id)
    await loop.chat_inbox.mark_enqueued(msg.chat_id, client_message_id)
    assert await loop.chat_inbox.mark_command_started(
        msg.chat_id,
        client_message_id,
    )
    loop._process_message = AsyncMock(  # type: ignore[method-assign]
        side_effect=asyncio.CancelledError
    )

    with pytest.raises(asyncio.CancelledError):
        await loop._dispatch(msg)

    loop.sessions.invalidate(msg.session_key)
    session = loop.sessions.get_or_create(msg.session_key)
    assert session.messages[-1]["content"] == loop._command_failure_message()
    assert await loop.chat_inbox.interrupted_commands() == []


@pytest.mark.asyncio
async def test_command_completion_failure_returns_and_recovers_known_result(
    tmp_path: Path,
) -> None:
    loop = _make_full_loop(tmp_path)
    client_message_id = "c5e597cc-1adb-4aa8-bde0-ac0edb92c5f5"
    msg = InboundMessage(
        channel="websocket",
        sender_id="u1",
        chat_id="command-completion",
        content="/status",
        metadata={"client_message_id": client_message_id},
    )
    await loop.chat_inbox.accept(msg, client_message_id)
    await loop.chat_inbox.mark_enqueued(msg.chat_id, client_message_id)
    original_mark_processed = loop.chat_inbox.mark_processed
    loop.chat_inbox.mark_processed = AsyncMock(  # type: ignore[method-assign]
        side_effect=OSError("disk unavailable")
    )
    result = OutboundMessage(
        channel="websocket",
        chat_id=msg.chat_id,
        content="known result",
    )

    await loop._dispatch_command_inline(
        msg,
        msg.session_key,
        msg.content,
        AsyncMock(return_value=result),
    )

    response = await loop.bus.consume_outbound()
    assert response.content == "known result"
    loop.sessions.invalidate(msg.session_key)
    session = loop.sessions.get_or_create(msg.session_key)
    assert session.messages[-1]["content"] == "known result"

    loop.chat_inbox.mark_processed = original_mark_processed  # type: ignore[method-assign]
    restarted = _make_full_loop(tmp_path)
    await restarted._recover_interrupted_commands()
    restarted.sessions.invalidate(msg.session_key)
    recovered = restarted.sessions.get_or_create(msg.session_key)
    assert [item["content"] for item in recovered.messages] == [
        msg.content,
        "known result",
    ]
    assert await restarted.chat_inbox.interrupted_commands() == []


@pytest.mark.asyncio
async def test_dispatch_retries_in_place_before_republishing_later_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = _make_full_loop(tmp_path)
    first_id = "c5e597cc-1adb-4aa8-bde0-ac0edb92c5f5"
    first = InboundMessage(
        channel="websocket",
        sender_id="u1",
        chat_id="ordered",
        content="first",
        metadata={"client_message_id": first_id},
    )
    second = InboundMessage(
        channel="websocket",
        sender_id="u1",
        chat_id="ordered",
        content="second",
        metadata={"client_message_id": "06eab632-f2ec-48f2-901b-0aa0a7f2a4c7"},
    )
    await loop.chat_inbox.accept(first, first_id)
    await loop.chat_inbox.mark_enqueued(first.chat_id, first_id)
    loop._process_message = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            RuntimeError("transient"),
            OutboundMessage(
                channel="websocket",
                chat_id="ordered",
                content="first done",
            ),
        ]
    )

    async def no_wait(_delay):
        loop._pending_queues[first.session_key].put_nowait(second)

    monkeypatch.setattr("nanobot.agent.loop.asyncio.sleep", no_wait)

    await loop._dispatch(first)

    assert loop._process_message.await_count == 2
    outbound = await loop.bus.consume_outbound()
    assert outbound.content == "Retrying in 2 seconds."
    completed = await loop.bus.consume_outbound()
    assert completed.content == "first done"
    assert await loop.bus.consume_inbound() == second


@pytest.mark.asyncio
async def test_permanent_processing_failure_is_closed_after_retry_limit(
    tmp_path: Path,
) -> None:
    loop = _make_full_loop(tmp_path)
    client_message_id = "c5e597cc-1adb-4aa8-bde0-ac0edb92c5f5"
    msg = InboundMessage(
        channel="websocket",
        sender_id="u1",
        chat_id="terminal",
        content="never succeeds",
        metadata={"client_message_id": client_message_id},
    )
    await loop.chat_inbox.accept(msg, client_message_id)
    await loop.chat_inbox.mark_enqueued(msg.chat_id, client_message_id)
    session = loop.sessions.get_or_create(msg.session_key)
    session.add_message(
        "user",
        msg.content,
        client_message_id=client_message_id,
    )
    loop._mark_pending_user_turn(session, [client_message_id])
    loop.sessions.save(session)

    for attempt in range(1, loop._MAX_CHAT_PROCESSING_RETRIES + 1):
        result = await loop._prepare_chat_message_retry(msg)
        if attempt < loop._MAX_CHAT_PROCESSING_RETRIES:
            assert result is not None and result > 0
            assert await loop.chat_inbox.claim_retry_for_enqueue(
                msg.chat_id,
                client_message_id,
            )
        else:
            assert result == loop._TERMINAL_CHAT_RETRY

    assert await loop.chat_inbox.recoverable() == []
    loop.sessions.invalidate(msg.session_key)
    completed = loop.sessions.get_or_create(msg.session_key)
    assert completed.messages[-1]["content"] == loop._terminal_chat_failure_message()
    assert AgentLoop._PENDING_USER_TURN_KEY not in completed.metadata


def test_save_turn_preserves_merged_client_message_ids() -> None:
    loop = _mk_loop()
    session = Session(key="websocket:merged")
    client_message_ids = [
        "7fbf82b5-37de-4df0-b2bb-749bb6fd2306",
        "06eab632-f2ec-48f2-901b-0aa0a7f2a4c7",
    ]

    loop._save_turn(
        session,
        [{
            "role": "user",
            "content": "first\n\nsecond",
            "_client_message_ids": client_message_ids,
        }],
        skip=0,
    )

    assert session.messages[0]["client_message_ids"] == client_message_ids
    assert "_client_message_ids" not in session.messages[0]


# 1x1 PNG used by the media-persistence tests. ``extract_documents`` runs
# at the top of ``_process_message`` and filters ``msg.media`` down to
# paths that magic-byte-sniff as images, so the test fixture needs real
# bytes on disk (not just placeholder paths).
_PNG_1X1 = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\nIDATx\x9cc\x00\x00\x00\x02\x00\x01"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)


@pytest.mark.asyncio
async def test_process_message_persists_media_paths_on_user_turn(tmp_path: Path) -> None:
    """User turns that attach images must record the media paths alongside
    the text so the webui can rehydrate previews on session replay.

    This is the producer half of the signed-media-URL round-trip: paths are
    stored here, then :meth:`WebSocketChannel._augment_media_urls` maps them
    onto signed URLs on the way out.
    """
    img_a = tmp_path / "uuid-1.png"
    img_a.write_bytes(_PNG_1X1)
    img_b = tmp_path / "uuid-2.png"
    img_b.write_bytes(_PNG_1X1)

    loop = _make_full_loop(tmp_path)
    loop.consolidator.maybe_consolidate_by_tokens = AsyncMock(return_value=False)  # type: ignore[method-assign]
    loop._run_agent_loop = AsyncMock(side_effect=RuntimeError("interrupt"))  # type: ignore[method-assign]

    msg = InboundMessage(
        channel="websocket",
        sender_id="u1",
        chat_id="c-media",
        content="look",
        media=[str(img_a), str(img_b)],
    )
    with pytest.raises(RuntimeError, match="interrupt"):
        await loop._process_message(msg)

    loop.sessions.invalidate("websocket:c-media")
    persisted = loop.sessions.get_or_create("websocket:c-media")
    assert [m["role"] for m in persisted.messages] == ["user"]
    assert persisted.messages[0]["content"] == "look"
    assert persisted.messages[0]["media"] == [str(img_a), str(img_b)]


@pytest.mark.asyncio
async def test_process_message_persists_media_only_turn_without_text(tmp_path: Path) -> None:
    """A turn with images but no text still persists (previously silent-dropped).

    The old early-persist gate skipped messages without text, leaving pure
    image turns un-checkpointed. They now materialise as an empty-content
    user row with ``media`` attached.
    """
    img = tmp_path / "only.png"
    img.write_bytes(_PNG_1X1)

    loop = _make_full_loop(tmp_path)
    loop.consolidator.maybe_consolidate_by_tokens = AsyncMock(return_value=False)  # type: ignore[method-assign]
    loop._run_agent_loop = AsyncMock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]

    msg = InboundMessage(
        channel="websocket",
        sender_id="u1",
        chat_id="c-images-only",
        content="",
        media=[str(img)],
    )
    with pytest.raises(RuntimeError):
        await loop._process_message(msg)

    loop.sessions.invalidate("websocket:c-images-only")
    persisted = loop.sessions.get_or_create("websocket:c-images-only")
    assert len(persisted.messages) == 1
    assert persisted.messages[0]["role"] == "user"
    assert persisted.messages[0]["content"] == ""
    assert persisted.messages[0]["media"] == [str(img)]


@pytest.mark.asyncio
async def test_process_message_does_not_duplicate_early_persisted_user_message(tmp_path: Path) -> None:
    loop = _make_full_loop(tmp_path)
    loop.consolidator.maybe_consolidate_by_tokens = AsyncMock(return_value=False)  # type: ignore[method-assign]
    loop._run_agent_loop = AsyncMock(return_value=(
        "done",
        None,
        [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "done"},
        ],
        "stop",
        False,
    ))  # type: ignore[method-assign]

    result = await loop._process_message(
        InboundMessage(channel="feishu", sender_id="u1", chat_id="c2", content="hello")
    )

    assert result is not None
    assert result.content == "done"
    session = loop.sessions.get_or_create("feishu:c2")
    assert [
        {k: v for k, v in m.items() if k in {"role", "content"}}
        for m in session.messages
    ] == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "done"},
    ]
    assert AgentLoop._PENDING_USER_TURN_KEY not in session.metadata


@pytest.mark.asyncio
async def test_process_message_uses_context_chat_id_for_runtime_prompt(tmp_path: Path) -> None:
    loop = _make_full_loop(tmp_path)
    loop.consolidator.maybe_consolidate_by_tokens = AsyncMock(return_value=False)  # type: ignore[method-assign]
    loop.context.build_messages = MagicMock(  # type: ignore[method-assign]
        return_value=[
            {"role": "system", "content": "system"},
            {"role": "user", "content": "runtime + hello"},
        ]
    )
    loop._run_agent_loop = AsyncMock(return_value=(  # type: ignore[method-assign]
        "done",
        [],
        [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "runtime + hello"},
            {"role": "assistant", "content": "done"},
        ],
        "stop",
        False,
    ))

    result = await loop._process_message(
        InboundMessage(
            channel="discord",
            sender_id="u1",
            chat_id="thread-777",
            content="hello",
            metadata={"context_chat_id": "parent-456"},
            session_key_override="discord:parent-456:thread:thread-777",
        )
    )

    assert result is not None
    assert result.chat_id == "thread-777"
    assert loop.context.build_messages.call_args.kwargs["chat_id"] == "parent-456"
    assert loop._run_agent_loop.call_args.kwargs["chat_id"] == "thread-777"


def test_set_tool_context_uses_effective_key_for_spawn_tool(tmp_path: Path) -> None:
    loop = _make_full_loop(tmp_path)
    spawn_tool = loop.tools.get("spawn")
    assert spawn_tool is not None

    loop._set_tool_context(
        "discord",
        "thread-777",
        session_key="discord:parent-456:thread:thread-777",
    )

    assert spawn_tool._origin_channel.get() == "discord"  # type: ignore[attr-defined]
    assert spawn_tool._origin_chat_id.get() == "thread-777"  # type: ignore[attr-defined]
    assert spawn_tool._session_key.get() == "discord:parent-456:thread:thread-777"  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_next_turn_after_crash_closes_pending_user_turn_before_new_input(tmp_path: Path) -> None:
    loop = _make_full_loop(tmp_path)
    loop.consolidator.maybe_consolidate_by_tokens = AsyncMock(return_value=False)  # type: ignore[method-assign]
    loop.provider.chat_with_retry = AsyncMock(return_value=MagicMock())  # unused because _run_agent_loop is stubbed

    session = loop.sessions.get_or_create("feishu:c3")
    session.add_message("user", "old question")
    session.metadata[AgentLoop._PENDING_USER_TURN_KEY] = True
    loop.sessions.save(session)

    loop._run_agent_loop = AsyncMock(return_value=(
        "new answer",
        None,
        [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "old question"},
            {"role": "assistant", "content": "Error: Task interrupted before a response was generated."},
            {"role": "user", "content": "new question"},
            {"role": "assistant", "content": "new answer"},
        ],
        "stop",
        False,
    ))  # type: ignore[method-assign]

    result = await loop._process_message(
        InboundMessage(channel="feishu", sender_id="u1", chat_id="c3", content="new question")
    )

    assert result is not None
    assert result.content == "new answer"
    session = loop.sessions.get_or_create("feishu:c3")
    assert [
        {k: v for k, v in m.items() if k in {"role", "content"}}
        for m in session.messages
    ] == [
        {"role": "user", "content": "old question"},
        {"role": "assistant", "content": "Error: Task interrupted before a response was generated."},
        {"role": "user", "content": "new question"},
        {"role": "assistant", "content": "new answer"},
    ]
    assert AgentLoop._PENDING_USER_TURN_KEY not in session.metadata


@pytest.mark.asyncio
async def test_stop_preserves_runtime_checkpoint_for_next_turn(tmp_path: Path) -> None:
    from nanobot.command.builtin import cmd_stop
    from nanobot.command.router import CommandContext

    loop = _make_full_loop(tmp_path)
    loop.consolidator.maybe_consolidate_by_tokens = AsyncMock(return_value=False)  # type: ignore[method-assign]

    checkpoint_saved = asyncio.Event()

    async def interrupted_run_agent_loop(_initial_messages, *, session=None, **_kwargs):
        assert session is not None
        loop._set_runtime_checkpoint(
            session,
            {
                "assistant_message": {
                    "role": "assistant",
                    "content": "working",
                    "tool_calls": [
                        {
                            "id": "call_done",
                            "type": "function",
                            "function": {"name": "read_file", "arguments": "{}"},
                        },
                        {
                            "id": "call_pending",
                            "type": "function",
                            "function": {"name": "exec", "arguments": "{}"},
                        },
                    ],
                },
                "completed_tool_results": [
                    {
                        "role": "tool",
                        "tool_call_id": "call_done",
                        "name": "read_file",
                        "content": "ok",
                    }
                ],
                "pending_tool_calls": [
                    {
                        "id": "call_pending",
                        "type": "function",
                        "function": {"name": "exec", "arguments": "{}"},
                    }
                ],
            },
        )
        checkpoint_saved.set()
        await asyncio.Event().wait()

    loop._run_agent_loop = interrupted_run_agent_loop  # type: ignore[method-assign]

    first_msg = InboundMessage(channel="feishu", sender_id="u1", chat_id="c4", content="keep progress")
    task = asyncio.create_task(loop._process_message(first_msg))
    loop._active_tasks[first_msg.session_key] = [task]
    await asyncio.wait_for(checkpoint_saved.wait(), timeout=1.0)

    stop_msg = InboundMessage(channel="feishu", sender_id="u1", chat_id="c4", content="/stop")
    stop_ctx = CommandContext(msg=stop_msg, session=None, key=stop_msg.session_key, raw="/stop", loop=loop)
    stop_result = await cmd_stop(stop_ctx)

    assert "Stopped 1 task" in stop_result.content
    assert task.done()

    loop.sessions.invalidate("feishu:c4")
    interrupted = loop.sessions.get_or_create("feishu:c4")
    assert interrupted.metadata.get(AgentLoop._PENDING_USER_TURN_KEY) is True
    assert interrupted.metadata.get(AgentLoop._RUNTIME_CHECKPOINT_KEY) is not None

    async def resumed_run_agent_loop(initial_messages, **_kwargs):
        return (
            "next answer",
            None,
            [*initial_messages, {"role": "assistant", "content": "next answer"}],
            "stop",
            False,
        )

    loop._run_agent_loop = resumed_run_agent_loop  # type: ignore[method-assign]
    result = await loop._process_message(
        InboundMessage(channel="feishu", sender_id="u1", chat_id="c4", content="continue here")
    )

    assert result is not None
    assert result.content == "next answer"

    session = loop.sessions.get_or_create("feishu:c4")
    assert [
        {k: v for k, v in m.items() if k in {"role", "content", "tool_call_id", "name"}}
        for m in session.messages
    ] == [
        {"role": "user", "content": "keep progress"},
        {"role": "assistant", "content": "working"},
        {"role": "tool", "tool_call_id": "call_done", "name": "read_file", "content": "ok"},
        {
            "role": "tool",
            "tool_call_id": "call_pending",
            "name": "exec",
            "content": "Error: Task interrupted before this tool finished.",
        },
        {"role": "user", "content": "continue here"},
        {"role": "assistant", "content": "next answer"},
    ]
    assert AgentLoop._PENDING_USER_TURN_KEY not in session.metadata
    assert AgentLoop._RUNTIME_CHECKPOINT_KEY not in session.metadata


@pytest.mark.asyncio
async def test_system_subagent_followup_is_persisted_before_prompt_assembly(tmp_path: Path) -> None:
    loop = _make_full_loop(tmp_path)
    loop.consolidator.maybe_consolidate_by_tokens = AsyncMock(return_value=False)  # type: ignore[method-assign]

    session = loop.sessions.get_or_create("cli:test")
    session.add_message("user", "question")
    session.add_message("assistant", "working")
    loop.sessions.save(session)

    seen: dict[str, list[dict]] = {}

    async def fake_run_agent_loop(initial_messages, **_kwargs):
        seen["initial_messages"] = initial_messages
        return (
            "done",
            [],
            [*initial_messages, {"role": "assistant", "content": "done"}],
            "stop",
            False,
        )

    loop._run_agent_loop = fake_run_agent_loop  # type: ignore[method-assign]

    await loop._process_message(
        InboundMessage(
            channel="system",
            sender_id="subagent",
            chat_id="cli:test",
            content="subagent result",
            metadata={"subagent_task_id": "sub-1"},
        )
    )

    non_system = [m for m in seen["initial_messages"] if m.get("role") != "system"]
    assert "question" in non_system[0]["content"]
    assert "working" in non_system[1]["content"]
    # User turns carry the timestamp prefix so the model can reason about
    # relative time. Assistant turns do NOT, otherwise the model treats those
    # past replies as in-context examples and starts its own outputs with
    # ``[Message Time: ...]`` (which then leaks back to the user).
    assert "[Message Time:" in non_system[0]["content"]
    assert "[Message Time:" not in non_system[1]["content"]
    assert non_system[2]["content"].count("subagent result") == 1
    assert "Current Time:" in non_system[2]["content"]

    loop.sessions.invalidate("cli:test")
    persisted = loop.sessions.get_or_create("cli:test")
    assert [
        {k: v for k, v in m.items() if k in {"role", "content", "injected_event", "subagent_task_id"}}
        for m in persisted.messages
    ] == [
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "working"},
        {
            "role": "assistant",
            "content": "subagent result",
            "injected_event": "subagent_result",
            "subagent_task_id": "sub-1",
        },
        {"role": "assistant", "content": "done"},
    ]


@pytest.mark.asyncio
async def test_multiple_subagent_followups_all_persist_as_standalone_history(tmp_path: Path) -> None:
    loop = _make_full_loop(tmp_path)
    loop.consolidator.maybe_consolidate_by_tokens = AsyncMock(return_value=False)  # type: ignore[method-assign]

    async def fake_run_agent_loop(initial_messages, **_kwargs):
        return (
            "ack",
            [],
            [*initial_messages, {"role": "assistant", "content": "ack"}],
            "stop",
            False,
        )

    loop._run_agent_loop = fake_run_agent_loop  # type: ignore[method-assign]

    for idx in range(3):
        await loop._process_message(
            InboundMessage(
                channel="system",
                sender_id="subagent",
                chat_id="cli:multi",
                content=f"subagent result {idx}",
                metadata={"subagent_task_id": f"sub-{idx}"},
            )
        )

    loop.sessions.invalidate("cli:multi")
    persisted = loop.sessions.get_or_create("cli:multi")
    followups = [m for m in persisted.messages if m.get("injected_event") == "subagent_result"]
    assert [m["content"] for m in followups] == [
        "subagent result 0",
        "subagent result 1",
        "subagent result 2",
    ]


def test_prompt_merge_does_not_replace_standalone_subagent_history_entry(tmp_path: Path) -> None:
    loop = _mk_loop()
    session = Session(key="cli:merge")
    session.add_message("assistant", "previous assistant")

    inserted = loop._persist_subagent_followup(
        session,
        InboundMessage(
            channel="system",
            sender_id="subagent",
            chat_id="cli:merge",
            content="subagent result",
            metadata={"subagent_task_id": "sub-1"},
        ),
    )

    assert inserted is True

    builder = ContextBuilder(tmp_path)
    projected = builder.build_messages(
        history=session.get_history(max_messages=0),
        current_message="",
        current_role="assistant",
        channel="cli",
        chat_id="merge",
    )

    non_system = [m for m in projected if m.get("role") != "system"]
    assert len(non_system) == 2
    assert "subagent result" in non_system[-1]["content"]
    assert session.messages[-1]["content"] == "subagent result"
    assert session.messages[-1]["injected_event"] == "subagent_result"


def test_subagent_followup_dedupes_by_task_id() -> None:
    loop = _mk_loop()
    session = Session(key="cli:dedupe")
    msg = InboundMessage(
        channel="system",
        sender_id="subagent",
        chat_id="cli:dedupe",
        content="subagent result",
        metadata={"subagent_task_id": "sub-1"},
    )

    assert loop._persist_subagent_followup(session, msg) is True
    assert loop._persist_subagent_followup(session, msg) is False
    assert len(session.messages) == 1


def test_subagent_followup_skips_empty_content() -> None:
    loop = _mk_loop()
    session = Session(key="cli:empty")
    msg = InboundMessage(
        channel="system",
        sender_id="subagent",
        chat_id="cli:empty",
        content="",
        metadata={"subagent_task_id": "sub-empty"},
    )

    assert loop._persist_subagent_followup(session, msg) is False
    assert session.messages == []


def test_set_tool_context_passes_thread_session_key_to_spawn(tmp_path: Path) -> None:
    loop = _make_full_loop(tmp_path)

    loop._set_tool_context(
        "slack",
        "C123",
        message_id="msg-123",
        metadata={"slack": {"thread_ts": "1700.42", "channel_type": "channel"}},
        session_key="slack:C123:1700.42",
    )

    spawn_tool = loop.tools.get("spawn")
    assert spawn_tool is not None
    assert spawn_tool._session_key.get() == "slack:C123:1700.42"
    assert spawn_tool._origin_message_id.get() == "msg-123"


@pytest.mark.asyncio
async def test_system_subagent_followup_uses_thread_session_and_slack_metadata(tmp_path: Path) -> None:
    loop = _make_full_loop(tmp_path)
    loop.consolidator.maybe_consolidate_by_tokens = AsyncMock(return_value=False)  # type: ignore[method-assign]

    thread_session = loop.sessions.get_or_create("slack:C123:1700.42")
    thread_session.add_message("user", "thread question")
    loop.sessions.save(thread_session)

    seen: dict[str, list[dict]] = {}

    async def fake_run_agent_loop(initial_messages, **_kwargs):
        seen["initial_messages"] = initial_messages
        return (
            "done",
            [],
            [*initial_messages, {"role": "assistant", "content": "done"}],
            "stop",
            False,
        )

    loop._run_agent_loop = fake_run_agent_loop  # type: ignore[method-assign]

    outbound = await loop._process_message(
        InboundMessage(
            channel="system",
            sender_id="subagent",
            chat_id="slack:C123",
            content="subagent result",
            session_key_override="slack:C123:1700.42",
            metadata={"subagent_task_id": "sub-1", "origin_message_id": "msg-123"},
        )
    )

    assert outbound is not None
    assert outbound.channel == "slack"
    assert outbound.chat_id == "C123"
    assert outbound.metadata == {
        "slack": {"thread_ts": "1700.42"},
        "origin_message_id": "msg-123",
    }
    assert "thread question" in seen["initial_messages"][1]["content"]

    loop.sessions.invalidate("slack:C123:1700.42")
    persisted = loop.sessions.get_or_create("slack:C123:1700.42")
    assert any(m.get("subagent_task_id") == "sub-1" for m in persisted.messages)
