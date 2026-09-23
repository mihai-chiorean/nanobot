"""ExecTool refuses environment building inside an interactive chat turn.

Ziggy-local (fork, MIT-1014). Regression cover for the 2026-09-15 trip-planning
turn: three travel sites refused ``web_fetch``, and the model answered by
installing Playwright and building a venv inside the live conversation.

Scoping is the point of this file as much as the refusal is: only the
interactive app websocket is restricted, and everything else -- CLI, API,
subagents, cron, heartbeat, scheduled Work, and any ExecTool with no turn
context -- must still be able to install software.
"""

import pytest

from nanobot.agent.tools.env_guard import INSTALL_ATTEMPT_BUDGET
from nanobot.agent.tools.shell import ExecTool


# --------------------------------------------------------------------------
# ExecTool: refuse in an interactive chat turn, allow everywhere else
# --------------------------------------------------------------------------


def _interactive(tool: ExecTool) -> ExecTool:
    tool.set_context(
        "websocket",
        "chat-1",
        message_id="m1",
        metadata={},
        session_key="websocket:chat-1",
    )
    return tool


@pytest.mark.asyncio
async def test_refuses_install_in_interactive_chat_turn(tmp_path):
    tool = _interactive(ExecTool(working_dir=str(tmp_path)))
    result = await tool.execute(command="pip3 install playwright")
    assert "Installing software is not available" in result
    assert "pip install" in result
    # Non-fatal: the runtime must treat this as an observation, not a tool
    # error, so no "try a different approach" retry hint gets appended.
    assert not str(result).startswith("Error")
    assert getattr(result, "is_error", False) is False


@pytest.mark.asyncio
async def test_refusal_tells_the_model_to_conclude(tmp_path):
    tool = _interactive(ExecTool(working_dir=str(tmp_path)))
    result = await tool.execute(
        command="cd /tmp && python3 -m venv .venv && .venv/bin/pip install playwright",
    )
    assert "tell the user what you could not do" in result
    assert "not a transient failure" in result


@pytest.mark.asyncio
async def test_ordinary_command_still_runs_in_interactive_chat_turn(tmp_path):
    tool = _interactive(ExecTool(working_dir=str(tmp_path)))
    result = await tool.execute(command="echo hello-from-exec")
    assert "hello-from-exec" in result


@pytest.mark.asyncio
async def test_per_turn_budget_escalates_then_holds(tmp_path):
    tool = _interactive(ExecTool(working_dir=str(tmp_path)))
    for _ in range(INSTALL_ATTEMPT_BUDGET):
        soft = await tool.execute(command="pip3 install playwright")
        assert "Installing software is not available" in soft
    hard = await tool.execute(command="npm install puppeteer")
    assert "Stop building an environment" in hard
    assert "Answer the user now" in hard
    # Still refused, still non-fatal.
    assert not str(hard).startswith("Error")


@pytest.mark.asyncio
async def test_budget_resets_on_a_new_turn(tmp_path):
    tool = _interactive(ExecTool(working_dir=str(tmp_path)))
    for _ in range(INSTALL_ATTEMPT_BUDGET + 2):
        await tool.execute(command="pip install x")
    tool.start_turn()
    result = await tool.execute(command="pip install x")
    assert "Installing software is not available" in result
    assert "Stop building an environment" not in result


@pytest.mark.asyncio
async def test_new_message_id_resets_the_budget(tmp_path):
    tool = _interactive(ExecTool(working_dir=str(tmp_path)))
    for _ in range(INSTALL_ATTEMPT_BUDGET + 2):
        await tool.execute(command="pip install x")
    tool.set_context(
        "websocket",
        "chat-1",
        message_id="m2",
        metadata={},
        session_key="websocket:chat-1",
    )
    result = await tool.execute(command="pip install x")
    assert "Stop building an environment" not in result


# --- scope: non-chat paths must be untouched ------------------------------


@pytest.mark.asyncio
async def test_no_context_means_no_restriction(tmp_path):
    """A tool nobody told about a chat turn (CLI, tests, SDK) is unrestricted."""
    tool = ExecTool(working_dir=str(tmp_path))
    result = await tool.execute(command="pip install --help")
    assert "Installing software is not available" not in result


@pytest.mark.parametrize(
    ("channel", "metadata", "session_key"),
    [
        # scheduled Work task dispatched over the websocket channel
        ("websocket", {"work_mode": "background", "work_task_id": "t1"}, "websocket:c"),
        ("websocket", {"work_mode": "scheduled", "work_task_id": "t2"}, "websocket:c"),
        # cron / heartbeat runs, which reuse the originating channel
        ("websocket", {}, "cron:job-1"),
        ("websocket", {}, "heartbeat"),
        # non-chat channels
        ("cli", {}, "cli:direct"),
        ("api", {}, "api:x"),
        ("system", {}, "system:sub"),
    ],
)
@pytest.mark.asyncio
async def test_non_interactive_paths_are_not_restricted(
    tmp_path, channel, metadata, session_key
):
    tool = ExecTool(working_dir=str(tmp_path))
    tool.set_context(
        channel,
        "c",
        message_id="m1",
        metadata=metadata,
        session_key=session_key,
    )
    result = await tool.execute(command="pip install --help")
    assert "Installing software is not available" not in result


# --- wiring: the loop must actually hand exec the turn --------------------


def _bare_loop(tools):
    """An AgentLoop with only the two attributes _set_tool_context reads."""
    from nanobot.agent.loop import AgentLoop

    loop = AgentLoop.__new__(AgentLoop)
    loop.tools = tools
    loop._unified_session = False
    return loop


def _registry(tool):
    from nanobot.agent.tools.registry import ToolRegistry

    registry = ToolRegistry()
    registry.register(tool)
    return registry


@pytest.mark.asyncio
async def test_loop_gives_exec_the_turn_context(tmp_path):
    """Without this wiring the guard would never see an interactive turn."""
    tool = ExecTool(working_dir=str(tmp_path))
    loop = _bare_loop(_registry(tool))

    loop._set_tool_context("websocket", "chat-1", "m1", {}, session_key=None)
    refused = await tool.execute(command="pip3 install playwright")
    assert "Installing software is not available" in refused

    loop._set_tool_context("cli", "direct", None, {}, session_key="cli:direct")
    allowed = await tool.execute(command="pip install --help")
    assert "Installing software is not available" not in allowed


@pytest.mark.asyncio
async def test_loop_resets_the_budget_once_per_inbound_turn(tmp_path):
    """``_process_message`` is the single funnel every turn passes through."""
    from nanobot.agent.loop import AgentLoop

    tool = ExecTool(working_dir=str(tmp_path))
    loop = _bare_loop(_registry(tool))
    loop._set_tool_context("websocket", "chat-1", "m1", {}, session_key=None)

    for _ in range(INSTALL_ATTEMPT_BUDGET + 2):
        await tool.execute(command="pip install x")
    assert "Stop building an environment" in await tool.execute(command="pip install x")

    # Replay the reset _process_message performs before anything else.
    exec_tool = loop.tools.get("exec")
    assert exec_tool is not None and hasattr(exec_tool, "start_turn")
    exec_tool.start_turn()
    loop._set_tool_context("websocket", "chat-1", "m2", {}, session_key=None)

    result = await tool.execute(command="pip install x")
    assert "Installing software is not available" in result
    assert "Stop building an environment" not in result

    # And the reset really is in _process_message, not somewhere optional.
    import inspect

    source = inspect.getsource(AgentLoop._process_message)
    assert 'self.tools.get("exec")' in source
    assert "start_turn()" in source
