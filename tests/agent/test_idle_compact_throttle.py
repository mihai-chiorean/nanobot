"""MIT-1439: the idle-compaction sweep must not flood the model gateway.

After a restart every idle session is due at once. The sweep runs one archive
at a time, as ``background`` load, and a retryable rejection such as
``capacity_unavailable`` leaves the session for a later retry instead of
raw-archiving it.
"""

import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import LLMResponse
from nanobot.providers.request_context import current_scheduling_class

SESSIONS = 20


class _RecordingProvider:
    """Records peak concurrency and the scheduling class of each call."""

    def __init__(self, response_for=None) -> None:
        self.in_flight = 0
        self.peak = 0
        self.classes: list[str] = []
        self._response_for = response_for or (lambda _messages: LLMResponse(content="Summary."))

    async def __call__(self, **kwargs) -> LLMResponse:
        self.classes.append(current_scheduling_class())
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            await asyncio.sleep(0.01)
            return self._response_for(kwargs["messages"])
        finally:
            self.in_flight -= 1


def _make_loop(tmp_path: Path, stub: _RecordingProvider) -> AgentLoop:
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.estimate_prompt_tokens.return_value = (10_000, "test")
    provider.chat_stream_with_retry = stub
    provider.generation.max_tokens = 4096
    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        model="test-model",
        context_window_tokens=128_000,
        session_ttl_minutes=15,
    )
    loop.tools.get_definitions = MagicMock(return_value=[])
    return loop


def _seed_idle_sessions(loop: AgentLoop, count: int) -> list[str]:
    keys = []
    for i in range(count):
        key = f"cli:idle-{i}"
        session = loop.sessions.get_or_create(key)
        for turn in range(3):
            session.add_message("user", f"session {i} user {turn}")
            session.add_message("assistant", f"session {i} assistant {turn}")
        session.updated_at = datetime.now() - timedelta(hours=2)
        loop.sessions.save(session)
        keys.append(key)
    return keys


async def _sweep(loop: AgentLoop) -> None:
    loop._check_expired_sessions_if_due()
    tasks = list(loop._background_tasks)
    assert tasks, "the idle scan scheduled no archives"
    await asyncio.gather(*tasks)


@pytest.mark.asyncio
async def test_idle_sweep_runs_one_background_archive_at_a_time(tmp_path):
    stub = _RecordingProvider()
    loop = _make_loop(tmp_path, stub)
    keys = _seed_idle_sessions(loop, SESSIONS)

    await _sweep(loop)

    assert len(stub.classes) == SESSIONS
    assert stub.peak == 1
    assert set(stub.classes) == {"background"}
    # The class does not leak into the caller's context.
    assert current_scheduling_class() == "foreground"
    for key in keys:
        loop.sessions.invalidate(key)
        assert loop.sessions.get_or_create(key).last_archived > 0


@pytest.mark.asyncio
async def test_capacity_rejection_leaves_the_session_for_retry(tmp_path):
    rejected = "cli:idle-3"

    def respond(messages):
        if any("session 3 user" in str(m.get("content")) for m in messages):
            return LLMResponse(
                content="error: capacity_unavailable",
                finish_reason="error",
                error_status_code=503,
                error_code="capacity_unavailable",
            )
        return LLMResponse(content="Summary.")

    stub = _RecordingProvider(respond)
    loop = _make_loop(tmp_path, stub)
    _seed_idle_sessions(loop, SESSIONS)

    await _sweep(loop)

    loop.sessions.invalidate(rejected)
    session = loop.sessions.get_or_create(rejected)
    assert session.last_archived == 0
    assert len(session.messages) == 6
    history = loop.context.memory.history_file
    text = history.read_text(encoding="utf-8") if history.exists() else ""
    assert "[RAW]" not in text
    assert "session 3 user" not in text
