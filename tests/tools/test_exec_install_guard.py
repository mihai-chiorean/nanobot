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


# The turn shapes below are built from what the runtime actually emits, not
# from hand-written metadata. A synthetic session_key like "cron:job-1" proves
# nothing: `run_bound_cron_job` binds the job to the originating *chat's*
# session key and only uses `cron:{job.id}` as a turn seed, so a test that
# invents the namespace passes while the real shape is misclassified.


def _bound_cron_turn() -> RequestContext:
    """The shape `nanobot.cron.bound_runner.run_bound_cron_job` really emits."""
    from nanobot.cron.bound_runner import _bound_session_delivery_context
    from nanobot.cron.session_turns import (
        CRON_DEFER_UNTIL_IDLE_META,
        CRON_TRIGGER_META,
    )
    from nanobot.cron.types import CronJob, CronPayload

    job = CronJob(
        id="job-7",
        name="nightly digest",
        payload=CronPayload(
            kind="agent_turn",
            message="post the digest",
            # Session-bound: the job runs inside the chat it was created from.
            session_key="websocket:chat-1",
            origin_channel="websocket",
            origin_chat_id="chat-1",
        ),
    )
    channel, chat_id, metadata = _bound_session_delivery_context(
        job, turn_seed=f"cron:{job.id}", source_label=job.name,
    )
    metadata[CRON_TRIGGER_META] = {"job_id": job.id, "job_name": job.name}
    metadata[CRON_DEFER_UNTIL_IDLE_META] = True
    return RequestContext(
        channel=channel,
        chat_id=chat_id,
        message_id=None,
        session_key=job.payload.session_key,
        metadata=metadata,
        turn_id=metadata.get("webui_turn_id"),
    )


def _local_trigger_turn() -> RequestContext:
    """A local trigger firing into a bound chat (nanobot.triggers.local_runner)."""
    from nanobot.webui.metadata import (
        WEBUI_MESSAGE_SOURCE_METADATA_KEY,
        WEBUI_TURN_METADATA_KEY,
    )

    return RequestContext(
        channel="websocket",
        chat_id="chat-1",
        session_key="websocket:chat-1",
        metadata={
            "webui": True,
            WEBUI_TURN_METADATA_KEY: "local:trigger-3:abcdef",
            WEBUI_MESSAGE_SOURCE_METADATA_KEY: {"kind": "local_trigger"},
        },
        turn_id="local:trigger-3:abcdef",
    )


def test_bound_cron_really_reuses_the_chat_session_key():
    """Guards the guard: if this ever changes, the scoping test below is stale."""
    ctx = _bound_cron_turn()
    assert ctx.channel == "websocket"
    assert ctx.session_key == "websocket:chat-1"
    assert not ctx.session_key.startswith("cron:")
    assert ctx.metadata.get("_cron_trigger", {}).get("job_id") == "job-7"
    assert ctx.metadata.get("_webui_message_source", {}).get("kind") == "cron"


def _turn_kinds():
    return [
        (_bound_cron_turn(), "session-bound cron run"),
        (_local_trigger_turn(), "local trigger run"),
        # Heartbeat really does use this session key, on the originating
        # channel (nanobot/cli/gateway_runtime.py: session_key="heartbeat").
        (_chat(session_key="heartbeat", turn_id=None), "heartbeat run"),
        # Dead code on this base; live once the Work app lands (#58).
        (_chat(metadata={"work_mode": "background", "work_task_id": "t1"}),
         "background Work task"),
        (_chat(metadata={"work_mode": "scheduled", "work_task_id": "t2"}),
         "scheduled Work task"),
        (_chat(channel="cli", chat_id="direct", session_key="cli:direct"), "cli"),
        (_chat(channel="api", chat_id="x", session_key="api:x"), "api"),
        (_chat(channel="system", chat_id="sub", session_key="system:sub"), "subagent"),
    ]


@pytest.mark.parametrize(("ctx", "why"), _turn_kinds())
@pytest.mark.asyncio
async def test_non_interactive_turns_are_not_restricted(tmp_path, ctx, why):
    tool = ExecTool(working_dir=str(tmp_path))
    with request_context(ctx):
        result = await tool.execute(command="pip install --help")
    assert "Installing software is not available" not in result, why
