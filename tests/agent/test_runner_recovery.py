"""MIT-1408: production runner recovery behaviours restored on 0.3.0.

Re-integrates three production behaviours the rewritten 0.3.0 runner lacked:

1. Repeated-exec recovery (production e9daa2a0): consecutive identical
   successful ``exec`` results are fingerprinted; the third repeat gets a
   loop-reassessment hint and the sixth stops the run.
2. Incomplete-final recovery (production 83028651): a ``stop``-finished
   reply that is a dangling introduction (ends with ``:``) or a short
   ``Let me/I'll/I will <verb> ...`` promise is re-prompted with a
   persisted system nudge, at most twice. The stub is never persisted as
   an assistant reply; after the two retries the reply is replaced with an
   explicit "couldn't finish" message and ``stop_reason`` is
   ``incomplete_response``. Blank replies belong to the empty-retry path
   only and are never routed through this mechanism.
3. The self tool's 1000-iteration per-run cap and the per-run
   max-iterations message (production e9daa2a0).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.runner_helpers import make_run_spec
from nanobot.agent.runner import AgentRunner
from nanobot.config.schema import AgentDefaults
from nanobot.providers.base import (
    LLMProvider,
    LLMResponse,
    LLMUsage,
    ToolCallRequest,
)
from nanobot.utils.prompt_templates import render_template

_MAX_TOOL_RESULT_CHARS = AgentDefaults().max_tool_result_chars
_EXEC_HINT_MARKER = "The same shell command has returned identical output"
_EXEC_STOP_MARKER = "identical output six times"
_RECOVERY_MARKER = "promise to act"  # distinctive fragment of the completion instruction
_FINALIZATION_MARKER = "Please provide your response to the user"


def _reported_usage() -> LLMUsage:
    return LLMUsage.reported(input_tokens=5, output_tokens=2)


def _script_provider(responses: list[LLMResponse]) -> tuple[MagicMock, list[dict]]:
    """Provider stub replaying *responses* in order while recording requests."""
    provider = MagicMock(spec=LLMProvider)
    requests: list[dict] = []

    async def chat_stream_with_retry(*, messages, tools=None, **kwargs):
        requests.append({"messages": [dict(message) for message in messages], "tools": tools})
        if not responses:
            raise AssertionError("provider called beyond the scripted responses")
        return responses.pop(0)

    provider.chat_stream_with_retry = chat_stream_with_retry
    return provider, requests


def _registry(result: str = "tool output") -> MagicMock:
    tools = MagicMock()
    tools.get_definitions.return_value = []
    tools.execute = AsyncMock(return_value=result)
    return tools


def _run_spec(provider: MagicMock, tools: MagicMock, *, max_iterations: int):
    return make_run_spec(
        provider,
        initial_messages=[{"role": "user", "content": "do the task"}],
        tools=tools,
        model="test-model",
        max_iterations=max_iterations,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
    )


def _exec_call(index: int, command: str = "pytest -q") -> LLMResponse:
    return LLMResponse(
        content=None,
        tool_calls=[
            ToolCallRequest(id=f"call-{index}", name="exec", arguments={"command": command})
        ],
        usage=_reported_usage(),
    )


def _tool_call(index: int, name: str, arguments: dict) -> LLMResponse:
    return LLMResponse(
        content=None,
        tool_calls=[ToolCallRequest(id=f"call-{index}", name=name, arguments=arguments)],
        usage=_reported_usage(),
    )


def _final(text: str) -> LLMResponse:
    return LLMResponse(content=text, tool_calls=[], usage=_reported_usage())


def _empty(finish_reason: str = "stop") -> LLMResponse:
    return LLMResponse(content=None, tool_calls=[], finish_reason=finish_reason, usage=_reported_usage())


def _contents(request: dict) -> str:
    return "\n".join(str(message.get("content", "")) for message in request["messages"])


# ---------------------------------------------------------------------------
# 1. Repeated-exec recovery: hint at 3, stop at 6
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_repeated_identical_exec_result_gets_loop_hint_after_three():
    command = "pytest -q"
    provider, requests = _script_provider([
        _exec_call(1, command),
        _exec_call(2, command),
        _exec_call(3, command),
        _final("all done"),
    ])
    tools = _registry(result="all 42 tests passed")

    result = await AgentRunner().run(_run_spec(provider, tools, max_iterations=10))

    assert result.stop_reason == "completed"
    assert result.final_content == "all done"
    assert len(requests) == 4
    assert tools.execute.await_count == 3
    # The first two repeats travel untouched; only the third identical result
    # is annotated, so the fourth request is the first to carry the hint.
    assert not any(_EXEC_HINT_MARKER in _contents(requests[index]) for index in range(3))
    assert sum(
        _EXEC_HINT_MARKER in str(message.get("content", ""))
        for message in requests[3]["messages"]
    ) == 1
    # The completion instruction belongs to cut-off final answers only.
    assert not any(_RECOVERY_MARKER in _contents(request) for request in requests)


@pytest.mark.asyncio
async def test_persistent_identical_exec_result_stops_before_seventh_request():
    provider, requests = _script_provider([_exec_call(index) for index in range(1, 7)])
    tools = _registry(result="unchanged\nExit code: 0")
    checkpoint = AsyncMock()
    spec = _run_spec(provider, tools, max_iterations=12)
    spec.checkpoint_callback = checkpoint

    result = await AgentRunner().run(spec)

    # Six identical executions produce six requests; the stop decision fires
    # before a seventh request is issued.
    assert len(requests) == 6
    assert tools.execute.await_count == 6
    assert result.stop_reason == "incomplete_response"
    assert result.error is not None and _EXEC_STOP_MARKER in result.error
    assert result.final_content == result.error
    assert result.messages[-1]["role"] == "assistant"
    assert _EXEC_STOP_MARKER in result.messages[-1]["content"]
    # The stop preserves the completed-work checkpoint of the final iteration.
    checkpoints = [call.args[0] for call in checkpoint.await_args_list]
    assert checkpoints[-1]["phase"] == "tools_completed"
    assert checkpoints[-1]["iteration"] == 5


@pytest.mark.asyncio
async def test_varying_exec_results_never_trigger_the_hint():
    command = "pytest -q"
    provider, requests = _script_provider(
        [_exec_call(index, command) for index in range(1, 6)] + [_final("done now")]
    )
    tools = _registry()
    tools.execute = AsyncMock(side_effect=[f"run output {index}" for index in range(5)])

    result = await AgentRunner().run(_run_spec(provider, tools, max_iterations=10))

    assert result.stop_reason == "completed"
    assert result.final_content == "done now"
    assert tools.execute.await_count == 5
    assert not any(_EXEC_HINT_MARKER in _contents(request) for request in requests)


@pytest.mark.asyncio
async def test_repeated_identical_nonexec_results_are_not_tracked():
    provider, requests = _script_provider(
        [_tool_call(index, "read_file", {"path": "a.txt"}) for index in range(1, 6)]
        + [_final("done now")]
    )
    tools = _registry(result="same file contents")

    result = await AgentRunner().run(_run_spec(provider, tools, max_iterations=10))

    assert result.stop_reason == "completed"
    assert result.final_content == "done now"
    assert tools.execute.await_count == 5
    assert not any(_EXEC_HINT_MARKER in _contents(request) for request in requests)


@pytest.mark.asyncio
async def test_repeated_identical_exec_results_in_multi_call_batch_are_not_tracked():
    def _batch(index: int) -> LLMResponse:
        return LLMResponse(
            content=None,
            tool_calls=[
                ToolCallRequest(id=f"a-{index}", name="exec", arguments={"command": "status"}),
                ToolCallRequest(id=f"b-{index}", name="exec", arguments={"command": "status"}),
            ],
            usage=_reported_usage(),
        )

    provider, requests = _script_provider([_batch(index) for index in range(1, 5)] + [_final("done")])
    tools = _registry(result="status: idle")

    result = await AgentRunner().run(_run_spec(provider, tools, max_iterations=10))

    assert result.stop_reason == "completed"
    assert result.final_content == "done"
    assert tools.execute.await_count == 8
    assert not any(_EXEC_HINT_MARKER in _contents(request) for request in requests)


# ---------------------------------------------------------------------------
# 2. Incomplete-final recovery (production 83028651): dangling-stub
#    detection on finish_reason "stop", persisted system nudge, two
#    retries, then an incomplete_response replacement
# ---------------------------------------------------------------------------

# Production's own re-prompt cases from commit 83028651.
_STUB_COLON = "Here are the two questions:"
_STUB_PROMISE = "Let me check the current state of things."
_INCOMPLETE_REPLACEMENT = (
    "I couldn't finish this response after two automatic retries. "
    "The task is not confirmed complete. Please retry; any required "
    "approvals still apply."
)


def _stub_nudge_count(request: dict) -> int:
    return sum(
        message.get("role") == "system" and _RECOVERY_MARKER in str(message.get("content", ""))
        for message in request["messages"]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("stub", [_STUB_PROMISE, _STUB_COLON])
async def test_incomplete_final_stub_is_reprompted_and_not_persisted(stub):
    """A dangling stub on 'stop' is re-prompted, never saved as the reply.

    Production 83028651: the nudge is persisted as a system message, the
    stub is not recorded as a completed assistant reply, and the run
    completes with the follow-up answer after (at most) two retries.
    """
    provider, requests = _script_provider([
        _final(stub),
        _final("Here are the answers: 42 tests passed, 0 failed."),
    ])
    tools = _registry()

    result = await AgentRunner().run(_run_spec(provider, tools, max_iterations=5))

    assert result.stop_reason == "completed"
    assert result.final_content == "Here are the answers: 42 tests passed, 0 failed."
    assert len(requests) == 2
    # The nudge is absent from the first request and present, exactly once,
    # as a persisted system message in the second.
    assert _stub_nudge_count(requests[0]) == 0
    assert _stub_nudge_count(requests[1]) == 1
    # The nudge rides on the regular tool surface, not the no-tools
    # finalization fallback.
    assert requests[1]["tools"] is not None
    assert _FINALIZATION_MARKER not in _contents(requests[1])
    # The abandoned stub never entered the transcript as an assistant turn.
    assert not any(
        message.get("role") == "assistant" and stub in str(message.get("content", ""))
        for message in result.messages
    )


@pytest.mark.asyncio
async def test_incomplete_final_recovery_is_bounded_and_not_successful():
    provider, requests = _script_provider([_final(_STUB_COLON) for _ in range(3)])
    tools = _registry()

    # A fourth request would exceed the bound and raise from the stub.
    result = await AgentRunner().run(_run_spec(provider, tools, max_iterations=10))

    # Initial + two re-prompts; the third detection replaces the reply with
    # production's exact message and stops with 'incomplete_response'.
    assert len(requests) == 3
    assert result.stop_reason == "incomplete_response"
    assert result.final_content == _INCOMPLETE_REPLACEMENT
    assert "not confirmed complete" in result.messages[-1]["content"]
    assert result.messages[-1]["role"] == "assistant"
    # Every re-prompt carried the persisted system nudge.
    assert _stub_nudge_count(requests[0]) == 0
    assert _stub_nudge_count(requests[1]) == 1
    assert _stub_nudge_count(requests[2]) == 2
    # The stub itself was never persisted as an assistant reply.
    assert not any(
        message.get("role") == "assistant" and _STUB_COLON in str(message.get("content", ""))
        for message in result.messages
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", [
    "The build passed. You can open the app.",
    "Should I build it?",
    "I can't proceed without your approval.",
    "Understood. Let me know if you need anything else.",
    "Final scan confirms 216 files.",
])
async def test_complete_final_reply_is_not_reprompted(reply):
    """Negative controls: real completions (incl. short ones and sentences
    mentioning the probe verbs without a dangling lead-in) complete on the
    first request, with no nudge and no extra provider call."""
    provider, requests = _script_provider([_final(reply)])
    tools = _registry()

    result = await AgentRunner().run(_run_spec(provider, tools, max_iterations=5))

    assert result.stop_reason == "completed"
    assert result.final_content == reply
    assert len(requests) == 1
    assert _stub_nudge_count(requests[0]) == 0


@pytest.mark.asyncio
async def test_long_stub_like_preamble_is_not_replaced():
    """The heuristic's <400-char bound must not flag a long preamble that
    shares the stub's lead-in wording."""
    reply = _STUB_PROMISE[:-1] + " " + "detail " * 60  # exceeds 400 chars
    assert len(reply) >= 400
    provider, requests = _script_provider([_final(reply)])
    tools = _registry()

    result = await AgentRunner().run(_run_spec(provider, tools, max_iterations=5))

    assert result.stop_reason == "completed"
    assert result.final_content is not None
    assert result.final_content.strip() == reply.strip()
    assert len(result.final_content) >= 400
    assert len(requests) == 1
    assert _stub_nudge_count(requests[0]) == 0


