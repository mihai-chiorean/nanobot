"""Shared-room history must never be consolidated into the owner's memory (MIT-1400).

Production (``feat/shared-rooms``, ``nanobot/agent/loop.py`` 1635/1806/1818)
never consolidates or raw-archives a shared-room session, so guest-authored
text never reaches the owner's memory. On 0.3.0 the runner was still handed
``consolidate_history`` for room turns, and ``Consolidator.archive`` wrote the
summary into the owner's ``memory/history.jsonl`` (read by Dream and
``MEMORY.md``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import GenerationSettings, LLMResponse

ROOM_CHAT = "11111111-2222-3333-4444-555555555555"
OWNER_CHAT = "chat_owner"
REPLY = "Short reply."
SUMMARY = "Guest said the secret launch code is 1234."
# Each seeded message is ~1.5k tokens, so twenty-four of them are well over the
# 16k window while any one of them (or a summary) fits.
FILLER = " ".join(["context"] * 1_500)


def _provider() -> MagicMock:
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = GenerationSettings(max_tokens=1_000)
    provider.can_resume_conversation_state.return_value = False

    async def _chat(**kwargs: Any) -> LLMResponse:
        messages = kwargs.get("messages") or []
        system = str(messages[0].get("content", "")) if messages else ""
        # The memory archiver asks for a summary; the turn asks for a reply.
        if "summar" in system.lower() or "archiv" in system.lower():
            return LLMResponse(content=SUMMARY, finish_reason="stop")
        return LLMResponse(content=REPLY, finish_reason="stop")

    provider.chat_with_retry = AsyncMock(side_effect=_chat)
    provider.chat_stream_with_retry = AsyncMock(side_effect=_chat)
    # No provider counter: the loop falls back to its real token estimator.
    provider.estimate_prompt_tokens = None
    return provider


def _loop(workspace: Path) -> AgentLoop:
    return AgentLoop(
        bus=MessageBus(),
        provider=_provider(),
        workspace=workspace,
        model="test-model",
        context_window_tokens=16_000,
    )


def _seed_history(loop: AgentLoop, key: str) -> None:
    session = loop.sessions.get_or_create(key)
    for i in range(12):
        session.add_message("user", f"guest message {i}: {FILLER}")
        session.add_message("assistant", f"reply {i}: {FILLER}")
    loop.sessions.save(session)


def _history_rows(loop: AgentLoop) -> list[str]:
    history = loop.consolidator.store.history_file
    if not history.exists():
        return []
    return [line for line in history.read_text().splitlines() if line.strip()]


def _spy(loop: AgentLoop) -> tuple[MagicMock, MagicMock]:
    summarize = MagicMock(wraps=loop.consolidator.summarize_transcript)
    archive = MagicMock(wraps=loop.consolidator.archiver.archive)

    async def _summarize(*args: Any, **kwargs: Any) -> Any:
        return await summarize(*args, **kwargs)

    async def _archive(*args: Any, **kwargs: Any) -> Any:
        return await archive(*args, **kwargs)

    loop.consolidator.summarize_transcript = _summarize  # type: ignore[method-assign]
    loop.consolidator.summarize_provider_compaction = _summarize  # type: ignore[method-assign]
    loop.consolidator.archiver.archive = _archive  # type: ignore[method-assign]
    return summarize, archive


@pytest.mark.asyncio
async def test_an_over_budget_room_turn_is_never_consolidated(tmp_path: Path) -> None:
    loop = _loop(tmp_path)
    key = f"websocket:{ROOM_CHAT}"
    _seed_history(loop, key)
    summarize, archive = _spy(loop)
    rows_before = _history_rows(loop)

    reply = await loop._process_message(InboundMessage(
        channel="websocket",
        sender_id="participant_" + "e" * 32,
        chat_id=ROOM_CHAT,
        content="one more guest message",
        metadata={"shared_room": True, "participant_display_name": "Guest"},
    ))

    assert reply is not None
    assert reply.content == REPLY
    summarize.assert_not_called()
    archive.assert_not_called()
    assert _history_rows(loop) == rows_before
    assert SUMMARY not in "\n".join(_history_rows(loop))


@pytest.mark.asyncio
async def test_an_over_budget_owner_turn_still_consolidates(tmp_path: Path) -> None:
    loop = _loop(tmp_path)
    key = f"websocket:{OWNER_CHAT}"
    _seed_history(loop, key)
    summarize, archive = _spy(loop)
    rows_before = _history_rows(loop)

    reply = await loop._process_message(InboundMessage(
        channel="websocket",
        sender_id="owner",
        chat_id=OWNER_CHAT,
        content="one more owner message",
    ))

    assert reply is not None
    summarize.assert_called()
    archive.assert_called()
    assert len(_history_rows(loop)) > len(rows_before)
