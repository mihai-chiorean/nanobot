"""A Work turn ends ``waiting`` when a tool result parks the task (D4-36, FR-LOGIN-009).

When a browser tool result carries ``_meta["ziggy.dev/park"]`` during a Work
turn (D5 stamps it for Work-origin parks only), the turn must close the task
out as ``waiting`` — an active, re-entrant status — not ``succeeded``.
ziggy-work's executor returns from its River job on a ``waiting``
``status.changed`` just like a terminal one, and re-queues the task when the
park is resolved, so the two load-bearing properties here are:

* the final published ``status.changed`` says ``waiting`` (not ``succeeded``),
  and nothing later flips it to a terminal status;
* an ordinary turn, and any turn whose tools set no park attribute, still ends
  ``succeeded`` — the new check must be inert for them.

The carrier is ``RequestContext.attributes`` (bound through the module-level
contextvar), *not* ``TurnContext.attributes``, which is a snapshot copy tools
cannot reach.  A regression that reads the wrong dict reads an always-empty
one and silently ends every parked turn ``succeeded``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.agent.tools.context import _CURRENT_REQUEST_CONTEXT, ZIGGY_PARK_ATTRIBUTE
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import LLMResponse, ToolCallRequest
from nanobot.work.store import WorkStore

CHAT_ID = "99999999-8888-7777-6666-555555555555"

PARK = {"task_id": "btask_" + "a" * 24, "site": "example.com"}


class _Collector:
    """Drain the Work events the loop published onto the outbound queue."""

    def __init__(self, bus: MessageBus) -> None:
        self._bus = bus
        self.events: list[dict[str, Any]] = []

    def drain(self) -> None:
        while True:
            try:
                msg = self._bus.outbound.get_nowait()
            except asyncio.QueueEmpty:
                return
            payload = msg.metadata.get("_work_event")
            if payload is not None:
                self.events.append(payload)

    def types(self) -> list[str]:
        return [e["type"] for e in self.events]

    def statuses(self) -> list[str]:
        return [
            e["payload"]["status"] for e in self.events if e["type"] == "status.changed"
        ]


def _make_loop(tmp_path: Path, bus: MessageBus) -> AgentLoop:
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    return AgentLoop(bus=bus, provider=provider, workspace=tmp_path, model="test-model")


def _scripted(loop: AgentLoop, responses: list[LLMResponse]) -> None:
    calls = iter(responses)
    loop.provider.chat_stream_with_retry = AsyncMock(  # type: ignore[method-assign]
        side_effect=lambda *a, **kw: next(calls)
    )
    loop.tools.get_definitions = MagicMock(return_value=[])  # type: ignore[method-assign]


async def _run(
    loop: AgentLoop,
    task_id: str,
    *,
    content: str = "log in to example.com",
) -> Any:
    return await loop.process_direct(
        content,
        session_key=f"work:{task_id}",
        channel="websocket",
        chat_id=CHAT_ID,
        metadata={"work_task_id": task_id, "work_mode": "background"},
    )


def _parking_tool(loop: AgentLoop, *, park: dict[str, Any] | None = PARK) -> None:
    """Make the turn's single tool call stamp the park signal like MCPToolWrapper does."""
    tool_call = ToolCallRequest(
        id="call1", name="browser_login", arguments={"site": "example.com"}
    )
    _scripted(
        loop,
        [
            LLMResponse(content="Signing in", tool_calls=[tool_call]),
            LLMResponse(content="Parked for sign-in.", tool_calls=[]),
        ],
    )
    loop.tools.prepare_call = MagicMock(  # type: ignore[method-assign]
        return_value=(None, {"site": "example.com"}, None)
    )
    loop.tools.prepare_call_ex = MagicMock(return_value=(*loop.tools.prepare_call.return_value, []))

    async def _execute(tool: Any, arguments: dict[str, Any], *a: Any, **kw: Any) -> str:
        # Assert the binding exists: it is what makes the carrier testable and
        # fails loudly if the run stage ever stops binding it.
        ctx = _CURRENT_REQUEST_CONTEXT.get()
        assert ctx is not None, "the run stage must bind the per-turn RequestContext"
        if park is not None:
            ctx.attributes[ZIGGY_PARK_ATTRIBUTE] = park
        return "task parked for sign-in"

    loop.tools.execute = AsyncMock(side_effect=_execute)  # type: ignore[method-assign]