@pytest.mark.asyncio
async def test_blank_reply_uses_empty_retry_path_without_incomplete_nudge():
    """Blank replies stay on the empty-retry path: exactly the pre-MIT-1408
    provider-call shape (initial + silent retry + no-tools finalization),
    with no completion nudge anywhere and the stub mechanism untriggered."""
    provider, requests = _script_provider([
        _empty(finish_reason="stop"),
        _empty(finish_reason="stop"),
        _final("Recovered after the silent retry"),
    ])
    tools = _registry()

    result = await AgentRunner().run(_run_spec(provider, tools, max_iterations=5))

    assert result.stop_reason == "completed"
    assert result.final_content == "Recovered after the silent retry"
    # 1 initial + 1 silent retry + 1 no-tools finalization = production's 3.
    assert len(requests) == 3
    assert all(_stub_nudge_count(request) == 0 for request in requests)
    assert _FINALIZATION_MARKER in _contents(requests[-1])
    assert requests[-1]["tools"] is None


@pytest.mark.asyncio
async def test_content_bearing_length_truncation_is_owned_by_length_recovery():
    """A truncation with recoverable content follows the length-continuation
    mechanism, not the incomplete-final re-prompt."""
    provider, requests = _script_provider([
        LLMResponse(
            content="First part... response interrupted",
            tool_calls=[],
            finish_reason="length",
            usage=_reported_usage(),
        ),
        LLMResponse(
            content="Continue the response",
            tool_calls=[],
            finish_reason="length",
            usage=_reported_usage(),
        ),
        _final("Final summary"),
    ])
    tools = _registry()

    result = await AgentRunner().run(_run_spec(provider, tools, max_iterations=5))

    assert result.stop_reason == "completed"
    assert "First part" in result.final_content
    assert "Final summary" in result.final_content
    assert not any(_RECOVERY_MARKER in _contents(request) for request in requests)
    assert not any(_FINALIZATION_MARKER in _contents(request) for request in requests)


# ---------------------------------------------------------------------------
# 3. Self tool: 1000-iteration per-run cap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_self_tool_iteration_cap_accepts_1000_and_rejects_1001():
    from agent.tools.test_self_tool import _make_tool

    tool = _make_tool()

    accepted = await tool.execute(action="set", key="max_iterations", value=1000)
    assert "Set max_iterations" in accepted
    assert tool._runtime_control.snapshot().max_iterations == 1000

    rejected = await tool.execute(action="set", key="max_iterations", value=1001)
    assert "Error" in rejected
    assert "1000" in rejected
    assert tool._runtime_control.snapshot().max_iterations == 1000


@pytest.mark.asyncio
async def test_max_iterations_message_states_the_per_run_budget():
    message = render_template("agent/max_iterations_message.md", strip=True, max_iterations=1000)

    assert "1000" in message
    assert "not a lifetime quota" in message
    assert "scheduled run" in message

    from agent.tools.test_self_tool import _make_tool

    assert "fresh budget" in _make_tool().description
