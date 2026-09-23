"""Tests for the ask_user tool: pause, resume, buttons, and abandoned turns."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.runner_helpers import make_run_spec
from nanobot.agent.loop import AgentLoop
from nanobot.agent.runner import AgentRunner
from nanobot.agent.tools.ask import (
    ASK_USER_ANSWER_MAX_AGE_S,
    AskUserInterrupt,
    AskUserTool,
    ask_user_call_is_expired,
    ask_user_options_from_messages,
    ask_user_outbound,
    pending_ask_user_id,
)
from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.schema import tool_parameters_schema
from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.config.schema import AgentDefaults
from nanobot.providers.base import GenerationSettings, LLMResponse, ToolCallRequest
from nanobot.session.recovery import RUNTIME_CHECKPOINT_KEY

_MAX_TOOL_RESULT_CHARS = AgentDefaults().max_tool_result_chars


def _make_provider(*responses):
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = GenerationSettings()
    provider.chat_stream_with_retry = AsyncMock(side_effect=list(responses))
    return provider


def _ask_response(tool_call_id: str, question: str, options: list[str] | None = None):
    return LLMResponse(
        content="",
        finish_reason="tool_calls",
        tool_calls=[
            ToolCallRequest(
                id=tool_call_id,
                name="ask_user",
                arguments={"question": question, "options": options or []},
            )
        ],
    )


def _make_loop(tmp_path, provider):
    return AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        model="test-model",
    )


def test_ask_user_tool_schema_and_interrupt():
    tool = AskUserTool()
    schema = tool.to_schema()["function"]

    assert schema["name"] == "ask_user"
    assert "question" in schema["parameters"]["required"]
    assert schema["parameters"]["properties"]["options"]["type"] == "array"

    with pytest.raises(AskUserInterrupt) as exc:
        asyncio.run(tool.execute("Continue?", options=["Yes", "No"]))

    assert exc.value.question == "Continue?"
    assert exc.value.options == ["Yes", "No"]


@pytest.mark.asyncio
async def test_runner_pauses_on_ask_user_without_executing_later_tools():
    @tool_parameters(tool_parameters_schema(required=[]))
    class LaterTool(Tool):
        called = False

        @property
        def name(self) -> str:
            return "later"

        @property
        def description(self) -> str:
            return "Should not run after ask_user pauses the turn."

        async def execute(self, **kwargs):
            self.called = True
            return "later result"

    provider = MagicMock()
    provider.generation = GenerationSettings()
    provider.chat_stream_with_retry = AsyncMock(return_value=LLMResponse(
        content="",
        finish_reason="tool_calls",
        tool_calls=[
            ToolCallRequest(
                id="call_ask",
                name="ask_user",
                arguments={"question": "Install this package?", "options": ["Yes", "No"]},
            ),
            ToolCallRequest(id="call_later", name="later", arguments={}),
        ],
    ))

    later = LaterTool()
    tools = ToolRegistry()
    tools.register(AskUserTool())
    tools.register(later)

    result = await AgentRunner().run(make_run_spec(
        provider,
        initial_messages=[{"role": "user", "content": "continue"}],
        tools=tools,
        model="test-model",
        max_iterations=3,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
        concurrent_tools=True,
    ))

    assert result.stop_reason == "ask_user"
    assert result.final_content == "Install this package?"
    assert "ask_user" in result.tools_used
    assert later.called is False
    assert result.messages[-1]["role"] == "assistant"
    tool_calls = result.messages[-1]["tool_calls"]
    assert [tool_call["function"]["name"] for tool_call in tool_calls] == ["ask_user"]
    assert not any(message.get("name") == "ask_user" for message in result.messages)
    assert {"name": "ask_user", "status": "waiting"} in [
        {key: event[key] for key in ("name", "status") if key in event}
        for event in result.tool_events
    ]


@pytest.mark.asyncio
async def test_ask_user_text_fallback_resumes_with_next_message(tmp_path):
    seen_messages: list[list[dict]] = []
    responses = iter([
        _ask_response("call_ask", "Install the optional package?", ["Install", "Skip"]),
        LLMResponse(content="Skipped install.", finish_reason="stop"),
    ])

    async def chat_with_retry(**kwargs):
        seen_messages.append([dict(message) for message in kwargs["messages"]])
        return next(responses)

    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = GenerationSettings()
    provider.chat_stream_with_retry = chat_with_retry

    loop = _make_loop(tmp_path, provider)

    first = await loop._process_message(
        InboundMessage(channel="cli", sender_id="user", chat_id="direct", content="set it up"),
    )

    assert first is not None
    assert first.content == "Install the optional package?\n\n1. Install\n2. Skip"
    assert first.buttons == []
    assert first.event is None

    session = loop.sessions.get_or_create("cli:direct")
    assert any(
        message.get("role") == "assistant" and message.get("tool_calls")
        for message in session.messages
    )
    assert not any(
        message.get("role") == "tool" and message.get("name") == "ask_user"
        for message in session.messages
    )
    assert pending_ask_user_id(session.get_history()) == "call_ask"

    second = await loop._process_message(
        InboundMessage(channel="cli", sender_id="user", chat_id="direct", content="Skip")
    )

    assert second is not None
    assert second.content == "Skipped install."
    assert any(
        message.get("role") == "tool"
        and message.get("name") == "ask_user"
        and message.get("content") == "Skip"
        for message in seen_messages[-1]
    )
    assert not any(
        message.get("role") == "user" and message.get("content") == "Skip"
        for message in session.messages
    )
    assert any(
        message.get("role") == "tool"
        and message.get("name") == "ask_user"
        and message.get("content") == "Skip"
        for message in session.messages
    )
    assert pending_ask_user_id(session.get_history()) is None


@pytest.mark.asyncio
async def test_ask_user_shared_room_message_does_not_consume_parked_question(tmp_path):
    seen_messages: list[list[dict]] = []
    responses = iter([
        _ask_response("call_ask", "Install the optional package?", None),
        LLMResponse(content="Room chatter noted.", finish_reason="stop"),
    ])

    async def chat_with_retry(**kwargs):
        seen_messages.append([dict(message) for message in kwargs["messages"]])
        return next(responses)

    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = GenerationSettings()
    provider.chat_stream_with_retry = chat_with_retry

    loop = _make_loop(tmp_path, provider)

    await loop._process_message(
        InboundMessage(channel="cli", sender_id="user", chat_id="room", content="set it up"),
    )

    room_reply = await loop._process_message(
        InboundMessage(
            channel="cli",
            sender_id="other",
            chat_id="room",
            content="Skip",
            metadata={"shared_room": True},
        ),
    )

    assert room_reply is not None
    assert room_reply.content == "Room chatter noted."
    # The room message was not used as the answer: it stayed a user message.
    assert any(
        message.get("role") == "user" and message.get("content") == "Skip"
        for message in seen_messages[-1]
    )
    assert not any(
        message.get("role") == "tool" and message.get("name") == "ask_user"
        and message.get("content") == "Skip"
        for message in seen_messages[-1]
    )

    session = loop.sessions.get_or_create("cli:room")
    assert pending_ask_user_id(session.get_history()) == "call_ask"


@pytest.mark.asyncio
async def test_ask_user_keeps_buttons_for_telegram(tmp_path):
    provider = _make_provider(
        _ask_response("call_ask", "Install the optional package?", ["Install", "Skip"]),
    )
    loop = _make_loop(tmp_path, provider)

    response = await loop._process_message(
        InboundMessage(channel="telegram", sender_id="user", chat_id="123", content="set it up")
    )

    assert response is not None
    assert response.content == "Install the optional package?"
    assert response.buttons == [["Install", "Skip"]]


@pytest.mark.asyncio
async def test_ask_user_keeps_buttons_for_websocket(tmp_path):
    provider = _make_provider(
        _ask_response("call_ask", "Install the optional package?", ["Install", "Skip"]),
    )
    loop = _make_loop(tmp_path, provider)

    response = await loop._process_message(
        InboundMessage(channel="websocket", sender_id="user", chat_id="123", content="set it up")
    )

    assert response is not None
    assert response.content == "Install the optional package?"
    assert response.buttons == [["Install", "Skip"]]


@pytest.mark.asyncio
async def test_cancelled_turn_while_parked_leaves_no_pending_ask(tmp_path):
    """Cancelling while the ask tool runs must not leave a resumable ask."""
    ask_started = asyncio.Event()
    hold = asyncio.Event()

    class _BlockingAskTool(Tool):
        @property
        def name(self) -> str:
            return "ask_user"

        @property
        def description(self) -> str:
            return "blocking stand-in for AskUserTool"

        @property
        def parameters(self) -> dict:
            return AskUserTool().parameters

        @property
        def exclusive(self) -> bool:
            return True

        async def execute(self, question: str, options: list[str] | None = None, **_):
            ask_started.set()
            await hold.wait()
            raise AskUserInterrupt(question=question, options=options)

    responses = iter([
        _ask_response("call_ask", "Really proceed?", ["Yes", "No"]),
        LLMResponse(content="New request handled.", finish_reason="stop"),
    ])
    seen_messages: list[list[dict]] = []

    async def chat_with_retry(**kwargs):
        seen_messages.append([dict(message) for message in kwargs["messages"]])
        return next(responses)

    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = GenerationSettings()
    provider.chat_stream_with_retry = chat_with_retry
    loop = _make_loop(tmp_path, provider)
    loop.tools.register(_BlockingAskTool())

    task = asyncio.create_task(
        loop._dispatch(
            InboundMessage(channel="cli", sender_id="user", chat_id="abandon", content="go")
        )
    )
    await asyncio.wait_for(ask_started.wait(), timeout=5)

    session = loop.sessions.get_or_create("cli:abandon")
    checkpoint = session.metadata.get(RUNTIME_CHECKPOINT_KEY)
    assert isinstance(checkpoint, dict)
    assert checkpoint.get("phase") == "awaiting_tools"
    assert [tc["function"]["name"] for tc in checkpoint.get("pending_tool_calls") or []] == [
        "ask_user"
    ]

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    hold.set()

    # The cancellation path materialized the checkpoint exactly once.
    assert RUNTIME_CHECKPOINT_KEY not in session.metadata
    history = session.get_history()
    assert pending_ask_user_id(history) is None
    assert any(
        message.get("role") == "tool"
        and message.get("tool_call_id") == "call_ask"
        and "interrupted" in str(message.get("content", "")).lower()
        for message in history
    )

    followup = await loop._process_message(
        InboundMessage(channel="cli", sender_id="user", chat_id="abandon", content="never mind")
    )
    assert followup is not None
    assert followup.content == "New request handled."
    # The follow-up entered as a fresh user message, not as a belated answer.
    final_request = seen_messages[-1]
    assert final_request[-1]["role"] == "user"
    assert "never mind" in str(final_request[-1]["content"])


def test_waiting_tool_event_is_not_reported_as_error():
    from nanobot.agent.hook import AgentHookContext
    from nanobot.utils.progress_events import build_tool_event_finish_payloads

    context = AgentHookContext(iteration=0, messages=[])
    context.tool_calls = [
        ToolCallRequest(id="call-1", name="ask_user", arguments={"question": "q"})
    ]
    context.tool_results = [""]
    context.tool_events = [{"name": "ask_user", "status": "waiting", "detail": "q"}]

    payloads = build_tool_event_finish_payloads(context)

    assert payloads and payloads[0]["phase"] == "end"
    assert payloads[0]["error"] is None


def test_ask_user_outbound_renders_text_fallback_off_button_channels():
    content, buttons = ask_user_outbound("Pick one:", ["A", "B"], "cli")
    assert content == "Pick one:\n\n1. A\n2. B"
    assert buttons == []

    content, buttons = ask_user_outbound("Pick one:", ["A", "B"], "telegram")
    assert content == "Pick one:"
    assert buttons == [["A", "B"]]

    content, buttons = ask_user_outbound(None, ["A"], "slack")
    assert content == "1. A"
    assert buttons == []

    content, buttons = ask_user_outbound("Pick one:", [], "cli")
    assert content == "Pick one:"
    assert buttons == []


def test_ask_user_options_recovered_from_trailing_call():
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "ask_user",
                        "arguments": '{"question": "x", "options": ["Yes", "No"]}',
                    },
                }
            ],
        }
    ]
    assert ask_user_options_from_messages(messages) == ["Yes", "No"]
    assert ask_user_options_from_messages([]) == []


def test_pending_ask_user_id_requires_unanswered_call():
    history = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "ask_user", "arguments": "{}"},
                }
            ],
        }
    ]
    assert pending_ask_user_id(history) == "call_1"

    answered = [
        *history,
        {"role": "tool", "tool_call_id": "call_1", "name": "ask_user", "content": "ok"},
    ]
    assert pending_ask_user_id(answered) is None

    assert pending_ask_user_id([
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_9",
                    "type": "function",
                    "function": {"name": "shell", "arguments": "{}"},
                }
            ],
        }
    ]) is None


def _parked_ask_history(*, timestamp: str | None) -> list[dict]:
    row: dict = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "ask_user", "arguments": '{"question": "Proceed?"}'},
            }
        ],
    }
    if timestamp is not None:
        row["timestamp"] = timestamp
    return [
        {"role": "user", "content": "set it up"},
        row,
    ]


def test_ask_user_call_is_expired_bounds_the_resume():
    now = datetime.now(timezone.utc)
    old = (now - timedelta(seconds=ASK_USER_ANSWER_MAX_AGE_S + 60)).isoformat()
    fresh = (now - timedelta(seconds=60)).isoformat()

    # A freshly parked question is still answerable (positive control, using a
    # phrasing the bound was not designed around: a near-bound recent stamp).
    assert ask_user_call_is_expired(_parked_ask_history(timestamp=fresh), "call_1", now=now) is False
    # The just-inside boundary is not expired; one second past it is.
    inside = (now - timedelta(seconds=ASK_USER_ANSWER_MAX_AGE_S - 1)).isoformat()
    outside = (now - timedelta(seconds=ASK_USER_ANSWER_MAX_AGE_S + 1)).isoformat()
    assert ask_user_call_is_expired(_parked_ask_history(timestamp=inside), "call_1", now=now) is False
    assert ask_user_call_is_expired(_parked_ask_history(timestamp=outside), "call_1", now=now) is True
    assert ask_user_call_is_expired(_parked_ask_history(timestamp=old), "call_1", now=now) is True

    # An unrelated old message must not be resolved against the parked call: the
    # helper keys on the assistant row that actually holds the call, so a stale
    # tail elsewhere in history is irrelevant (negative control).
    unrelated_old_user = {"role": "user", "content": "unrelated", "timestamp": old}
    assert ask_user_call_is_expired(
        [unrelated_old_user, *_parked_ask_history(timestamp=fresh)], "call_1", now=now
    ) is False

    # Missing / unparseable timestamps fail open (the caller still requires an
    # unanswered call, so this never orphans a resumable question).
    assert ask_user_call_is_expired(_parked_ask_history(timestamp=None), "call_1", now=now) is False
    assert ask_user_call_is_expired(
        _parked_ask_history(timestamp="not-a-timestamp"), "call_1", now=now
    ) is False
    # An id that is not a parked ask_user is never expirable.
    assert ask_user_call_is_expired(_parked_ask_history(timestamp=old), "call_zzz", now=now) is False


@pytest.mark.asyncio
async def test_stale_parked_ask_is_not_answered_by_a_later_message(tmp_path):
    """An abandoned question is not resolved by a much-later unrelated message."""
    seen_messages: list[list[dict]] = []
    responses = iter(
        [
            _ask_response("call_ask", "Install the optional package?", ["Install", "Skip"]),
            LLMResponse(content="New request handled.", finish_reason="stop"),
        ]
    )

    async def chat_with_retry(**kwargs):
        seen_messages.append([dict(message) for message in kwargs["messages"]])
        return next(responses)

    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = GenerationSettings()
    provider.chat_stream_with_retry = chat_with_retry

    loop = _make_loop(tmp_path, provider)

    first = await loop._process_message(
        InboundMessage(channel="cli", sender_id="user", chat_id="direct", content="set it up")
    )
    assert first is not None
    assert first.content == "Install the optional package?\n\n1. Install\n2. Skip"

    # Age the parked question beyond the resume window, exactly as a real
    # abandoned turn would be aged by wall-clock time passing.
    session = loop.sessions.get_or_create("cli:direct")
    stale = (datetime.now(timezone.utc) - timedelta(seconds=ASK_USER_ANSWER_MAX_AGE_S + 3600)).isoformat()
    parked_rows = [
        message
        for message in session.messages
        if message.get("role") == "assistant"
        and any(
            (call.get("function") or {}).get("name") == "ask_user"
            for call in message.get("tool_calls") or []
        )
    ]
    assert parked_rows, "expected a parked ask_user row to age"
    for message in parked_rows:
        message["timestamp"] = stale
    loop.sessions.save(session)

    # The next plain message is a fresh request, not a belated answer: it is
    # delivered as a user row, never as the parked call's tool result.
    second = await loop._process_message(
        InboundMessage(channel="cli", sender_id="user", chat_id="direct", content="what is the weather")
    )
    assert second is not None
    assert second.content == "New request handled."
    assert not any(
        message.get("role") == "tool"
        and message.get("name") == "ask_user"
        and message.get("content") == "what is the weather"
        for message in seen_messages[-1]
    )
    assert any(
        message.get("role") == "user" and "what is the weather" in str(message.get("content"))
        for message in seen_messages[-1]
    )


@pytest.mark.asyncio
async def test_fresh_parked_ask_is_still_answered_after_aging_a_distractor(tmp_path):
    """Negative control for the bound: aging a *distractor* row must not block
    the real answer, whose own parked row stays fresh."""
    seen_messages: list[list[dict]] = []
    responses = iter(
        [
            _ask_response("call_ask", "Install the optional package?", ["Install", "Skip"]),
            LLMResponse(content="Skipped install.", finish_reason="stop"),
        ]
    )

    async def chat_with_retry(**kwargs):
        seen_messages.append([dict(message) for message in kwargs["messages"]])
        return next(responses)

    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = GenerationSettings()
    provider.chat_stream_with_retry = chat_with_retry

    loop = _make_loop(tmp_path, provider)

    await loop._process_message(
        InboundMessage(channel="cli", sender_id="user", chat_id="direct", content="set it up")
    )

    # Age only the user prompt row; the parked ask_user row keeps its fresh
    # timestamp, so the resume must still succeed.
    session = loop.sessions.get_or_create("cli:direct")
    stale = (datetime.now(timezone.utc) - timedelta(seconds=ASK_USER_ANSWER_MAX_AGE_S + 3600)).isoformat()
    user_rows = [m for m in session.messages if m.get("role") == "user"]
    assert user_rows
    for message in user_rows:
        message["timestamp"] = stale
    loop.sessions.save(session)

    second = await loop._process_message(
        InboundMessage(channel="cli", sender_id="user", chat_id="direct", content="Skip")
    )
    assert second is not None
    assert second.content == "Skipped install."
    assert any(
        message.get("role") == "tool"
        and message.get("name") == "ask_user"
        and message.get("content") == "Skip"
        for message in seen_messages[-1]
    )
