"""MIT-1811: shutdown-grace tests for the agent loop drain.

Covers ``AgentLoop.begin_drain``/``wait_idle`` and the dispatch-loop gate:
on the first SIGTERM the gateway stops admitting new turns, in-flight turns
run to completion inside the grace, a turn outliving the grace is cancelled
like today, and a message arriving while draining is held unprocessed so the
next gateway replays it from ``ChatInboxStore``.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from nanobot.agent.loop import AgentLoop
from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.websocket.chat_inbox import ChatInboxStore


def _make_loop(tmp_path):
    """A real AgentLoop whose heavy collaborators are mocked."""
    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    with (
        patch("nanobot.agent.loop.ContextBuilder"),
        patch("nanobot.agent.loop.SessionManager"),
        patch("nanobot.agent.loop.SubagentManager") as mock_sub_mgr,
    ):
        mock_sub_mgr.return_value.cancel_by_session = AsyncMock(return_value=0)
        loop = AgentLoop(
            bus=bus,
            provider=provider,
            workspace=tmp_path,
            memory_index_enabled=False,
        )
    loop.auto_compact = SimpleNamespace(check_expired=lambda *args, **kwargs: None)
    loop.runtime_event_publisher = SimpleNamespace(
        user_input_accepted=AsyncMock(),
        run_status_changed=AsyncMock(),
        clear_turn=MagicMock(),
    )
    loop.subagents.close = AsyncMock()
    loop._exec_session_manager.close_all = AsyncMock()
    return loop, bus


class _FakeTurns:
    """Stand-in for ``_dispatch_one`` that records how each turn ended."""

    def __init__(self) -> None:
        self.started: list[str] = []
        self.completed: list[str] = []
        self.cancelled: list[str] = []
        self.blocks: dict[str, asyncio.Event] = {}
        self.mark_processed = False

    def install(self, loop: AgentLoop) -> None:
        async def _dispatch_one(msg, pending_queue=None, **kwargs):
            content = msg.content
            self.started.append(content)
            block = self.blocks.get(content)
            try:
                if block is not None:
                    await block.wait()
                else:
                    await asyncio.sleep(1.0)
            except asyncio.CancelledError:
                self.cancelled.append(content)
                raise
            self.completed.append(content)
            if self.mark_processed:
                await loop._mark_chat_message_processed(msg)

        loop._dispatch_one = _dispatch_one

    async def wait_started(self, content: str) -> None:
        deadline = time.monotonic() + 2.0
        while content not in self.started:
            if time.monotonic() > deadline:
                raise AssertionError(f"turn {content!r} never started")
            await asyncio.sleep(0.01)


def _ws_message(content: str, client_message_id: str) -> InboundMessage:
    return InboundMessage(
        channel="websocket",
        sender_id="u1",
        chat_id="c1",
        content=content,
        metadata={"client_message_id": client_message_id},
    )


async def _stop_dispatch(loop: AgentLoop, run_task: asyncio.Task) -> None:
    loop.stop()
    run_task.cancel()
    await asyncio.gather(run_task, return_exceptions=True)


async def _await_active_turns(loop: AgentLoop, count: int) -> None:
    deadline = time.monotonic() + 2.0
    while loop.active_turn_count() != count:
        if time.monotonic() > deadline:
            raise AssertionError(
                f"active_turn_count never reached {count}: {loop.active_turn_count()}"
            )
        await asyncio.sleep(0.01)


async def test_turn_finishing_within_grace_completes_untouched(tmp_path) -> None:
    """A ~1 s fake turn under a 5 s grace completes instead of being cut off."""
    loop, bus = _make_loop(tmp_path)
    turns = _FakeTurns()
    turns.install(loop)
    run_task = asyncio.create_task(loop.run())
    try:
        await bus.publish_inbound(_ws_message("slow", "cm-slow"))
        await turns.wait_started("slow")
        await _await_active_turns(loop, 1)

        loop.begin_drain()
        assert loop.active_turn_count() == 1  # draining must not cancel anything

        start = time.monotonic()
        drained = await loop.wait_idle(5.0)
        elapsed = time.monotonic() - start

        assert drained is True
        assert elapsed < 4.5  # returned when the turn finished, not at the grace
        assert turns.completed == ["slow"]
        assert turns.cancelled == []  # never checkpointed as interrupted
        assert loop.active_turn_count() == 0
    finally:
        await _stop_dispatch(loop, run_task)


async def test_turn_outliving_grace_is_cancelled_and_preserved(tmp_path) -> None:
    """A turn that outlives the grace is cancelled on the unchanged shutdown path."""
    loop, bus = _make_loop(tmp_path)
    turns = _FakeTurns()
    turns.blocks["hang"] = asyncio.Event()  # never set: the turn outlives the grace
    turns.install(loop)
    run_task = asyncio.create_task(loop.run())
    try:
        await bus.publish_inbound(_ws_message("hang", "cm-hang"))
        await turns.wait_started("hang")
        await _await_active_turns(loop, 1)

        loop.begin_drain()
        start = time.monotonic()
        drained = await loop.wait_idle(1.0)
        elapsed = time.monotonic() - start

        assert drained is False
        assert 0.9 <= elapsed < 2.5  # returned at the timeout, not early
        assert turns.cancelled == []  # the grace itself cancelled nothing

        # Today's shutdown path from here: preserve checkpoints, stop, close.
        loop.preserve_inflight_turns_on_shutdown()
        loop.stop()
        await loop.aclose()

        assert turns.cancelled == ["hang"]  # the real cancellation reached it
        assert turns.completed == []
        assert loop.active_turn_count() == 0
        assert loop._preserve_inflight_turns_on_shutdown is True
    finally:
        await _stop_dispatch(loop, run_task)


async def test_message_arriving_after_begin_drain_stays_unprocessed(tmp_path) -> None:
    """Post-drain input is held, never dispatched, and its receipt stays recoverable."""
    loop, bus = _make_loop(tmp_path)
    inbox = ChatInboxStore(tmp_path)
    loop._chat_inbox = inbox
    turns = _FakeTurns()
    turns.mark_processed = True  # completes like the real post-turn receipt close
    turns.install(loop)
    run_task = asyncio.create_task(loop.run())
    try:
        # Control: a message admitted BEFORE the drain is processed and its
        # receipt leaves the recoverable set.
        pre = _ws_message("pre", "cm-pre")
        await inbox.accept(pre, "cm-pre")
        await inbox.claim_for_enqueue("c1", "cm-pre")
        await bus.publish_inbound(pre)
        await turns.wait_started("pre")
        deadline = time.monotonic() + 3.0
        while "pre" not in turns.completed:
            if time.monotonic() > deadline:
                raise AssertionError("control turn never completed")
            await asyncio.sleep(0.01)

        loop.begin_drain()

        late = _ws_message("late", "cm-late")
        await inbox.accept(late, "cm-late")
        await inbox.claim_for_enqueue("c1", "cm-late")
        await bus.publish_inbound(late)
        deadline = time.monotonic() + 3.0
        while not loop._held_messages:
            if time.monotonic() > deadline:
                raise AssertionError("late message was never held")
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)  # let any (wrong) dispatch attempt run

        assert [msg.content for msg in loop._held_messages] == ["late"]
        assert "late" not in turns.started  # no turn was started for it
        assert bus.inbound_size == 0  # held, not left dangling on the bus

        recoverable = {record.client_message_id for record in await inbox.recoverable()}
        assert "cm-late" in recoverable  # unprocessed: the next process replays it
        assert "cm-pre" not in recoverable  # control: processed receipts leave
        assert "late" not in turns.completed
    finally:
        await _stop_dispatch(loop, run_task)


async def test_wait_idle_returns_immediately_when_already_idle(tmp_path) -> None:
    loop, _bus = _make_loop(tmp_path)
    start = time.monotonic()
    assert await loop.wait_idle(5.0) is True
    assert time.monotonic() - start < 0.4


async def test_wait_idle_zero_timeout_reports_busy(tmp_path) -> None:
    loop, _bus = _make_loop(tmp_path)

    async def _turn() -> None:
        await asyncio.Event().wait()

    task = asyncio.create_task(_turn())
    await asyncio.sleep(0)
    loop._track_active_task("websocket:c1", task)
    try:
        start = time.monotonic()
        assert await loop.wait_idle(0.0) is False
        assert time.monotonic() - start < 0.4
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
