"""ExecTool refuses environment building inside an interactive chat turn.

Ziggy-local (fork, MIT-1014). Regression cover for the 2026-09-15 trip-planning
turn: three travel sites refused ``web_fetch``, and the model answered by
installing Playwright and building a venv inside the live conversation.

Scoping is the point of this file as much as the refusal is: only the
interactive app websocket is restricted, and everything else -- CLI, API,
subagents, cron, heartbeat, scheduled Work, and any caller with no bound
request context -- must still be able to install software.
"""

import pytest

from nanobot.agent.tools.context import RequestContext, request_context
from nanobot.agent.tools.env_guard import INSTALL_ATTEMPT_BUDGET
from nanobot.agent.tools.shell import ExecTool


def _chat(turn_id: str = "turn-1", **overrides) -> RequestContext:
    fields = {
        "channel": "websocket",
        "chat_id": "chat-1",
        "message_id": "m1",
        "session_key": "websocket:chat-1",
        "metadata": {},
        "turn_id": turn_id,
    }
    fields.update(overrides)
    return RequestContext(**fields)


@pytest.mark.asyncio
async def test_refuses_install_in_interactive_chat_turn(tmp_path):
    tool = ExecTool(working_dir=str(tmp_path))
    with request_context(_chat()):
        result = await tool.execute(command="pip3 install playwright")
    assert "Installing software is not available" in result
    assert "pip install" in result
    # Non-fatal: the runtime must read this as an observation, not a tool
    # error, so no "try a different approach" retry hint gets appended and
    # fail_on_tool_error cannot abort the turn on it.
    assert not str(result).startswith("Error")
    assert getattr(result, "is_error", False) is False


@pytest.mark.asyncio
async def test_refusal_tells_the_model_to_conclude(tmp_path):
    tool = ExecTool(working_dir=str(tmp_path))
    with request_context(_chat()):
        result = await tool.execute(
            command="cd /tmp && python3 -m venv .venv && .venv/bin/pip install playwright",
        )
    assert "tell the user what you could not do" in result
    assert "not a transient failure" in result


@pytest.mark.asyncio
async def test_ordinary_command_still_runs_in_interactive_chat_turn(tmp_path):
    tool = ExecTool(working_dir=str(tmp_path))
    with request_context(_chat()):
        result = await tool.execute(command="echo hello-from-exec")
    assert "hello-from-exec" in result


@pytest.mark.asyncio
async def test_per_turn_budget_escalates_then_holds(tmp_path):
    tool = ExecTool(working_dir=str(tmp_path))
    with request_context(_chat()):
        for _ in range(INSTALL_ATTEMPT_BUDGET):
            soft = await tool.execute(command="pip3 install playwright")
            assert "Installing software is not available" in soft
        hard = await tool.execute(command="npm install puppeteer")
    assert "Stop building an environment" in hard
    assert "Answer the user now" in hard
    assert not str(hard).startswith("Error")


@pytest.mark.asyncio
async def test_budget_resets_on_a_new_turn(tmp_path):
    tool = ExecTool(working_dir=str(tmp_path))
    with request_context(_chat("turn-1")):
        for _ in range(INSTALL_ATTEMPT_BUDGET + 2):
            await tool.execute(command="pip install x")
    with request_context(_chat("turn-2")):
        result = await tool.execute(command="pip install x")
    assert "Installing software is not available" in result
    assert "Stop building an environment" not in result


@pytest.mark.asyncio
async def test_start_turn_resets_the_budget(tmp_path):
    tool = ExecTool(working_dir=str(tmp_path))
    with request_context(_chat()):
        for _ in range(INSTALL_ATTEMPT_BUDGET + 2):
            await tool.execute(command="pip install x")
        tool.start_turn()
        result = await tool.execute(command="pip install x")
    assert "Stop building an environment" not in result


# --- scope: non-chat paths must be untouched ------------------------------


@pytest.mark.asyncio
async def test_no_request_context_means_no_restriction(tmp_path):
    """CLI runs, SDK embedders and tests bind no context and stay unrestricted."""
    tool = ExecTool(working_dir=str(tmp_path))
    result = await tool.execute(command="pip install --help")
    assert "Installing software is not available" not in result


@pytest.mark.parametrize(
    ("overrides", "why"),
    [
        ({"metadata": {"work_mode": "background", "work_task_id": "t1"}},
         "background Work task"),
        ({"metadata": {"work_mode": "scheduled", "work_task_id": "t2"}},
         "scheduled Work task"),
        ({"session_key": "cron:job-1"}, "cron run"),
        ({"session_key": "heartbeat"}, "heartbeat run"),
        ({"channel": "cli", "session_key": "cli:direct"}, "cli"),
        ({"channel": "api", "session_key": "api:x"}, "api"),
        ({"channel": "system", "session_key": "system:sub"}, "subagent"),
    ],
)
@pytest.mark.asyncio
async def test_non_interactive_turns_are_not_restricted(tmp_path, overrides, why):
    tool = ExecTool(working_dir=str(tmp_path))
    with request_context(_chat(**overrides)):
        result = await tool.execute(command="pip install --help")
    assert "Installing software is not available" not in result, why
