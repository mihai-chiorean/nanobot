from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.agent.loop import AgentLoop


@pytest.mark.asyncio
async def test_process_direct_registers_session_for_stop() -> None:
    loop = AgentLoop.__new__(AgentLoop)
    loop._active_tasks = {}
    loop._connect_mcp = AsyncMock()
    loop.subagents = MagicMock()
    loop.subagents.cancel_by_session = AsyncMock(return_value=0)
    started = asyncio.Event()

    async def process(*_args, **_kwargs):
        started.set()
        await asyncio.Event().wait()

    loop._process_message = process
    task = asyncio.create_task(
        loop.process_direct("run", session_key="cron:job-1")
    )
    await asyncio.wait_for(started.wait(), timeout=1)

    assert loop.active_session_keys() == {"cron:job-1"}
    assert await loop._cancel_active_tasks("cron:job-1") == 1
    assert task.cancelled()
    assert loop.active_session_keys() == set()


@pytest.mark.asyncio
async def test_process_direct_cleans_registration_after_success() -> None:
    loop = AgentLoop.__new__(AgentLoop)
    loop._active_tasks = {}
    loop._connect_mcp = AsyncMock()
    loop._process_message = AsyncMock(return_value=None)

    assert await loop.process_direct("run", session_key="cron:job-2") is None
    assert loop.active_session_keys() == set()