@pytest.fixture
def bus() -> MessageBus:
    return MessageBus()


@pytest.fixture
def collector(bus: MessageBus) -> _Collector:
    return _Collector(bus)


@pytest.mark.asyncio
async def test_parked_tool_result_ends_turn_waiting(
    tmp_path: Path,
    bus: MessageBus,
    collector: _Collector,
) -> None:
    loop = _make_loop(tmp_path, bus)
    task = loop.work_store.create_task(chat_id=CHAT_ID, content="log in to example.com")
    task_id = str(task["task_id"])
    collector.drain()
    collector.events.clear()

    _parking_tool(loop)

    await _run(loop, task_id)
    await asyncio.sleep(0)
    collector.drain()

    assert collector.types()[-1] == "status.changed"
    assert collector.events[-1]["payload"]["status"] == "waiting"
    assert (
        collector.events[-1]["payload"]["result_summary"]
        == "Waiting for a sign-in to example.com"
    )
    # No terminal status ever lands on the parked turn: a ``succeeded`` after
    # the ``waiting`` (or instead of it) would tell ziggy-work the job is done.
    assert collector.statuses() == ["running", "waiting"]
    assert loop.work_store.get_task(task_id)["status"] == "waiting"


@pytest.mark.asyncio
async def test_normal_turn_still_succeeds(
    tmp_path: Path,
    bus: MessageBus,
    collector: _Collector,
) -> None:
    """The park check is inert unless its own attribute is set.

    Negative control: the tool stamps an unrelated attribute (a phrasing the
    rule was not designed around) and must not affect the outcome."""
    loop = _make_loop(tmp_path, bus)
    task = loop.work_store.create_task(chat_id=CHAT_ID, content="log in to example.com")
    task_id = str(task["task_id"])
    collector.drain()
    collector.events.clear()

    _parking_tool(loop, park=None)
    original = loop.tools.execute

    async def _execute_unrelated(tool: Any, arguments: dict[str, Any], *a: Any, **kw: Any) -> str:
        result = await original(tool, arguments, *a, **kw)
        ctx = _CURRENT_REQUEST_CONTEXT.get()
        assert ctx is not None
        ctx.attributes["some_other_tool_signal"] = {"note": "not a park"}
        return result

    loop.tools.execute = AsyncMock(side_effect=_execute_unrelated)  # type: ignore[method-assign]

    await _run(loop, task_id)
    await asyncio.sleep(0)
    collector.drain()

    assert collector.statuses() == ["running", "succeeded"]
    assert loop.work_store.get_task(task_id)["status"] == "succeeded"


def test_followup_on_waiting_task_runs(tmp_path: Path) -> None:
    """Store-level half of the contract: ``waiting`` is re-entrant.

    The follow-up turn's ``_WorkHook.before_iteration`` unconditionally
    re-records ``running``; the store must let that through from ``waiting``,
    unlike a terminal status, which refuses every later write."""
    store = WorkStore(tmp_path)
    task = store.create_task(chat_id=CHAT_ID, content="wait then continue")
    task_id = str(task["task_id"])

    waiting = store.update_status(
        task_id, "waiting", result_summary="Waiting for a sign-in to example.com"
    )
    assert waiting is not None
    assert waiting.type == "status.changed"
    assert store.get_task(task_id)["status"] == "waiting"

    running = store.update_status(task_id, "running")
    assert running is not None
    assert running.type == "status.changed"
    assert store.get_task(task_id)["status"] == "running"

    # Contrast: terminal statuses are a one-way door.
    assert store.update_status(task_id, "succeeded") is not None
    assert store.update_status(task_id, "running") is None
    assert store.get_task(task_id)["status"] == "succeeded"
