"""TS-13 (MIT-1807): ``thinkFirstIteration`` — thinking on iteration 0 only.

84% of chat turns resolve ``auto`` -> ``fast``, so the first model call, where
the tool family is chosen, has no reasoning.  This experiment runs only
iteration 0 of an ``requested auto -> fast`` turn at the ``think`` generation
(effort/temperature/max_tokens from ``reasoning_policy.generation_profile``);
every later request keeps the turn's own generation, and the tools list is
untouched so the cached prompt prefix stays byte-stable.  Off by default;
TS-14 decides.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from loguru import logger

from agent.runner_helpers import make_run_spec
from nanobot.agent.context import TranscriptInput
from nanobot.agent.loop import AgentLoop
from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.context import RequestContext
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.bus.queue import MessageBus
from nanobot.config.schema import AgentDefaults, Config
from nanobot.providers.base import (
    GenerationSettings,
    LLMProvider,
    LLMResponse,
    ToolCallRequest,
)
from nanobot.utils.llm_runtime import LLMRuntime

_MAX_TOOL_RESULT_CHARS = AgentDefaults().max_tool_result_chars

# Literal values from reasoning_policy._PROFILES, written independently of
# the fix: think = high/1.0/32_768, fast = none/0.7/8_192.
_THINK_EFFORT, _THINK_TEMP, _THINK_MAX_TOKENS = "high", 1.0, 32_768
_FAST_EFFORT, _FAST_TEMP, _FAST_MAX_TOKENS = "none", 0.7, 8_192


class _EchoTool(Tool):
    """A registered tool so iteration 0 executes work and iteration 1 runs."""

    @property
    def name(self) -> str:
        return "echo_ts13"

    @property
    def description(self) -> str:
        return "echo"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "required": []}

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, **kwargs: Any) -> str:
        return "ok"


def _tools() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(_EchoTool())
    return registry


def _tool_call_response() -> LLMResponse:
    return LLMResponse(
        content=None,
        tool_calls=[ToolCallRequest(id="call_1", name="echo_ts13", arguments={})],
        finish_reason="tool_calls",
    )


def _text(content: str) -> LLMResponse:
    return LLMResponse(content=content, tool_calls=[], finish_reason="stop")


def _blank() -> LLMResponse:
    return LLMResponse(content=None, tool_calls=[], finish_reason="stop")


def _recording_provider(responses: list[LLMResponse]) -> tuple[MagicMock, list[dict[str, Any]]]:
    calls: list[dict[str, Any]] = []
    remaining = list(responses)

    async def chat_stream_with_retry(**kwargs: Any) -> LLMResponse:
        calls.append(dict(kwargs))
        if remaining:
            return remaining.pop(0)
        return _text("done")

    provider = MagicMock(spec=LLMProvider)
    provider.chat_stream_with_retry = chat_stream_with_retry
    return provider, calls


def _fast_chat_spec(
    provider: MagicMock,
    tools: ToolRegistry,
    *,
    think_first_iteration: bool,
    reasoning_profile: str | None = "fast",
    allow_reasoning_escalation: bool = False,
) -> Any:
    """The spec the loop builds for an auto->fast chat turn (fast generation values)."""
    return make_run_spec(
        provider,
        initial_messages=[{"role": "user", "content": "which tool answers this?"}],
        tools=tools,
        model="test-model",
        max_iterations=4,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
        temperature=_FAST_TEMP,
        max_tokens=_FAST_MAX_TOKENS,
        reasoning_effort=_FAST_EFFORT,
        reasoning_profile=reasoning_profile,
        allow_reasoning_escalation=allow_reasoning_escalation,
        think_first_iteration=think_first_iteration,
        session_key="test:c1",
    )


def _capture_logs() -> tuple[list[str], int]:
    messages: list[str] = []
    sink_id = logger.add(lambda message: messages.append(str(message)), level="INFO")
    return messages, sink_id


async def _run(responses: list[LLMResponse], **spec_kwargs: Any) -> tuple[Any, list[dict[str, Any]]]:
    from nanobot.agent.runner import AgentRunner

    provider, calls = _recording_provider(responses)
    tools = _tools()
    spec = _fast_chat_spec(provider, tools, **spec_kwargs)
    result = await AgentRunner().run(spec)
    return result, calls


@pytest.mark.asyncio
async def test_flag_on_fast_turn_thinks_on_iteration_0_only() -> None:
    messages, sink_id = _capture_logs()
    try:
        result, calls = await _run(
            [_tool_call_response(), _text("done")],
            think_first_iteration=True,
        )
    finally:
        logger.remove(sink_id)

    assert len(calls) == 2
    assert result.stop_reason == "completed"
    # Iteration 0: the think generation from the policy's THINK profile.
    assert calls[0]["reasoning_effort"] == _THINK_EFFORT
    assert calls[0]["temperature"] == _THINK_TEMP
    assert calls[0]["max_tokens"] == _THINK_MAX_TOKENS
    # Iteration 1: back to the turn's fast generation, not the leaked override.
    assert calls[1]["reasoning_effort"] == _FAST_EFFORT
    assert calls[1]["temperature"] == _FAST_TEMP
    assert calls[1]["max_tokens"] == _FAST_MAX_TOKENS
    # Once per turn, at info.
    applied = [m for m in messages if "think_first_iteration applied session=test:c1" in m]
    assert len(applied) == 1


@pytest.mark.asyncio
async def test_flag_off_every_call_stays_fast() -> None:
    messages, sink_id = _capture_logs()
    try:
        _result, calls = await _run(
            [_tool_call_response(), _text("done")],
            think_first_iteration=False,
        )
    finally:
        logger.remove(sink_id)

    assert len(calls) == 2
    assert [call["reasoning_effort"] for call in calls] == [_FAST_EFFORT, _FAST_EFFORT]
    assert [call["temperature"] for call in calls] == [_FAST_TEMP, _FAST_TEMP]
    assert [m for m in messages if "think_first_iteration applied" in m] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", ["think", "think-code", None])
async def test_flag_on_leaves_non_fast_turns_untouched(profile: str | None) -> None:
    """Negative control: the override is gated on the resolved fast profile only."""
    _result, calls = await _run(
        [_tool_call_response(), _text("done")],
        think_first_iteration=True,
        reasoning_profile=profile,
    )

    assert len(calls) == 2
    assert [call["reasoning_effort"] for call in calls] == [_FAST_EFFORT, _FAST_EFFORT]
    assert [call["max_tokens"] for call in calls] == [_FAST_MAX_TOKENS, _FAST_MAX_TOKENS]


@pytest.mark.asyncio
async def test_escalation_still_fires_on_a_later_empty_response() -> None:
    """The think-first-iteration copy must not consume or block MIT-1410 escalation."""
    _result, calls = await _run(
        [_tool_call_response(), _blank(), _text("done")],
        think_first_iteration=True,
        allow_reasoning_escalation=True,
    )

    assert [call["reasoning_effort"] for call in calls] == [
        _THINK_EFFORT,  # iteration 0 think override
        _FAST_EFFORT,   # later iterations use the ORIGINAL spec
        _THINK_EFFORT,  # escalation on the empty response still fires
    ]
    # The escalation ladder stepped from the untouched fast spec, one rung only.
    assert calls[2]["temperature"] == _THINK_TEMP
    assert calls[2]["max_tokens"] == _THINK_MAX_TOKENS


@pytest.mark.asyncio
async def test_tools_list_is_identical_on_calls_0_and_1() -> None:
    """The per-request spec copy may not perturb the tools list (prefix cache)."""
    tools = _tools()
    from nanobot.agent.runner import AgentRunner

    provider, calls = _recording_provider([_tool_call_response(), _text("done")])
    spec = _fast_chat_spec(provider, tools, think_first_iteration=True)
    await AgentRunner().run(spec)

    definitions = tools.get_definitions()
    assert definitions, "the registry must actually carry a tool"
    assert calls[0]["tools"] == calls[1]["tools"] == definitions


# ---------------------------------------------------------------------------
# Loop wiring: the flag reaches the chat-turn spec only for the
# ``requested auto -> fast`` decision (the real caller path).
# ---------------------------------------------------------------------------


class _LoopHarness:
    """Drives the full loop with a scripted provider, recording generation kwargs."""

    def __init__(
        self,
        tmp_path: Path,
        responses: list[LLMResponse],
        *,
        think_first_iteration: bool,
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self._responses = list(responses)
        provider = MagicMock()
        provider.get_default_model.return_value = "test-model"

        async def chat_stream_with_retry(**kwargs: Any) -> LLMResponse:
            self.calls.append(dict(kwargs))
            if self._responses:
                return self._responses.pop(0)
            return _text("done")

        provider.chat_stream_with_retry = chat_stream_with_retry
        provider.chat_with_retry = AsyncMock(return_value=_text("done"))
        self.provider = provider
        self.loop = AgentLoop(
            bus=MessageBus(),
            provider=provider,
            workspace=tmp_path,
            model="test-model",
            think_first_iteration=think_first_iteration,
        )
        self.loop.tools.register(_EchoTool())
        self.runtime = LLMRuntime(
            provider=provider,
            model="test-model",
            generation=GenerationSettings(
                temperature=_FAST_TEMP,
                max_tokens=_FAST_MAX_TOKENS,
                reasoning_effort=_FAST_EFFORT,
            ),
            context_window_tokens=200_000,
        )

    async def run_turn(self, metadata: dict[str, Any], *, text: str = "hello") -> None:
        await self.loop._run_agent_loop(
            TranscriptInput(history=[], current_message=text, media=[]),
            runtime=self.runtime,
            request_context=RequestContext(
                channel="test",
                chat_id="c1",
                session_key="test:c1",
                runtime=self.runtime,
                metadata=dict(metadata),
            ),
        )


@pytest.mark.asyncio
async def test_loop_arms_iteration_0_only_for_requested_auto_fast(
    tmp_path: Path,
) -> None:
    harness = _LoopHarness(
        tmp_path,
        [_tool_call_response(), _text("here you go")],
        think_first_iteration=True,
    )

    await harness.run_turn({"reasoning_profile": "auto"})

    # "hello" resolves auto -> fast; iteration 0 gets the think generation.
    assert harness.calls[0]["reasoning_effort"] == _THINK_EFFORT
    assert harness.calls[0]["temperature"] == _THINK_TEMP
    assert harness.calls[0]["max_tokens"] == _THINK_MAX_TOKENS
    assert harness.calls[1]["reasoning_effort"] == _FAST_EFFORT


@pytest.mark.asyncio
async def test_loop_ignores_the_flag_for_an_explicit_fast_turn(tmp_path: Path) -> None:
    """Negative control: explicit fast is NOT the requested-auto->fast decision."""
    harness = _LoopHarness(
        tmp_path,
        [_tool_call_response(), _text("here you go")],
        think_first_iteration=True,
    )

    await harness.run_turn({"reasoning_profile": "fast"})

    assert [call["reasoning_effort"] for call in harness.calls] == [
        _FAST_EFFORT,
        _FAST_EFFORT,
    ]


def test_flag_defaults_off_in_config_and_loop(tmp_path: Path) -> None:
    assert Config().agents.defaults.think_first_iteration is False
    loop = AgentLoop(
        bus=MessageBus(),
        provider=_provider_for_loop(),
        workspace=tmp_path,
        model="test-model",
    )
    assert loop.think_first_iteration is False


def test_from_config_threads_the_flag(tmp_path: Path) -> None:
    config = Config.model_validate({
        "agents": {"defaults": {
            "workspace": str(tmp_path),
            "thinkFirstIteration": True,
        }},
    })
    assert config.agents.defaults.think_first_iteration is True
    loop = AgentLoop.from_config(
        config,
        tool_registry=ToolRegistry(),
        provider=_provider_for_loop(),
    )
    assert loop.think_first_iteration is True


def _provider_for_loop() -> MagicMock:
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    return provider
