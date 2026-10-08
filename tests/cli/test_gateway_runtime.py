"""Regression tests for gateway runtime resource teardown on stop.

Covers the lifecycle contract of ``_close_gateway_runtime``: runtime tasks
(including the agent loop and in-flight turns) are cancelled and awaited --
bounded -- before exec sessions, subagents, and MCP servers are closed, the
close is deterministic and idempotent, and a stuck or failing cleanup cannot
block the stop.
"""

import asyncio
import time
from contextlib import suppress
from typing import Any

import pytest

from nanobot.agent.hook import AgentRunHookContext
from nanobot.agent.loop import AgentLoop
from nanobot.agent.tools.mcp import MCPProvider
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.cli.gateway_runtime import (
    _close_gateway_runtime,
    _gateway_readiness_payload,
    _MCPReadinessHook,
)


class _FakeAgent:
    def __init__(self, events: list[str] | None = None) -> None:
        self.close_calls = 0
        self.events = events if events is not None else []
        self.hang_on_close = False
        self.raise_on_close = False
        self.background: asyncio.Task[None] | None = None

    async def aclose(self) -> None:
        self.close_calls += 1
        if self.hang_on_close:
            await asyncio.sleep(3600)
        if self.raise_on_close:
            raise RuntimeError("cleanup exploded")
        if self.background is not None:
            await self.background
        self.events.append("aclose")


class _FakeChannels:
    def __init__(self) -> None:
        self.stopped = 0
        self.events: list[str] = []

    async def stop_all(self) -> None:
        self.stopped += 1
        self.events.append("channels_stopped")


class _FakeMCPProvider:
    def __init__(self, events: list[str] | None = None) -> None:
        self.close_calls = 0
        self.events = events if events is not None else []

    async def aclose(self) -> None:
        self.close_calls += 1
        self.events.append("mcp_closed")


class _TrackingMCPProvider(MCPProvider):
    def __init__(self) -> None:
        super().__init__({}, ToolRegistry())
        self.connect_calls = 0

    async def connect(self) -> None:
        self.connect_calls += 1


def test_gateway_readiness_is_degraded_when_required_websocket_is_unavailable() -> None:
    channels = type(
        "Channels",
        (),
        {
            "enabled_channels": ["websocket"],
            "get_status": lambda self: {
                "websocket": {
                    "enabled": True,
                    "running": False,
                    "state": "starting",
                }
            },
        },
    )()

    ready, payload = _gateway_readiness_payload(channels)

    assert ready is False
    assert payload == {
        "status": "degraded",
        "process": "alive",
        "ready": False,
        "websocket": "starting",
    }


# MIT-1804: /health carries a bare active_turns count for the idle-restart
# deploy (design doc "Safe retries and scheduled work" §1). The field is an
# integer only -- no session keys or tenant data -- and never changes ready.


def _ready_channels() -> Any:
    return type("Channels", (), {"enabled_channels": [], "get_status": None})()


def test_health_payload_reports_active_turns() -> None:
    ready, payload = _gateway_readiness_payload(_ready_channels(), lambda: 2)

    assert ready is True
    assert payload["active_turns"] == 2
    assert isinstance(payload["active_turns"], int)


def test_health_payload_active_turns_error_reports_minus_one() -> None:
    def _boom() -> int:
        raise RuntimeError("counter exploded")

    ready, payload = _gateway_readiness_payload(_ready_channels(), _boom)

    assert ready is True
    assert payload["active_turns"] == -1


def test_health_payload_without_counter_has_no_field() -> None:
    ready, payload = _gateway_readiness_payload(_ready_channels())

    assert ready is True
    assert "active_turns" not in payload


