"""A Work run, end to end in-process (Ziggy-local, MIT-1010).

The channel-side contract is pinned in ``tests/channels/test_work_event_stream.py``.
What this file covers is the producing half: one real ``AgentLoop`` turn stamped
with ``work_task_id`` must write the ordered ``work_events`` rows the Work app
renders *and* publish each of them onto the outbound bus, because nothing else
tells a subscriber the run is progressing.

The terminal ``status.changed`` is the load-bearing assertion.  ``services/
ziggy-work``'s River executor returns from its job when it sees a terminal or
``waiting`` status (``internal/executor/executor.go:248``); a turn that finishes
without one leaves the job blocked until its own timeout.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.agent.loop import AgentLoop, work_task_id
from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import LLMResponse, ToolCallRequest

CHAT_ID = "99999999-8888-7777-6666-555555555555"


class _Collector:
    """Drain the Work events the loop published onto the outbound queue."""

    def __init__(self, bus: MessageBus) -> None:
        self._bus = bus
        self.events: list[dict[str, Any]] = []
        self.other: list[OutboundMessage] = []

    def drain(self) -> None:
        while True:
            try:
                msg = self._bus.outbound.get_nowait()
            except asyncio.QueueEmpty:
                return
            payload = msg.metadata.get("_work_event")
            if payload is None:
                self.other.append(msg)
                continue
            assert msg.channel == "websocket"
            assert msg.chat_id == CHAT_ID
            # A Work event is not a chat message; the channel demultiplexes it
            # before the chat projection, and the empty content is what makes
            # a channel that knows nothing about Work render nothing.
            assert msg.content == ""
            self.events.append(payload)

    def types(self) -> list[str]:
        return [e["type"] for e in self.events]


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
    content: str = "summarize the inbox",
) -> Any:
    return await loop.process_direct(
        content,
        session_key=f"work:{task_id}",
        channel="websocket",
        chat_id=CHAT_ID,
        metadata={"work_task_id": task_id, "work_mode": "background"},
    )


@pytest.fixture
def bus() -> MessageBus:
    return MessageBus()


@pytest.fixture
def collector(bus: MessageBus) -> _Collector:
    return _Collector(bus)


def test_work_task_id_only_accepts_a_work_prefixed_string() -> None:
    assert work_task_id({"work_task_id": "work_" + "a" * 32}) == "work_" + "a" * 32
    assert work_task_id({"work_task_id": "cron_abc"}) is None
    assert work_task_id({"work_task_id": 7}) is None
    assert work_task_id(None) is None
    assert work_task_id({}) is None


@pytest.mark.asyncio
async def test_a_work_run_emits_an_ordered_event_stream_and_ends_terminal(
    tmp_path: Path,
    bus: MessageBus,
    collector: _Collector,
) -> None:
    loop = _make_loop(tmp_path, bus)
    task = loop.work_store.create_task(chat_id=CHAT_ID, content="summarize the inbox")
    task_id = str(task["task_id"])
    collector.drain()
    collector.events.clear()

    tool_call = ToolCallRequest(id="call1", name="gmail_search", arguments={"q": "is:unread"})
    _scripted(
        loop,
        [
            LLMResponse(content="Looking", tool_calls=[tool_call]),
            LLMResponse(content="Here is the digest.", tool_calls=[]),
        ],
    )
    loop.tools.prepare_call = MagicMock(  # type: ignore[method-assign]
        return_value=(None, {"q": "is:unread"}, None)
    )
    loop.tools.execute = AsyncMock(return_value="3 messages")  # type: ignore[method-assign]

    await _run(loop, task_id)
    await asyncio.sleep(0)
    collector.drain()

    types = collector.types()
    assert types[0] == "status.changed"
    assert "tool.started" in types
    assert "tool.finished" in types
    assert types[-1] == "status.changed"

    # Every event is published exactly once and in seq order, because the
    # clients resume from the highest seq they have seen.
    seqs = [e["seq"] for e in collector.events]
    assert seqs == sorted(seqs)
    assert len(seqs) == len(set(seqs))

    started = next(e for e in collector.events if e["type"] == "tool.started")
    assert started["payload"] == {"name": "gmail_search", "arguments": {"q": "is:unread"}}
    assert started["actor"] == "main_agent"

    assert collector.events[0]["payload"]["status"] == "running"
    assert collector.events[-1]["payload"]["status"] == "succeeded"
    assert loop.work_store.get_task(task_id)["status"] == "succeeded"

    # The durable log matches what was pushed, so a late subscriber replaying
    # from seq 0 sees the same stream a live one did.
    persisted = loop.work_store.list_events(task_id, after_seq=0)
    assert [e["seq"] for e in persisted if e["seq"] in set(seqs)] == seqs


@pytest.mark.asyncio
async def test_report_progress_and_publish_artifact_reach_the_stream(
    tmp_path: Path,
    bus: MessageBus,
    collector: _Collector,
) -> None:
    """The Work tools write rows mid-tool-call; the hook must replay them.

    Without the replay in ``after_iteration`` the step and artifact rows exist
    in SQLite but never reach a subscriber, which is exactly the "Work app
    stays dark" failure this port closes.
    """
    loop = _make_loop(tmp_path, bus)
    task = loop.work_store.create_task(chat_id=CHAT_ID, content="write the digest")
    task_id = str(task["task_id"])
    collector.drain()
    collector.events.clear()

    from nanobot.agent.tools.work import PublishArtifactTool, ReportProgressTool

    progress = ReportProgressTool()
    publish = PublishArtifactTool()
    tool_calls = [
        ToolCallRequest(id="c1", name="report_progress", arguments={"title": "Reading inbox"}),
        ToolCallRequest(
            id="c2",
            name="publish_artifact",
            arguments={"name": "digest.md", "kind": "markdown", "content": "# Digest"},
        ),
    ]
    _scripted(
        loop,
        [
            LLMResponse(content="Working", tool_calls=tool_calls),
            LLMResponse(content="Done.", tool_calls=[]),
        ],
    )
    loop.tools.prepare_call = MagicMock(  # type: ignore[method-assign]
        side_effect=lambda name, params, *a, **kw: (
            progress if name == "report_progress" else publish,
            params,
            None,
        )
    )

    async def _execute(tool: Any, arguments: dict[str, Any], *a: Any, **kw: Any) -> Any:
        return await tool.execute(**arguments)

    loop.tools.execute = AsyncMock(side_effect=_execute)  # type: ignore[method-assign]

    await _run(loop, task_id, content="write the digest")
    await asyncio.sleep(0)
    collector.drain()

    types = collector.types()
    assert "step.started" in types, types
    assert "artifact.created" in types, types
    artifact_event = next(e for e in collector.events if e["type"] == "artifact.created")
    assert artifact_event["payload"]["name"] == "digest.md"

    refs = loop.work_artifact_refs(
        MagicMock(metadata={"work_task_id": task_id}, channel="websocket", chat_id=CHAT_ID)
    )
    assert [r["name"] for r in refs] == ["digest.md"]


@pytest.mark.asyncio
async def test_an_ordinary_turn_writes_no_work_rows(
    tmp_path: Path,
    bus: MessageBus,
    collector: _Collector,
) -> None:
    """Non-vacuity, and the cost argument for the hook factory.

    A turn without ``work_task_id`` must build no Work hook, bind no Work
    context and publish nothing -- otherwise every chat turn would pay for a
    feature it is not using, and a nested run could write rows against an
    unrelated task.
    """
    loop = _make_loop(tmp_path, bus)
    _scripted(loop, [LLMResponse(content="Hello.", tool_calls=[])])

    await loop.process_direct(
        "hello",
        session_key=f"websocket:{CHAT_ID}",
        channel="websocket",
        chat_id=CHAT_ID,
    )
    await asyncio.sleep(0)
    collector.drain()

    assert collector.events == []
    assert loop.work_store.list_tasks() == []


@pytest.mark.asyncio
async def test_a_failing_work_run_still_publishes_a_terminal_status(
    tmp_path: Path,
    bus: MessageBus,
    collector: _Collector,
) -> None:
    loop = _make_loop(tmp_path, bus)
    task = loop.work_store.create_task(chat_id=CHAT_ID, content="explode")
    task_id = str(task["task_id"])
    collector.drain()
    collector.events.clear()

    loop.provider.chat_stream_with_retry = AsyncMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("provider is down")
    )
    loop.tools.get_definitions = MagicMock(return_value=[])  # type: ignore[method-assign]

    # Go through _dispatch, not process_direct: a work.create task arrives on
    # the bus, and _dispatch is where a failed turn is caught and recorded.
    await loop._dispatch(  # pyright: ignore[reportPrivateUsage]
        InboundMessage(
            channel="websocket",
            sender_id="client-1",
            chat_id=CHAT_ID,
            content="explode",
            metadata={"work_task_id": task_id, "work_mode": "background"},
            session_key_override=f"work:{task_id}",
        )
    )
    await asyncio.sleep(0)
    collector.drain()

    assert collector.types()[-1] == "status.changed"
    assert collector.events[-1]["payload"]["status"] == "failed"
    assert loop.work_store.get_task(task_id)["status"] == "failed"


@pytest.mark.asyncio
async def test_the_restart_sweep_cannot_interrupt_a_task_this_process_accepted(
    tmp_path: Path,
    bus: MessageBus,
) -> None:
    """Regression, in production order.

    The gateway constructs the channel (which inserts tasks) and the agent loop
    as peers, and the loop's store brings its schema up lazily. When the sweep
    rode along with that first open it fired at first *agent* use -- after the
    task existed -- and marked the live task interrupted. The executor sees a
    terminal status.changed, returns, and the task is lost. Once per restart,
    deterministically, on the first task submitted after a deploy.
    """
    # The channel's handle inserts a task before the agent loop has run anything.
    from nanobot.work.store import WorkStore

    reader = WorkStore(tmp_path, reconcile_on_open=False)
    task_id = str(reader.create_task(chat_id=CHAT_ID, content="long job")["task_id"])

    loop = _make_loop(tmp_path, bus)
    # First touch of the loop's own handle.
    assert loop.work_store.get_task(task_id)["status"] == "queued"

    _scripted(loop, [LLMResponse(content="Done.", tool_calls=[])])
    await _run(loop, task_id, content="long job")
    assert loop.work_store.get_task(task_id)["status"] == "succeeded"


@pytest.mark.asyncio
async def test_the_sweep_still_runs_when_startup_asks_for_it(
    tmp_path: Path,
    bus: MessageBus,
) -> None:
    """Non-vacuity for the test above: the sweep is moved, not removed."""
    loop = _make_loop(tmp_path, bus)
    task_id = str(loop.work_store.create_task(chat_id=CHAT_ID, content="orphan")["task_id"])
    loop.work_store.update_status(task_id, "running")

    assert await loop.reconcile_work_store() == 1
    assert loop.work_store.get_task(task_id)["status"] == "interrupted"


@pytest.mark.asyncio
async def test_a_work_task_refused_by_the_owner_guard_still_ends_terminal(
    tmp_path: Path,
    bus: MessageBus,
    collector: _Collector,
) -> None:
    """A Work turn's sender is the creating client, never the owner, so the
    system-modification guard always refuses it. Before, that exit published no
    status at all and the executor blocked until its read deadline."""
    loop = _make_loop(tmp_path, bus)
    task = loop.work_store.create_task(chat_id=CHAT_ID, content="x")
    task_id = str(task["task_id"])
    collector.drain()
    collector.events.clear()

    from nanobot.agent.loop import is_system_modification

    content = "modify your code"
    assert is_system_modification(content), "guard no longer trips on this phrasing"

    await loop._dispatch(  # pyright: ignore[reportPrivateUsage]
        InboundMessage(
            channel="websocket",
            sender_id="ziggy-work",
            chat_id=CHAT_ID,
            content=content,
            metadata={"work_task_id": task_id, "work_mode": "background"},
            session_key_override=f"work:{task_id}",
        )
    )
    await asyncio.sleep(0)
    collector.drain()

    assert collector.types()[-1] == "status.changed"
    assert collector.events[-1]["payload"]["status"] == "failed"
    assert loop.work_store.get_task(task_id)["status"] == "failed"
