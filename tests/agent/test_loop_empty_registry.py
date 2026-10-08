"""Tests for the empty-registry fallback in AgentLoop (MIT-1812).

ToolRegistry defines ``__len__``, so an empty registry is falsy. Every place
that picked a registry with ``X or self.tools`` silently handed the turn the
agent's FULL registry whenever the intended one was empty (e.g. a session
policy that disables every tool). These tests pin the explicit-``None``
checks: an empty registry stays empty; only ``None`` falls back.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.agent.context import TranscriptInput
from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import GenerationSettings, LLMResponse
from nanobot.session.manager import SessionPolicy

_DISABLED_A = "fake_disable_a"
_DISABLED_B = "fake_disable_b"


class _StubTool(Tool):
    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f"stub tool {self._name}"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "additionalProperties": False}

    async def execute(self, **kwargs: Any) -> Any:
        return "ok"


def _make_loop(tmp_path):
    """Build a loop the way the existing loop tests do, with two extra stub
    tools so the agent registry is guaranteed non-empty."""
    from nanobot.agent.loop import AgentLoop

    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = GenerationSettings()
    provider.chat_stream_with_retry = AsyncMock(
        return_value=LLMResponse(content="done", tool_calls=[], usage=None)
    )
    loop = AgentLoop(bus=bus, provider=provider, workspace=tmp_path, model="test-model")
    loop.tools.register(_StubTool(_DISABLED_A))
    loop.tools.register(_StubTool(_DISABLED_B))
    assert len(loop.tools) > 0, "agent registry must be non-empty for these tests"
    return loop


def _offered_tool_names(tools: list[dict[str, Any]] | None) -> set[str]:
    if not tools:
        return set()
    return {definition["function"]["name"] for definition in tools}


def _assert_no_tools_offered(provider: MagicMock) -> None:
    calls = provider.chat_stream_with_retry.await_args_list
    assert calls, "provider was never called; the turn did not run"
    for call in calls:
        offered = call.kwargs["tools"]
        assert offered in ([], None), (
            "empty registry fell back to the full agent registry: "
            f"{sorted(_offered_tool_names(offered))}"
        )


@pytest.mark.asyncio
async def test_turn_with_every_tool_disabled_offers_no_tools(tmp_path):
    """A session whose policy disables every registered tool must reach the
    provider with no tools, not the full registry."""
    loop = _make_loop(tmp_path)
    session = loop.sessions.get_or_create("cli:direct")
    session.policy = SessionPolicy(disabled_tools=frozenset(loop.tools.tool_names))
    assert len(loop.tools.get_definitions()) > 0

    await loop._process_message(
        InboundMessage(
            channel="cli",
            sender_id="user",
            chat_id="direct",
            content="hello",
        )
    )

    _assert_no_tools_offered(loop.provider)


@pytest.mark.asyncio
async def test_process_direct_with_empty_registry_offers_no_tools(tmp_path):
    """Passing tools=ToolRegistry() through the public turn entry point
    (ctx.tools -> _restore_turn -> _run_agent_loop) must yield no tools."""
    loop = _make_loop(tmp_path)

    await loop.process_direct(
        "hello",
        session_key="cli:empty",
        channel="cli",
        chat_id="empty",
        tools=ToolRegistry(),
    )

    _assert_no_tools_offered(loop.provider)


@pytest.mark.asyncio
async def test_run_agent_loop_with_empty_registry_offers_no_tools(tmp_path):
    """The runner entry point itself must honour an explicit empty registry."""
    loop = _make_loop(tmp_path)

    await loop._run_agent_loop(
        TranscriptInput(history=[], current_message=None),
        runtime=loop.llm_runtime(),
        tools=ToolRegistry(),
    )

    _assert_no_tools_offered(loop.provider)


@pytest.mark.asyncio
async def test_run_agent_loop_with_tools_none_falls_back_to_agent_registry(tmp_path):
    """None still means "use the agent's registry" (negative control)."""
    loop = _make_loop(tmp_path)

    await loop._run_agent_loop(
        TranscriptInput(history=[], current_message=None),
        runtime=loop.llm_runtime(),
        tools=None,
    )

    calls = loop.provider.chat_stream_with_retry.await_args_list
    assert calls, "provider was never called; the turn did not run"
    expected = _offered_tool_names(loop.tools.get_definitions())
    assert expected
    for call in calls:
        assert _offered_tool_names(call.kwargs["tools"]) == expected


@pytest.mark.asyncio
async def test_default_turn_still_offers_all_tools(tmp_path):
    """Negative control on the policy path: a session without disabled_tools
    keeps the full registry."""
    loop = _make_loop(tmp_path)
    loop.sessions.get_or_create("cli:direct")

    await loop._process_message(
        InboundMessage(
            channel="cli",
            sender_id="user",
            chat_id="direct",
            content="hello",
        )
    )

    calls = loop.provider.chat_stream_with_retry.await_args_list
    assert calls, "provider was never called; the turn did not run"
    for call in calls:
        offered = _offered_tool_names(call.kwargs["tools"])
        assert {_DISABLED_A, _DISABLED_B} <= offered