class _FakeRuntimeEventPublisher:
    def __init__(self) -> None:
        self.idle_calls = 0

    async def run_status_changed(self, *_args: Any, **_kwargs: Any) -> None:
        self.idle_calls += 1

    def clear_turn(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def _bare_loop() -> AgentLoop:
    loop = AgentLoop.__new__(AgentLoop)
    loop._active_tasks = {}
    loop._direct_turn_count = 0
    loop._session_locks = {}
    loop.runtime_event_publisher = _FakeRuntimeEventPublisher()
    return loop


async def test_active_turn_count_counts_undone_tasks_across_sessions() -> None:
    loop = _bare_loop()

    async def _pend() -> None:
        await asyncio.Event().wait()

    async def _finish() -> None:
        return None

    pending_a = asyncio.create_task(_pend())
    pending_b = asyncio.create_task(_pend())
    finished = asyncio.create_task(_finish())
    await finished
    loop._active_tasks = {
        "websocket:one": {pending_a, finished},
        "websocket:two": {pending_b},
    }

    assert loop.active_turn_count() == 2

    for task in (pending_a, pending_b):
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


async def test_process_direct_turn_in_flight_counts_as_one() -> None:
    loop = _bare_loop()
    seen: list[int] = []

    async def _process_message(_msg: Any, **_kwargs: Any) -> None:
        seen.append(loop.active_turn_count())
        return None

    loop._process_message = _process_message  # type: ignore[method-assign]

    assert loop.active_turn_count() == 0
    await loop.process_direct("hi")

    assert seen == [1]
    assert loop.active_turn_count() == 0


async def test_process_direct_counter_drops_when_turn_raises() -> None:
    loop = _bare_loop()
    seen: list[int] = []

    async def _process_message(_msg: Any, **_kwargs: Any) -> None:
        seen.append(loop.active_turn_count())
        raise RuntimeError("turn exploded")

    loop._process_message = _process_message  # type: ignore[method-assign]

    with pytest.raises(RuntimeError):
        await loop.process_direct("hi")

    assert seen == [1]
    assert loop.active_turn_count() == 0


async def test_mcp_readiness_hook_delegates_to_application_provider() -> None:
    provider = _TrackingMCPProvider()
    hook = _MCPReadinessHook(provider)

    await hook.before_run(AgentRunHookContext(messages=[]))

    assert provider.connect_calls == 1


async def _cancellable_task(events: list[str]) -> None:
    try:
        await asyncio.sleep(3600)
    except asyncio.CancelledError:
        events.append("cancelled")
        raise


async def _stubborn_task(events: list[str]) -> None:
    """Task that swallows cancellation and keeps running."""
    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        events.append("swallowed")
        await asyncio.sleep(3600)


async def test_runtime_tasks_cancelled_before_resources_closed() -> None:
    events: list[str] = []
    agent = _FakeAgent(events)
    provider = _FakeMCPProvider(events)
    channels = _FakeChannels()
    task = asyncio.create_task(_cancellable_task(events))
    await asyncio.sleep(0)  # let the task start (cancellation pre-start skips its body)

    await _close_gateway_runtime(agent, provider, channels, [task], None)

    assert events == ["cancelled", "aclose", "mcp_closed"]
    assert channels.stopped == 1
    assert agent.close_calls == 1
    assert task.cancelled()


async def test_pending_background_work_is_drained_before_close_returns() -> None:
    agent = _FakeAgent()
    provider = _FakeMCPProvider()
    channels = _FakeChannels()
    done: dict[str, bool] = {"done": False}

    async def background_work() -> None:
        await asyncio.sleep(0.01)
        done["done"] = True

    agent.background = asyncio.create_task(background_work())

    await _close_gateway_runtime(agent, provider, channels, [], None)

    assert done["done"] is True
    assert agent.close_calls == 1


async def test_stubborn_task_does_not_block_past_wait_timeout() -> None:
    agent = _FakeAgent()
    provider = _FakeMCPProvider()
    channels = _FakeChannels()
    events: list[str] = []
    task = asyncio.create_task(_stubborn_task(events))
    await asyncio.sleep(0)  # let the task start (cancellation pre-start skips its body)
    runtime_tasks = asyncio.gather(task)

    start = time.monotonic()
    await _close_gateway_runtime(
        agent,
        provider,
        channels,
        [task],
        runtime_tasks,
        task_wait_timeout=0.05,
    )
    elapsed = time.monotonic() - start
    for _ in range(10):
        await asyncio.sleep(0)  # let the swallowed cancellation handler run

    assert "swallowed" in events  # task was cancelled, then refused to die
    assert task.done()  # the timed-out task received a second cancellation
    assert runtime_tasks.done()
    assert agent.close_calls == 1  # resources still closed underneath it
    assert provider.close_calls == 1
    assert elapsed < 1.0  # bounded, not held open by the stubborn task


async def test_hanging_close_is_bounded_and_does_not_raise() -> None:
    agent = _FakeAgent()
    provider = _FakeMCPProvider()
    agent.hang_on_close = True
    channels = _FakeChannels()

    start = time.monotonic()
    await _close_gateway_runtime(
        agent,
        provider,
        channels,
        [],
        None,
        close_timeout=0.05,
    )
    elapsed = time.monotonic() - start

    assert agent.close_calls == 1
    assert provider.close_calls == 1
    assert channels.stopped == 1
    assert elapsed < 1.0


async def test_failing_close_is_logged_but_shutdown_proceeds() -> None:
    agent = _FakeAgent()
    provider = _FakeMCPProvider()
    agent.raise_on_close = True
    channels = _FakeChannels()

    await _close_gateway_runtime(agent, provider, channels, [], None)

    assert agent.close_calls == 1
    assert provider.close_calls == 1
    assert channels.stopped == 1  # teardown continued past the failure


async def test_duplicate_cleanup_is_idempotent() -> None:
    agent = _FakeAgent()
    provider = _FakeMCPProvider()
    channels = _FakeChannels()
    task = asyncio.create_task(_cancellable_task([]))

    await _close_gateway_runtime(agent, provider, channels, [task], None)
    await _close_gateway_runtime(agent, provider, channels, [task], None)

    assert agent.close_calls == 2  # second pass is a clean no-op
    assert provider.close_calls == 2
    assert channels.stopped == 2
    assert task.cancelled()


async def test_finished_runtime_tasks_gather_is_retrieved() -> None:
    agent = _FakeAgent()
    provider = _FakeMCPProvider()
    channels = _FakeChannels()
    finished = asyncio.get_running_loop().create_future()
    finished.set_result(None)
    runtime_tasks = asyncio.gather(finished)
    await asyncio.sleep(0)  # let the gather observe the finished child

    await _close_gateway_runtime(agent, provider, channels, [], runtime_tasks)

    assert runtime_tasks.done()
    assert agent.close_calls == 1
    assert provider.close_calls == 1


async def test_cancelled_runtime_tasks_gather_does_not_raise() -> None:
    agent = _FakeAgent()
    provider = _FakeMCPProvider()
    channels = _FakeChannels()
    runtime_tasks = asyncio.gather(asyncio.sleep(3600))
    runtime_tasks.cancel()

    await _close_gateway_runtime(agent, provider, channels, [], runtime_tasks)
    with suppress(asyncio.CancelledError):
        await runtime_tasks  # settle the cancelled gather without raising

    assert runtime_tasks.done()  # the cancelled gather was awaited without raising
    assert agent.close_calls == 1
    assert provider.close_calls == 1


async def test_dream_cron_job_records_provenance_off_the_event_loop(tmp_path) -> None:
    """Coverage asked for in the MIT-1441 re-review (MIT-1591): the cron Dream
    call site must hand the blocking provenance write to a worker thread.
    Pinning the fake's executing thread downgrades a reverted
    ``asyncio.to_thread`` into a red test instead of a silent pass.

    Drives the real ``_run_dream_cron_job`` (the extracted body of the
    on_cron_job dream branch) with a real MemoryStore so the finally-path
    (commit check, compact, prune) also runs for real."""
    import threading
    from types import SimpleNamespace

    from nanobot.agent.memory import MemoryStore
    from nanobot.bus.events import OutboundMessage
    from nanobot.cli.gateway_runtime import _run_dream_cron_job
    from nanobot.session.manager import SessionManager

    workspace = tmp_path / "ws"
    workspace.mkdir()
    store = MemoryStore(workspace)
    batch = [{"cursor": 42, "content": "consolidated entry 42", "session_key": "cli:direct"}]
    store.build_dream_prompt = lambda: ("dream prompt", 42, batch)
    store.dream_content_diff = lambda: "memory/MEMORY.md: +1 -0"
    seen: dict = {}

    def record(diff_body, provenance_batch):
        seen["thread"] = threading.current_thread()
        seen["args"] = (diff_body, provenance_batch)

    store.record_dream_provenance = record

    async def process_direct(prompt, **kwargs):
        return OutboundMessage(
            channel="cli",
            chat_id="direct",
            content="done",
            metadata={"_stop_reason": "completed"},
        )

    from nanobot.config.schema import ToolsConfig

    agent = SimpleNamespace(
        context=SimpleNamespace(memory=store),
        dream_runtime=lambda: None,
        process_direct=process_direct,
        sessions=SessionManager(workspace, sessions_root=tmp_path / "sessions"),
        tools_config=ToolsConfig(),
    )

    class _Provider:
        def __init__(self) -> None:
            self.connects = 0

        async def connect(self) -> None:
            self.connects += 1

    provider = _Provider()

    loop_thread = threading.current_thread()  # this coroutine runs on the loop thread
    await _run_dream_cron_job(agent, provider)

    assert provider.connects == 1
    assert store.get_last_dream_cursor() == 42  # completed run advanced the cursor
    assert seen["thread"] is not None
    assert seen["thread"] is not loop_thread, "provenance ran on the event-loop thread"
    assert seen["thread"] is not threading.main_thread()
    assert seen["args"] == ("memory/MEMORY.md: +1 -0", batch)
