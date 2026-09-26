"""The first room reply must survive the discussion-message merge (MIT-1465).

Production 0.2.x (``feat/shared-rooms``, commit 83028651) fixed a bug where
ContextBuilder merged a guest's current user message into the preceding
discussion message and the turn's save boundary was computed from the
pre-merge length, so the first assistant reply in the room was dropped from
the persisted session. 0.3.0 rewrote the turn pipeline (``_persist_turn``,
``prepare_save_boundary``), so this pins the behaviour on the new line: the
first guest reply after a merged discussion message is persisted in the room
session exactly once.
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
from nanobot.session.manager import SessionManager

ROOM_CHAT = "11111111-2222-3333-4444-555555555555"
OWNER_CHAT = "chat_owner"
GUEST = "participant_" + "e" * 32
DISCUSSION = "Plain reminder: the repo sync job finished."
QUESTION = "Can you confirm the sync result?"
REPLY = "Confirmed, the sync finished cleanly."


def _provider(requests: list[list[dict[str, Any]]]) -> MagicMock:
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = GenerationSettings(max_tokens=1_000)
    provider.can_resume_conversation_state.return_value = False

    async def _chat(**kwargs: Any) -> LLMResponse:
        requests.append([dict(m) for m in (kwargs.get("messages") or [])])
        return LLMResponse(content=REPLY, finish_reason="stop")

    provider.chat_with_retry = AsyncMock(side_effect=_chat)
    provider.chat_stream_with_retry = AsyncMock(side_effect=_chat)
    # No provider counter: the loop falls back to its real token estimator.
    provider.estimate_prompt_tokens = None
    return provider


def _loop(workspace: Path, requests: list[list[dict[str, Any]]]) -> AgentLoop:
    return AgentLoop(
        bus=MessageBus(),
        provider=_provider(requests),
        workspace=workspace,
        model="test-model",
        context_window_tokens=16_000,
    )


def _seed(session: SessionManager, key: str, messages: list[dict[str, Any]]) -> None:
    room = session.get_or_create(key)
    for message in messages:
        room.messages.append(dict(message))
    session.save(room)


def _persisted(workspace: Path, key: str) -> list[dict[str, Any]]:
    """Reload the session from disk, the way a later turn would see it."""
    return SessionManager(workspace).get_or_create(key).messages


def _count_containing(messages: list[dict[str, Any]], needle: str) -> int:
    return sum(1 for m in messages if needle in str(m.get("content", "")))


def _user_messages(request: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [m for m in request if m.get("role") == "user"]


async def _guest_ask(loop: AgentLoop, content: str) -> None:
    reply = await loop._process_message(InboundMessage(
        channel="websocket",
        sender_id=GUEST,
        chat_id=ROOM_CHAT,
        content=content,
        metadata={
            "shared_room": True,
            "participant_id": GUEST,
            "participant_display_name": "Guest",
            "room_intent": "ask_ziggy",
        },
    ))
    assert reply is not None
    assert reply.content == REPLY


@pytest.mark.asyncio
async def test_first_reply_after_a_merged_discussion_persists_once(tmp_path: Path) -> None:
    """A guest turn merged into the trailing discussion message must not eat the reply.

    The room history ends with a participant discussion message; the guest's
    ask_ziggy message is adjacent to it, so the model-facing copy merges the
    two user messages into one. The save boundary must be taken from the real
    saved transcript, not from the pre-merge assumption
    ``1 + len(history) + 1`` — otherwise the first assistant reply is cut
    from the persisted room session (the 0.2.x ``initial_message_count``
    bug, commit 83028651).
    """
    requests: list[list[dict[str, Any]]] = []
    loop = _loop(tmp_path, requests)
    key = f"websocket:{ROOM_CHAT}"
    _seed(loop.sessions, key, [
        {"role": "user", "content": DISCUSSION, "participant_id": GUEST,
         "participant_display_name": "Guest", "room_intent": "discussion"},
    ])

    await _guest_ask(loop, QUESTION)

    # Premise: the model saw the guest ask merged into the discussion
    # message — one user turn, both texts present. If this breaks, the
    # test below is no longer covering the merge path and must be retuned.
    assert requests, "the guest turn never reached the provider"
    merged_turns = _user_messages(requests[-1])
    assert len(merged_turns) == 1, (
        "expected the discussion + guest messages to reach the model merged"
    )
    assert DISCUSSION in str(merged_turns[0].get("content"))
    assert QUESTION in str(merged_turns[0].get("content"))

    messages = _persisted(tmp_path, key)
    assert _count_containing(messages, REPLY) == 1, (
        "the first room reply was dropped or duplicated by the save boundary"
    )
    # The raw pair stays in the transcript (the merge is model-facing only),
    # each exactly once — a boundary that skipped the merge tail would show
    # the reply missing here although the provider call succeeded.
    assert _count_containing(messages, DISCUSSION) == 1, (
        "the pre-merge boundary lost or duplicated the discussion tail"
    )
    assert _count_containing(messages, QUESTION) == 1, (
        "the guest ask was lost or persisted twice"
    )
    assert messages[-1]["role"] == "assistant"
    assert messages[-1]["content"] == REPLY


@pytest.mark.asyncio
async def test_reply_after_an_unmerged_room_turn_persists_once(tmp_path: Path) -> None:
    """Negative control: no adjacent user message to merge here.

    The history ends with an assistant message, so the guest turn is a plain
    append; the save boundary must still persist exactly the new reply. A
    fix that trimmed the tail whenever inputs were present would fail this
    control (and the main test above) rather than passing silently.
    """
    requests: list[list[dict[str, Any]]] = []
    loop = _loop(tmp_path, requests)
    key = f"websocket:{ROOM_CHAT}"
    _seed(loop.sessions, key, [
        {"role": "user", "content": DISCUSSION, "participant_id": GUEST,
         "participant_display_name": "Guest", "room_intent": "discussion"},
        {"role": "assistant", "content": "Noted."},
    ])

    await _guest_ask(loop, QUESTION)

    # Premise: nothing merged — the guest ask reached the model as its own
    # user turn (still attributed, but to no preceding message).
    assert requests
    user_turns = _user_messages(requests[-1])
    assert len(user_turns) == 2, (
        "the unmerged control unexpectedly collapsed the guest ask into the history"
    )
    assert DISCUSSION in str(user_turns[0].get("content"))
    assert QUESTION in str(user_turns[1].get("content"))

    messages = _persisted(tmp_path, key)
    assert _count_containing(messages, REPLY) == 1
    assert _count_containing(messages, DISCUSSION) == 1
    assert _count_containing(messages, QUESTION) == 1
    assert messages[-1]["role"] == "assistant"
    assert messages[-1]["content"] == REPLY


@pytest.mark.asyncio
async def test_owner_reply_after_a_merged_question_persists_once(tmp_path: Path) -> None:
    """Negative control on the owner copy: same merge shape, private session.

    The owner session ends with a user message and the next owner turn is
    adjacent to it, so the model-facing copy merges them; persistence must
    keep the earlier prompt and the reply exactly once each. The room fix
    must not have been smuggled into non-room turns, and ordinary turns
    must not lose replies to any merge-boundary trimming.
    """
    requests: list[list[dict[str, Any]]] = []
    loop = _loop(tmp_path, requests)
    key = f"websocket:{OWNER_CHAT}"
    _seed(loop.sessions, key, [
        {"role": "user", "content": "Draft the agenda."},
        {"role": "assistant", "content": "Drafted."},
        {"role": "user", "content": "Second item: budget."},
    ])

    reply = await loop._process_message(InboundMessage(
        channel="websocket",
        sender_id="owner",
        chat_id=OWNER_CHAT,
        content="Should the demos come before or after the recap?",
    ))
    assert reply is not None
    assert reply.content == REPLY

    assert requests
    merged_turns = _user_messages(requests[-1])
    assert len(merged_turns) == 2, (
        "the trailing owner prompt and the new turn must reach the model as "
        "the saved pair, merged into one user turn"
    )
    assert "Second item: budget." in str(merged_turns[-1].get("content"))
    assert "recap" in str(merged_turns[-1].get("content"))

    messages = _persisted(tmp_path, key)
    assert _count_containing(messages, REPLY) == 1
    assert _count_containing(messages, "Draft the agenda.") == 1
    assert _count_containing(messages, "budget") == 1
    assert _count_containing(messages, "recap") == 1
    assert messages[-1]["role"] == "assistant"
    assert messages[-1]["content"] == REPLY
