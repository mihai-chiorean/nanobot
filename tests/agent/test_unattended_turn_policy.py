"""Turn-scoped policy for unattended (background / scheduled) Work runs.

Two things are decided per turn from the inbound ``work_mode`` metadata:

* the scheduling class the provider sends as ``X-Ziggy-Scheduling-Class``,
  which the admission gateway uses to cap background concurrency, and
* whether ``ask_user`` is offered: a scheduled (cron) run has nobody to answer,
  so a question would park forever while the Work task is recorded succeeded.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.agent.tools.ask import ask_user_unanswerable
from nanobot.agent.tools.context import RequestContext, request_context
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import GenerationSettings, LLMResponse, LLMUsage, ToolCallRequest
from nanobot.providers.request_context import current_scheduling_class


def _tool_names(tools: list[dict[str, Any]] | None) -> set[str]:
    names: set[str] = set()
    for schema in tools or []:
        fn = schema.get("function") if isinstance(schema, dict) else None
        name = fn.get("name") if isinstance(fn, dict) else schema.get("name")
        if isinstance(name, str):
            names.add(name)
    return names


class _Recorder:
    """Provider stub that records the class and tool list of every model call."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.gate: asyncio.Event | None = None
        self.entered: asyncio.Event | None = None

    async def chat_stream_with_retry(self, **kwargs: Any) -> LLMResponse:
        record: dict[str, Any] = {
            "class": current_scheduling_class(),
            "tools": _tool_names(kwargs.get("tools")),
        }
        self.calls.append(record)
        if self.entered is not None:
            self.entered.set()
        if self.gate is not None:
            await self.gate.wait()
            # Re-read after yielding: a concurrent turn must not have changed it.
            record["class_after_wait"] = current_scheduling_class()
        return LLMResponse(
            content="done",
            tool_calls=[],
            usage=LLMUsage.reported(input_tokens=1, output_tokens=1),
        )


def _make_loop(tmp_path: Path, recorder: _Recorder) -> AgentLoop:
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = GenerationSettings()
    provider.chat_stream_with_retry = recorder.chat_stream_with_retry
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


async def _scheduled_turn(loop: AgentLoop) -> None:
    await loop.process_direct(
        "run the digest",
        session_key="cron:job-1",
        channel="websocket",
        chat_id="owner-chat",
        metadata={"work_mode": "scheduled", "work_task_id": "work_1", "cron_job_id": "job-1"},
    )


async def _background_turn(loop: AgentLoop) -> None:
    await loop.process_direct(
        "research this",
        session_key="websocket:owner-chat",
        channel="websocket",
        chat_id="owner-chat",
        metadata={"work_mode": "background", "work_task_id": "work_2"},
    )


async def _interactive_turn(loop: AgentLoop, chat_id: str = "owner-chat") -> None:
    await loop.process_direct(
        "hello",
        session_key=f"websocket:{chat_id}",
        channel="websocket",
        chat_id=chat_id,
    )


# -- scheduling class ------------------------------------------------------


@pytest.mark.asyncio
async def test_scheduled_turn_is_background(tmp_path: Path) -> None:
    recorder = _Recorder()
    await _scheduled_turn(_make_loop(tmp_path, recorder))
    assert [c["class"] for c in recorder.calls] == ["background"]


@pytest.mark.asyncio
async def test_background_turn_is_background(tmp_path: Path) -> None:
    recorder = _Recorder()
    await _background_turn(_make_loop(tmp_path, recorder))
    assert [c["class"] for c in recorder.calls] == ["background"]


@pytest.mark.asyncio
async def test_interactive_turn_is_foreground(tmp_path: Path) -> None:
    recorder = _Recorder()
    await _interactive_turn(_make_loop(tmp_path, recorder))
    assert [c["class"] for c in recorder.calls] == ["foreground"]


@pytest.mark.asyncio
async def test_interactive_turn_after_background_turn_is_foreground_again(
    tmp_path: Path,
) -> None:
    recorder = _Recorder()
    loop = _make_loop(tmp_path, recorder)
    await _background_turn(loop)
    await _interactive_turn(loop)
    assert [c["class"] for c in recorder.calls] == ["background", "foreground"]
    assert current_scheduling_class() == "foreground"


