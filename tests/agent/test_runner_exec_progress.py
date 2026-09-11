"""A repeated no-op must recover before exhausting a long task's budget."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.agent.runner import AgentRunner, AgentRunSpec
from nanobot.providers.base import LLMResponse, ToolCallRequest


def setup_run(responses, outputs):
    provider = MagicMock()
    provider.chat_with_retry = AsyncMock(side_effect=responses)
    tools = MagicMock()
    tools.get_definitions.return_value = []
    tools.execute = AsyncMock(side_effect=outputs)
    spec = AgentRunSpec(initial_messages=[], tools=tools, model="test", max_iterations=1000,
                        max_tool_result_chars=16000)
    return AgentRunner(provider), spec


def call(index, name="exec", command="correct spelling"):
    return LLMResponse(content=None, tool_calls=[ToolCallRequest(
        id=f"call-{index}", name=name, arguments={"command": command},
    )])


@pytest.mark.asyncio
async def test_recovers_from_repeated_noop_and_completes():
    runner, spec = setup_run(
        [call(i) for i in range(3)] + [call(3, name="read_file"), LLMResponse(content="Published the validated result.")],
        ["remaining wrong: 2\nExit code: 0"] * 3 + ["Spelling is already correct."],
    )
    result = await runner.run(spec)
    assert result.stop_reason == "completed"
    assert result.final_content == "Published the validated result."
    third = [m for m in result.messages if m.get("role") == "tool"][2]
    assert "3 times consecutively" in third["content"]
    assert "inspect the saved result" in third["content"]


@pytest.mark.asyncio
async def test_persistent_noop_stops_with_checkpoint_before_spending_1000_iterations():
    runner, spec = setup_run([call(i) for i in range(1000)], ["unchanged\nExit code: 0"] * 1000)
    spec.checkpoint_callback = AsyncMock()
    result = await runner.run(spec)
    assert spec.tools.execute.await_count == 6
    assert result.stop_reason == "incomplete_response"
    assert result.error and "identical output six times" in result.error
    assert result.messages[-1]["role"] == "assistant"
    checkpoints = [c.args[0] for c in spec.checkpoint_callback.await_args_list]
    assert checkpoints[-1]["phase"] == "tools_completed"
    assert checkpoints[-1]["iteration"] == 5


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", ["command", "output", "tool"])
async def test_observed_changes_allow_work_to_continue(changes):
    calls = [call(i, command=str(i) if changes == "command" else "same",
                  name="status_poll" if changes == "tool" and i == 3 else "exec") for i in range(8)]
    outputs = [str(i) if changes == "output" else "same\nExit code: 0" for i in range(8)]
    runner, spec = setup_run(calls + [LLMResponse(content="Done.")], outputs)
    result = await runner.run(spec)
    assert result.stop_reason == "completed"
    assert spec.tools.execute.await_count == 8


@pytest.mark.asyncio
async def test_repetition_state_resets_between_runs():
    responses = [call(i) for i in range(3)] + [LLMResponse(content="Done.")]
    runner, spec = setup_run(responses * 2, ["same\nExit code: 0"] * 6)
    for _ in range(2):
        result = await runner.run(spec)
        assert result.stop_reason == "completed"
