"""TP-02 (MIT-1853): the turn provenance record, effective_turn_id, provenance_v1.

Design: ``docs/design/turn-provenance.md`` sections 1, 2 and 7. Each turn
builds one ``TurnProvenance`` at turn start, fills it as it runs, and the
save stage appends ``to_dict()`` to ``session.metadata["provenance_v1"]``,
bounded like ``activity_v1``.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.session_helpers import run_session
from nanobot.agent.loop import AgentLoop, _effective_turn_id
from nanobot.agent.tools.base import Tool
from nanobot.agent.turn_provenance import (
    PROVENANCE_KEY,
    TurnProvenance,
    bind_turn_provenance,
    current_turn_provenance,
    reset_turn_provenance,
    save_to_session,
)
from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import (
    GenerationSettings,
    LLMResponse,
    ToolCallRequest,
)
from nanobot.webui.metadata import WEBUI_TURN_METADATA_KEY

_SEARCH_SCHEMA = {
    "type": "object",
    "properties": {"query": {"type": "string"}, "count": {"type": "integer"}},
    "required": ["query"],
}


class _SearchTool(Tool):
    """Tool whose ``count`` is an integer, so a string arg is a silent repair."""

    @property
    def name(self) -> str:
        return "search"

    @property
    def description(self) -> str:
        return "search"

    @property
    def parameters(self) -> dict[str, Any]:
        return _SEARCH_SCHEMA

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, **kwargs: Any) -> Any:
        return "ok"


def _make_loop(
    tmp_path: Path,
    respond: Any,
) -> tuple[AgentLoop, MessageBus]:
    """A loop whose provider answers every model call via ``respond()``.

    A fresh response per call (not a fixed iterator): the runner's bounded
    empty-response retries and multi-round turns must never run the
    provider dry.
    """
    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = GenerationSettings()
    provider.chat_stream_with_retry = AsyncMock(side_effect=lambda *a, **kw: respond())
    loop = AgentLoop(bus=bus, provider=provider, workspace=tmp_path, model="test-model")
    return loop, bus


def _msg(
    content: str = "what is the capital of France",
    metadata: dict[str, Any] | None = None,
) -> InboundMessage:
    return InboundMessage(
        channel="websocket",
        sender_id="u1",
        chat_id="chat1",
        content=content,
        metadata=dict(metadata or {}),
    )


async def _run_turn(
    loop: AgentLoop,
    bus: MessageBus,
    *,
    content: str = "what is the capital of France",
    metadata: dict[str, Any] | None = None,
) -> None:
    await run_session(loop, _msg(content, metadata))
    while bus.outbound_size > 0:
        await bus.consume_outbound()


def _entries(loop: AgentLoop) -> list[dict[str, Any]]:
    persisted = loop.sessions.read_session_file("websocket:chat1")
    assert persisted is not None
    entries = (persisted.get("metadata") or {}).get(PROVENANCE_KEY)
    assert isinstance(entries, list)
    return entries


def _answer(text: str = "Paris.") -> LLMResponse:
    return LLMResponse(content=text, tool_calls=[], usage=None)


class TestToDict:
    def test_schema_tag_and_defaults(self):
        entry = TurnProvenance().to_dict()
        assert entry["v"] == 1
        assert entry["answered"] is False
        assert entry["account_intent"] is False
        assert entry["prompt"] == []
        assert entry["prompt_rebuilt"] is False
        assert entry["skills_loaded"] == []
        assert entry["calls"] == []
        assert entry["args_repaired"] == {}
        assert entry["used"] == []
        assert entry["other_steps"] == 0

    def test_none_values_are_omitted(self):
        entry = TurnProvenance().to_dict()
        for absent in (
            "turn_id",
            "started_at",
            "source",
            "release",
            "model",
            "model_preset",
            "reasoning_profile",
            "skills_listed_sha",
        ):
            assert absent not in entry

    def test_filled_fields_are_kept(self):
        record = TurnProvenance(
            turn_id="t1",
            started_at="2026-10-08T00:00:00+00:00",
            source="user",
            answered=True,
            release="unknown",
            model="qwen3.6-35b",
            model_preset=None,
            reasoning_profile="fast",
            skills_listed_sha="abcd",
        )
        entry = record.to_dict()
        assert entry["turn_id"] == "t1"
        assert entry["source"] == "user"
        assert entry["answered"] is True
        assert entry["release"] == "unknown"
        assert entry["model"] == "qwen3.6-35b"
        assert "model_preset" not in entry
        assert entry["reasoning_profile"] == "fast"
        assert entry["skills_listed_sha"] == "abcd"


class TestContextvar:
    def test_default_none_bind_and_reset(self):
        assert current_turn_provenance() is None
        record = TurnProvenance(turn_id="t1")
        token = bind_turn_provenance(record)
        try:
            assert current_turn_provenance() is record
        finally:
            reset_turn_provenance(token)
        assert current_turn_provenance() is None


class TestSaveToSession:
    class _Session:
        def __init__(self) -> None:
            self.metadata: dict[str, Any] = {}

    def test_keeps_newest_cap(self):
        session = self._Session()
        for i in range(105):
            save_to_session(session, TurnProvenance(turn_id=f"t{i}"))
        entries = session.metadata[PROVENANCE_KEY]
        assert len(entries) == 100
        assert entries[0]["turn_id"] == "t5"
        assert entries[-1]["turn_id"] == "t104"

    def test_non_list_metadata_is_replaced(self):
        session = self._Session()
        session.metadata[PROVENANCE_KEY] = "junk"
        save_to_session(session, TurnProvenance(turn_id="t0"))
        assert session.metadata[PROVENANCE_KEY] == [{"v": 1, "turn_id": "t0",
                                                     "answered": False, "account_intent": False,
                                                     "prompt": [], "prompt_rebuilt": False,
                                                     "skills_loaded": [], "calls": [],
                                                     "args_repaired": {}, "used": [],
                                                     "other_steps": 0}]


class TestEffectiveTurnId:
    def test_wire_id_wins_and_fallback_is_used(self):
        assert _effective_turn_id({WEBUI_TURN_METADATA_KEY: "wire-1"}, "minted") == "wire-1"
        assert _effective_turn_id({WEBUI_TURN_METADATA_KEY: ""}, "minted") == "minted"
        assert _effective_turn_id({WEBUI_TURN_METADATA_KEY: 7}, "minted") == "minted"
        assert _effective_turn_id(None, "minted") == "minted"


@pytest.mark.asyncio
async def test_wire_turn_id_is_the_record_key(tmp_path: Path):
    loop, bus = _make_loop(tmp_path, lambda: _answer())
    await _run_turn(loop, bus, metadata={WEBUI_TURN_METADATA_KEY: "wire-turn-1"})

    entries = _entries(loop)
    assert len(entries) == 1
    assert entries[0]["turn_id"] == "wire-turn-1"
    assert entries[0]["v"] == 1


@pytest.mark.asyncio
async def test_record_key_falls_back_to_context_turn_id(tmp_path: Path):
    loop, bus = _make_loop(tmp_path, lambda: _answer())
    await _run_turn(loop, bus)

    entries = _entries(loop)
    assert len(entries) == 1
    # ctx.turn_id is "{session_key}:{time_ns}" (loop, minted at turn start).
    assert re.fullmatch(r"websocket:chat1:\d+", entries[0]["turn_id"])


@pytest.mark.asyncio
async def test_effective_turn_id_reaches_the_run_spec(tmp_path: Path):
    loop, bus = _make_loop(tmp_path, lambda: _answer("Prague."))
    specs: list[Any] = []
    real_run = loop.runner.run

    async def spy(spec: Any) -> Any:
        specs.append(spec)
        return await real_run(spec)

    loop.runner.run = spy  # type: ignore[method-assign]
    await _run_turn(loop, bus, metadata={WEBUI_TURN_METADATA_KEY: "wire-spec-1"})
    await _run_turn(loop, bus)
    assert [spec.turn_id for spec in specs] == ["wire-spec-1", specs[1].turn_id]
    assert re.fullmatch(r"websocket:chat1:\d+", specs[1].turn_id)
    # The saved records carry the identical ids the runner was given.
    assert [e["turn_id"] for e in _entries(loop)] == [spec.turn_id for spec in specs]


@pytest.mark.asyncio
async def test_fields_come_from_the_turn_runtime_and_decision(tmp_path: Path):
    loop, bus = _make_loop(tmp_path, lambda: _answer())
    await _run_turn(loop, bus, metadata={"reasoning_profile": "fast"})

    entry = _entries(loop)[0]
    assert entry["reasoning_profile"] == "fast"
    assert entry["model"] == "test-model"
    assert entry["source"] == "user"
    assert entry["answered"] is True
    # No nanobot.runtime_release module on this branch (TP-03 owns it).
    assert entry["release"] == "unknown"
    assert datetime.fromisoformat(entry["started_at"]).tzinfo is not None


@pytest.mark.asyncio
async def test_source_follows_the_llm_usage_classification(tmp_path: Path):
    loop, bus = _make_loop(tmp_path, lambda: _answer())
    await _run_turn(loop, bus)
    await _run_turn(loop, bus, metadata={"_cron_trigger": {"job": "x"}})

    entries = _entries(loop)
    assert [e["source"] for e in entries] == ["user", "cron"]


@pytest.mark.asyncio
async def test_empty_final_answer_is_not_answered(tmp_path: Path):
    loop, bus = _make_loop(tmp_path, lambda: _answer(""))
    await _run_turn(loop, bus)

    entry = _entries(loop)[0]
    assert entry["answered"] is False


@pytest.mark.asyncio
async def test_one_entry_per_turn_and_cap_holds_across_turns(tmp_path: Path):
    loop, bus = _make_loop(tmp_path, lambda: _answer())
    # One turn: a single entry.
    await _run_turn(loop, bus, metadata={WEBUI_TURN_METADATA_KEY: "turn-0"})
    assert len(_entries(loop)) == 1
    # 105 turns: only the newest 100 survive, newest last.
    for i in range(1, 105):
        await _run_turn(loop, bus, metadata={WEBUI_TURN_METADATA_KEY: f"turn-{i}"})
    entries = _entries(loop)
    assert len(entries) == 100
    assert entries[0]["turn_id"] == "turn-5"
    assert entries[-1]["turn_id"] == "turn-104"


@pytest.mark.asyncio
async def test_record_never_carries_the_users_message_text(tmp_path: Path):
    loop, bus = _make_loop(tmp_path, lambda: _answer())
    await _run_turn(
        loop,
        bus,
        content="check my inbox for the ZEPHYR-9-SENTINEL booking code",
    )

    entries = _entries(loop)
    assert len(entries) == 1
    blob = json.dumps(entries[0])
    assert "ZEPHYR-9-SENTINEL" not in blob
    assert "inbox" not in blob
    # The account-intent regex fires on that wording, but only the boolean lands.
    assert entries[0]["account_intent"] is True


@pytest.mark.asyncio
async def test_argument_repairs_reach_the_record_via_the_contextvar(tmp_path: Path):
    tool_round = LLMResponse(
        content=None,
        tool_calls=[
            ToolCallRequest(id="c1", name="search", arguments={"query": "x", "count": "3"})
        ],
        usage=None,
    )

    state = {"n": 0}

    def respond() -> LLMResponse:
        state["n"] += 1
        return tool_round if state["n"] == 1 else _answer("found it")

    loop, bus = _make_loop(tmp_path, respond)
    loop.tools.register(_SearchTool())
    await _run_turn(loop, bus)

    entry = _entries(loop)[0]
    assert entry["args_repaired"] == {"type_cast": 1}
    assert entry["answered"] is True


@pytest.mark.asyncio
async def test_clean_arguments_record_no_repairs(tmp_path: Path):
    """Negative control: an unrepaired call leaves ``args_repaired`` empty."""
    tool_round = LLMResponse(
        content=None,
        tool_calls=[
            ToolCallRequest(id="c1", name="search", arguments={"query": "x", "count": 3})
        ],
        usage=None,
    )
    state = {"n": 0}

    def respond() -> LLMResponse:
        state["n"] += 1
        return tool_round if state["n"] == 1 else _answer("found it")

    loop, bus = _make_loop(tmp_path, respond)
    loop.tools.register(_SearchTool())
    await _run_turn(loop, bus)

    entry = _entries(loop)[0]
    assert entry["args_repaired"] == {}
    assert entry["answered"] is True