@pytest.mark.asyncio
async def test_concurrent_turns_do_not_leak_their_class(tmp_path: Path) -> None:
    """A background run parked mid-call must not mark a concurrent chat turn."""
    recorder = _Recorder()
    recorder.gate = asyncio.Event()
    recorder.entered = asyncio.Event()
    loop = _make_loop(tmp_path, recorder)

    background = asyncio.create_task(_scheduled_turn(loop))
    await recorder.entered.wait()
    recorder.entered.clear()
    interactive = asyncio.create_task(_interactive_turn(loop, chat_id="other-chat"))
    await recorder.entered.wait()
    recorder.gate.set()
    await asyncio.gather(background, interactive)

    by_class = sorted((c["class"], c["class_after_wait"]) for c in recorder.calls)
    assert by_class == [("background", "background"), ("foreground", "foreground")]


# -- ask_user availability -------------------------------------------------


@pytest.mark.asyncio
async def test_scheduled_turn_is_not_offered_ask_user(tmp_path: Path) -> None:
    recorder = _Recorder()
    await _scheduled_turn(_make_loop(tmp_path, recorder))
    tools = recorder.calls[0]["tools"]
    assert "ask_user" not in tools
    assert "web_search" in tools  # only ask_user is withheld


@pytest.mark.asyncio
async def test_background_turn_keeps_ask_user(tmp_path: Path) -> None:
    recorder = _Recorder()
    await _background_turn(_make_loop(tmp_path, recorder))
    assert "ask_user" in recorder.calls[0]["tools"]


@pytest.mark.asyncio
async def test_interactive_turn_keeps_ask_user(tmp_path: Path) -> None:
    recorder = _Recorder()
    await _interactive_turn(_make_loop(tmp_path, recorder))
    assert "ask_user" in recorder.calls[0]["tools"]


@pytest.mark.asyncio
async def test_scheduled_turn_that_calls_ask_user_anyway_is_refused(tmp_path: Path) -> None:
    """A hallucinated call is answered with an error, never parked."""
    seen: list[str] = []
    step = 0

    async def chat_stream_with_retry(**kwargs: Any) -> LLMResponse:
        nonlocal step
        step += 1
        if step == 1:
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(id="ask-1", name="ask_user", arguments={"question": "Which?"})
                ],
                usage=LLMUsage.reported(input_tokens=1, output_tokens=1),
            )
        seen.extend(
            str(m.get("content"))
            for m in kwargs["messages"]
            if m.get("role") == "tool"
        )
        return LLMResponse(
            content="assumed the default",
            tool_calls=[],
            usage=LLMUsage.reported(input_tokens=1, output_tokens=1),
        )

    recorder = _Recorder()
    loop = _make_loop(tmp_path, recorder)
    loop.provider.chat_stream_with_retry = chat_stream_with_retry  # type: ignore[attr-defined]
    loop.runner.provider = loop.provider
    result = await loop.process_direct(
        "run the digest",
        session_key="cron:job-1",
        channel="websocket",
        chat_id="owner-chat",
        metadata={"work_mode": "scheduled"},
    )
    assert result is not None and result.content == "assumed the default"
    assert any("unavailable in a scheduled run" in text for text in seen)


def test_registry_hides_ask_user_for_a_bound_cron_turn(tmp_path: Path) -> None:
    """A session-bound cron run reuses the chat's session key; its marker decides."""
    loop = _make_loop(tmp_path, _Recorder())
    ctx = RequestContext(
        channel="websocket",
        chat_id="owner-chat",
        session_key="websocket:owner-chat",
        metadata={"_cron_trigger": {"job_id": "job-1"}},
    )
    with request_context(ctx):
        assert "ask_user" not in _tool_names(loop.tools.get_definitions())
    assert "ask_user" in _tool_names(loop.tools.get_definitions())


@pytest.mark.parametrize(
    ("metadata", "session_key", "expected"),
    [
        ({"work_mode": "scheduled"}, "websocket:x", True),
        ({"_cron_trigger": {"job_id": "j"}}, "websocket:x", True),
        ({}, "cron:job-1", True),
        ({"work_mode": "background"}, "websocket:x", False),
        ({}, "websocket:x", False),
        (None, None, False),
    ],
)
def test_ask_user_unanswerable(metadata, session_key, expected) -> None:
    assert ask_user_unanswerable(metadata, session_key) is expected
