"""Chat-turn -> Work hand-off (MIT-1855 / OA-12).

Covers the three guarantees of ``docs/design/onboarding-and-approval-tuning.md``
§4: the hand-off fires at a tool boundary *before* the next batch executes, a
turn that has written (or has an approval outstanding) is never handed off,
and both the chat and the seeded Work transcripts stay valid.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.runner_helpers import make_run_spec
from nanobot.agent import handoff as handoff_module
from nanobot.agent.handoff import (
    HANDOFF_REPLY,
    HANDOFF_UNAVAILABLE_REPLY,
    HandoffHook,
    HandoffRequested,
    is_write_tool_name,
)
from nanobot.agent.hook import AgentHook, AgentRunHookContext, CompositeHook
from nanobot.agent.loop import AgentLoop, TurnContext, TurnKind
from nanobot.agent.runner import AgentRunner
from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.schema import tool_parameters_schema
from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import LLMResponse, LLMUsage, ToolCallRequest

_CHAT_ID = "chat-handoff"
_ORIGINAL_MESSAGE = "Read my inbox and draft a summary\n(second line must not leak into the title)"


def _provider() -> MagicMock:
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = SimpleNamespace(max_tokens=4096)
    return provider


def _make_loop(tmp_path: Path, provider: MagicMock, **kwargs: Any) -> AgentLoop:
    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        model="test-model",
        memory_index_enabled=False,
        **kwargs,
    )
    loop.auto_compact.prepare_session = MagicMock(  # type: ignore[method-assign]
        side_effect=lambda session, key: (session, None)
    )
    return loop


def _chat_message(metadata: dict[str, Any] | None = None) -> InboundMessage:
    return InboundMessage(
        channel="websocket",
        sender_id="client-1",
        chat_id=_CHAT_ID,
        content=_ORIGINAL_MESSAGE,
        metadata=dict(metadata or {}),
    )


def _tool_response(tool_call_id: str, name: str) -> LLMResponse:
    return LLMResponse(
        content="",
        finish_reason="tool_calls",
        tool_calls=[ToolCallRequest(id=tool_call_id, name=name, arguments={})],
        usage=LLMUsage.reported(input_tokens=1, output_tokens=1),
    )


def _final_response(content: str) -> LLMResponse:
    return LLMResponse(
        content=content,
        tool_calls=[],
        usage=LLMUsage.reported(input_tokens=1, output_tokens=1),
    )


def _fake_tool(name: str, *, result: str = "ok", calls: list[str], delay: float = 0.0) -> Tool:
    @tool_parameters(tool_parameters_schema(required=[]))
    class _FakeTool(Tool):
        @property
        def name(self) -> str:
            return name

        @property
        def description(self) -> str:
            return f"fake tool {name}"

        async def execute(self, **kwargs: Any) -> str:
            if delay:
                await asyncio.sleep(delay)
            calls.append(name)
            return result

    return _FakeTool()


def _registry(tools: list[Tool]) -> ToolRegistry:
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    return registry


def _assert_no_unanswered_tool_calls(messages: list[dict[str, Any]]) -> None:
    fulfilled = {
        message["tool_call_id"]
        for message in messages
        if message.get("role") == "tool" and message.get("tool_call_id")
    }
    for message in messages:
        if message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls") or []:
            assert call["id"] in fulfilled, (
                "transcript keeps an assistant message whose tool calls never ran"
            )


class _HandoffAtSecondBatchHook(HandoffHook):
    """Raise at the second tool boundary, without wall-clock races.

    The elapsed-time check itself is covered by the runner-level test; this
    subclass keeps the loop-path tests deterministic while still routing
    through the real ``after_iteration`` write gate (only the elapsed test is
    replaced, and it still honours ``saw_write``).
    """

    def __init__(self) -> None:
        super().__init__(threshold=float("inf"))
        self.batches = 0

    async def before_execute_tools(self, context: Any) -> None:
        self.transcript = context.messages
        self.batches += 1
        if self.batches >= 2 and not self.saw_write:
            raise HandoffRequested("time")


async def _handoff_turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    responses: list[LLMResponse],
    tools: list[Tool],
    threshold: float = 0.01,
    message: InboundMessage | None = None,
    advance_before_call: dict[int, float] | None = None,
    hook_factory: Any = None,
    create_task_override: Any = None,
) -> tuple[AgentLoop, Any]:
    provider = _provider()

    # A fake clock handed to the hook module: the wait between batches is
    # modelled in the provider, so the threshold checks stay deterministic.
    clock = {"now": 1_000.0}
    monkeypatch.setattr(handoff_module, "monotonic", lambda: clock["now"])

    async def chat_stream_with_retry(**kwargs: Any) -> LLMResponse:
        index = len(calls)
        calls.append(index)
        if advance_before_call and index in advance_before_call:
            clock["now"] += advance_before_call[index]
        return responses[index]

    calls: list[int] = []
    provider.chat_stream_with_retry = chat_stream_with_retry
    loop = _make_loop(tmp_path, provider, background_handoff_seconds=threshold)
    if hook_factory is not None:
        loop._handoff_hook_for_turn = (  # type: ignore[method-assign]
            lambda ctx: hook_factory()
        )
    if create_task_override is not None:
        loop.work_store.create_task = create_task_override  # type: ignore[method-assign]
    outbound = await loop._process_message(
        message or _chat_message(),
        tools=_registry(tools),
    )
    return loop, outbound


# ---------------------------------------------------------------------------
# Time-triggered hand-off
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_long_read_only_turn_hands_off_before_the_third_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    tools = [
        _fake_tool("read_a", calls=calls),
        _fake_tool("read_b", calls=calls),
        _fake_tool("read_c", calls=calls),
    ]
    loop, outbound = await _handoff_turn(
        tmp_path,
        monkeypatch,
        responses=[
            _tool_response("call-a", "read_a"),
            _tool_response("call-b", "read_b"),
            _tool_response("call-c", "read_c"),  # the batch that must never run
        ],
        tools=tools,
        advance_before_call={2: 0.05},
    )

    assert calls == ["read_a", "read_b"], "the pending third batch must never execute"
    assert outbound is not None
    assert outbound.content == HANDOFF_REPLY

    task_id = outbound.metadata.get("handoff_task_id")
    assert isinstance(task_id, str) and task_id.startswith("work_")
    tasks = loop.work_store.list_tasks()
    assert len(tasks) == 1
    task = loop.work_store.get_task(task_id)
    assert task is not None
    assert task["notify_on_finish"] == 1
    assert task["title"] == _ORIGINAL_MESSAGE.splitlines()[0][:80]
    assert task["session_key"] == f"work:{task_id}"
    assert task["chat_id"] == _CHAT_ID
    assert task["prompt_preview"].startswith("Read my inbox")

    # The Work task was enqueued exactly as ``work.create`` does, carrying the
    # original user message into the task's own session.
    inbound = loop.bus.inbound.get_nowait()
    assert loop.bus.inbound.empty()
    assert inbound.session_key_override == f"work:{task_id}"
    assert inbound.content == _ORIGINAL_MESSAGE
    assert inbound.metadata["work_task_id"] == task_id
    assert inbound.metadata["work_mode"] == "background"
    assert inbound.metadata["_wants_stream"] is True

    # Chat session: kept history + fixed reply, nothing from the dropped batch.
    chat = loop.sessions.get_or_create(f"websocket:{_CHAT_ID}")
    assert chat.messages[-1]["role"] == "assistant"
    assert chat.messages[-1]["content"] == HANDOFF_REPLY
    _assert_no_unanswered_tool_calls(chat.messages)
    persisted_names = [
        message.get("name") for message in chat.messages if message.get("role") == "tool"
    ]
    assert persisted_names == ["read_a", "read_b"]

    # Work session: seeded with the kept history only (no reply, no pending
    # batch), starting from the user request.
    work = loop.sessions.get_or_create(f"work:{task_id}")
    assert work.messages[0]["role"] == "user"
    assert work.messages[-1]["role"] == "tool"
    _assert_no_unanswered_tool_calls(work.messages)


@pytest.mark.asyncio
async def test_write_tool_turn_never_hands_off_however_long(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    tools = [
        _fake_tool("mcp_ziggy_gmail_gmail_archive_message", result="archived", calls=calls),
        _fake_tool("read_b", calls=calls),
    ]
    loop, outbound = await _handoff_turn(
        tmp_path,
        monkeypatch,
        responses=[
            _tool_response("call-w", "mcp_ziggy_gmail_gmail_archive_message"),
            _tool_response("call-b", "read_b"),
            _final_response("archived and read"),
        ],
        tools=tools,
        hook_factory=_HandoffAtSecondBatchHook,
    )

    assert calls == ["mcp_ziggy_gmail_gmail_archive_message", "read_b"]
    assert outbound is not None
    assert outbound.content == "archived and read"
    assert "handoff_task_id" not in outbound.metadata
    assert loop.work_store.list_tasks() == []


@pytest.mark.asyncio
async def test_approval_required_result_never_hands_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    approval_payload = (
        '{"status": "approval_required", "approval_id": "approval_1", '
        '"operation": "browser_act"}'
    )
    tools = [
        _fake_tool("mcp_ziggy_browser_act", result=approval_payload, calls=calls),
        _fake_tool("read_b", calls=calls),
    ]
    loop, outbound = await _handoff_turn(
        tmp_path,
        monkeypatch,
        responses=[
            _tool_response("call-act", "mcp_ziggy_browser_act"),
            _tool_response("call-b", "read_b"),
            _final_response("waiting for your tap"),
        ],
        tools=tools,
        hook_factory=_HandoffAtSecondBatchHook,
    )

    assert calls == ["mcp_ziggy_browser_act", "read_b"]
    assert outbound is not None
    assert outbound.content == "waiting for your tap"
    assert "handoff_task_id" not in outbound.metadata
    assert loop.work_store.list_tasks() == []


@pytest.mark.asyncio
async def test_threshold_zero_never_hands_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    tools = [
        _fake_tool("read_a", calls=calls),
        _fake_tool("read_b", calls=calls),
        _fake_tool("read_c", calls=calls),
    ]
    loop, outbound = await _handoff_turn(
        tmp_path,
        monkeypatch,
        responses=[
            _tool_response("call-a", "read_a"),
            _tool_response("call-b", "read_b"),
            _tool_response("call-c", "read_c"),
            _final_response("all read"),
        ],
        tools=tools,
        threshold=0,
        advance_before_call={2: 0.05},
    )

    assert calls == ["read_a", "read_b", "read_c"]
    assert outbound is not None
    assert outbound.content == "all read"
    assert "handoff_task_id" not in outbound.metadata
    assert loop.work_store.list_tasks() == []


# ---------------------------------------------------------------------------
# Hook attachment gates
# ---------------------------------------------------------------------------


def _fake_ctx(msg: InboundMessage) -> TurnContext:
    return TurnContext(
        msg=msg,
        session_key=f"{msg.channel}:{msg.chat_id}",
        turn_id="t1",
        runtime=None,
        kind=TurnKind.USER,
        delivery=MagicMock(),
    )


@pytest.mark.asyncio
async def test_only_private_websocket_chat_turns_get_the_hook(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path, _provider(), background_handoff_seconds=45)

    assert isinstance(loop._handoff_hook_for_turn(_fake_ctx(_chat_message())), HandoffHook)

    work_turn = _chat_message({"work_task_id": f"work_{'0' * 32}"})
    assert loop._handoff_hook_for_turn(_fake_ctx(work_turn)) is None

    room_turn = _chat_message({"shared_room": True})
    assert loop._handoff_hook_for_turn(_fake_ctx(room_turn)) is None

    cron_turn = _chat_message({"_cron_trigger": {"run_id": "run-1", "job_id": "job-1"}})
    assert loop._handoff_hook_for_turn(_fake_ctx(cron_turn)) is None

    continuation = _chat_message({"_internal_continuation": True})
    assert loop._handoff_hook_for_turn(_fake_ctx(continuation)) is None

    cli_turn = InboundMessage(
        channel="cli", sender_id="client-1", chat_id=_CHAT_ID, content="hello"
    )
    assert loop._handoff_hook_for_turn(_fake_ctx(cli_turn)) is None

    loop.background_handoff_seconds = 0
    assert loop._handoff_hook_for_turn(_fake_ctx(_chat_message())) is None


# ---------------------------------------------------------------------------
# Runner-level contract: HandoffRequested is a stop, not an error
# ---------------------------------------------------------------------------


class _RecorderHook(AgentHook):
    def __init__(self) -> None:
        super().__init__()
        self.error_calls = 0
        self.finally_context: AgentRunHookContext | None = None

    async def on_error(self, context: AgentRunHookContext) -> None:
        self.error_calls += 1

    async def on_finally(self, context: AgentRunHookContext) -> None:
        self.finally_context = context


@pytest.mark.asyncio
async def test_handoff_requested_is_not_an_error_for_the_runner() -> None:
    provider = _provider()
    provider.chat_stream_with_retry = AsyncMock(
        return_value=_tool_response("call-a", "never_runs")
    )
    calls: list[str] = []
    registry = _registry([_fake_tool("never_runs", calls=calls)])
    recorder = _RecorderHook()
    hook = CompositeHook(
        [recorder, HandoffHook(threshold=1, started_at=time.monotonic() - 10)]
    )

    with pytest.raises(HandoffRequested) as excinfo:
        await AgentRunner().run(
            make_run_spec(
                provider,
                model="test-model",
                max_iterations=5,
                max_tool_result_chars=16_000,
                initial_messages=[{"role": "user", "content": "go"}],
                tools=registry,
                hook=hook,
            )
        )

    assert excinfo.value.reason == "time"
    assert calls == [], "the pending batch must not execute on hand-off"
    assert recorder.error_calls == 0, "on_error must not fire for a hand-off stop"
    assert recorder.finally_context is not None
    assert recorder.finally_context.stop_reason == "handoff"


def test_handoff_hook_time_semantics_and_write_gate() -> None:
    assert is_write_tool_name("mcp_ziggy_gmail_gmail_archive_message")
    assert is_write_tool_name("mcp_ziggy_gmail_send_message")
    assert is_write_tool_name("browser_fill_form")
    assert not is_write_tool_name("gmail_search")
    assert not is_write_tool_name("mcp_ziggy_browser_read_page")

    async def scenario() -> tuple[bool, bool]:
        from nanobot.agent.hook import AgentHookContext

        stale = time.monotonic() - 100
        handoff_raised = False
        hook = HandoffHook(threshold=1, started_at=stale)
        context = AgentHookContext(iteration=0, messages=[{"role": "user", "content": "go"}])
        context.tool_calls = [ToolCallRequest(id="1", name="mcp_ziggy_browser_act", arguments={})]
        try:
            await hook.before_execute_tools(context)
        except HandoffRequested as exc:
            handoff_raised = exc.reason == "time"

        write_raised = False
        gated = HandoffHook(threshold=1, started_at=stale)
        gated_context = AgentHookContext(
            iteration=0, messages=[{"role": "user", "content": "go"}]
        )
        gated_context.tool_calls = [
            ToolCallRequest(id="1", name="mcp_ziggy_gmail_send_message", arguments={})
        ]
        await gated.after_iteration(gated_context)
        try:
            await gated.before_execute_tools(gated_context)
        except HandoffRequested:
            write_raised = True
        return handoff_raised, write_raised

    handoff_raised, write_raised = asyncio.run(scenario())
    assert handoff_raised
    assert not write_raised, "a turn that called a write tool must never hand off"


@pytest.mark.asyncio
async def test_failed_task_creation_replies_with_the_apology(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    tools = [_fake_tool("read_a", calls=calls)]

    def exploding_create(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("store on fire")

    loop, outbound = await _handoff_turn(
        tmp_path,
        monkeypatch,
        responses=[
            _tool_response("call-a", "read_a"),
            _tool_response("call-b", "read_a"),  # batch never runs; hand-off fires
        ],
        tools=tools,
        hook_factory=_HandoffAtSecondBatchHook,
        create_task_override=exploding_create,
    )

    assert calls == ["read_a"], "the pending batch must not re-execute after the failure"
    assert outbound is not None
    assert outbound.content == HANDOFF_UNAVAILABLE_REPLY
    assert "handoff_task_id" not in outbound.metadata
    chat = loop.sessions.get_or_create(f"websocket:{_CHAT_ID}")
    assert chat.messages[-1]["content"] == HANDOFF_UNAVAILABLE_REPLY
    _assert_no_unanswered_tool_calls(chat.messages)
