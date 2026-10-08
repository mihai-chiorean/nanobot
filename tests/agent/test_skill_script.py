"""SR-18: run a skill's script deterministically before the agent step.

Covers the primitives in ``nanobot.agent.skill_script``:

* the injected ``skill_script`` tool result reaches the *first* model call
  (driven through the real ``AgentRunner`` with a recording provider, i.e.
  the production call path, not a hand-built message list);
* path confinement: absolute paths, ``..`` components, and symlink escapes
  outside the skill dir are refused before anything spawns;
* the timeout kills the whole process group (children included) and reports
  ``timed_out``;
* interpreter selection, tails, clamps, and non-zero exits.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from agent.runner_helpers import make_run_spec
from nanobot.agent.hook import AgentHookContext
from nanobot.agent.runner import AgentRunner
from nanobot.agent.skill_script import (
    SKILL_SCRIPT_TOOL_NAME,
    SkillScriptError,
    SkillScriptInjectionHook,
    build_skill_script_messages,
    run_skill_script,
    validate_script_rel_path,
)
from nanobot.agent.tools.base import Tool as ToolLike
from nanobot.config.schema import AgentDefaults
from nanobot.providers.base import LLMProvider, LLMResponse, ToolCallRequest

MAX_REAP_WAIT_S = 5.0


def _skill_dir(tmp_path: Path) -> Path:
    root = tmp_path / "skill"
    (root / "scripts").mkdir(parents=True)
    return root


def _proc_state(pid: int) -> str | None:
    """Process state letter from /proc, or None when the pid is gone."""
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("State:"):
                return line.split(":", 1)[1].strip()[:1]
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return None
    return "?"


async def _wait_dead(pid: int) -> bool:
    """True once *pid* is gone or a zombie (killed, awaiting reap)."""
    deadline = asyncio.get_event_loop().time() + MAX_REAP_WAIT_S
    while asyncio.get_event_loop().time() < deadline:
        state = _proc_state(pid)
        if state is None or state == "Z":
            return True
        await asyncio.sleep(0.05)
    return False


# ---------------------------------------------------------------------------
# Script execution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_script_runs_with_skill_dir_as_cwd_and_env(tmp_path: Path) -> None:
    skill = _skill_dir(tmp_path)
    (skill / "scripts" / "watch.py").write_text(
        "import os, sys\n"
        "print(os.getcwd())\n"
        "print(sys.executable)\n"
        "print(os.environ.get('ZIGGY_TEST', 'missing'))\n"
    )
    result = await run_skill_script(
        skill, "scripts/watch.py", 30, {"ZIGGY_TEST": "from-exec-env"}
    )
    lines = result.stdout_tail.splitlines()
    assert result.exit_code == 0 and not result.failed
    assert Path(lines[0]).resolve() == skill.resolve()
    # .py runs under the runtime's interpreter, and the env is exactly what
    # the caller (exec tool's build_subprocess_env) passed in.
    assert Path(lines[1]).resolve() == Path(sys.executable).resolve()
    assert lines[2] == "from-exec-env"


@pytest.mark.asyncio
async def test_shell_script_runs_via_bin_sh_and_raw_executables_run_directly(
    tmp_path: Path,
) -> None:
    skill = _skill_dir(tmp_path)
    (skill / "scripts" / "job.sh").write_text("echo sh-works\n")
    result = await run_skill_script(skill, "scripts/job.sh", 30, {})
    assert (result.exit_code, result.stdout_tail.strip()) == (0, "sh-works")

    binary = skill / "scripts" / "raw"
    binary.write_bytes(b"#!/bin/sh\necho raw-works\n")
    os.chmod(binary, 0o755)
    result = await run_skill_script(skill, "scripts/raw", 30, {})
    assert (result.exit_code, result.stdout_tail.strip()) == (0, "raw-works")


@pytest.mark.asyncio
async def test_non_executable_unknown_suffix_reports_failure_without_running(
    tmp_path: Path,
) -> None:
    skill = _skill_dir(tmp_path)
    (skill / "scripts" / "data.bin").write_bytes(b"\x00\x01not-a-program")
    result = await run_skill_script(skill, "scripts/data.bin", 30, {})
    assert result.exit_code is None
    assert result.failed
    assert "not executable" in result.stderr_tail


@pytest.mark.asyncio
async def test_nonzero_exit_is_reported_not_raised(tmp_path: Path) -> None:
    skill = _skill_dir(tmp_path)
    (skill / "scripts" / "bad.py").write_text("import sys\nsys.stderr.write('boom')\nsys.exit(3)\n")
    result = await run_skill_script(skill, "scripts/bad.py", 30, {})
    assert result.exit_code == 3
    assert result.failed and not result.timed_out
    assert result.stderr_tail == "boom"


@pytest.mark.asyncio
async def test_missing_script_reports_failure(tmp_path: Path) -> None:
    skill = _skill_dir(tmp_path)
    result = await run_skill_script(skill, "scripts/gone.py", 30, {})
    # spawn fails -> non-positive exit, stderr explains; never raises.
    assert result.failed
    assert result.exit_code is not None and result.exit_code != 0


@pytest.mark.asyncio
async def test_tails_keep_the_last_16k_stdout_and_4k_stderr(tmp_path: Path) -> None:
    skill = _skill_dir(tmp_path)
    (skill / "scripts" / "chatty.py").write_text(
        "import sys\n"
        "sys.stdout.write('STDOUT_HEADMARK' + 'x' * 40000 + 'STDOUT_TAILMARK')\n"
        "sys.stderr.write('STDERR_HEADMARK' + 'e' * 9000 + 'STDERR_TAILMARK')\n"
    )
    result = await run_skill_script(skill, "scripts/chatty.py", 30, {})
    assert result.exit_code == 0
    assert len(result.stdout_tail) <= 16 * 1024
    assert len(result.stderr_tail) <= 4 * 1024
    # The tail that is kept must be the *end* of the output: markers at the
    # head are gone, markers at the tail survive.
    assert result.stdout_tail.endswith("STDOUT_TAILMARK")
    assert result.stderr_tail.endswith("STDERR_TAILMARK")
    assert "STDOUT_HEADMARK" not in result.stdout_tail
    assert "STDERR_HEADMARK" not in result.stderr_tail


@pytest.mark.asyncio
async def test_timeout_kills_the_whole_process_group(tmp_path: Path) -> None:
    skill = _skill_dir(tmp_path)
    # A group leader that spawns its own child: killing only the leader
    # would leave the sleep running (the 105-exec fare-watch failure class).
    (skill / "scripts" / "slow.sh").write_text(
        "echo $$ > self.pid\nsleep 25 & echo $! > child.pid\nwait\n"
    )
    started = time.monotonic()
    result = await run_skill_script(skill, "scripts/slow.sh", 1, {})
    assert result.timed_out and result.failed
    assert time.monotonic() - started < 10.0

    self_pid = int((skill / "self.pid").read_text().strip())
    child_pid = int((skill / "child.pid").read_text().strip())
    assert await _wait_dead(self_pid), "group leader survived the timeout"
    assert await _wait_dead(child_pid), "child escaped the process-group kill"


@pytest.mark.asyncio
async def test_timeout_cancels_the_communicate_task_when_the_reap_times_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A survivor holding the stdout pipe must not leave ``communicate()`` pending.

    ``asyncio.wait_for`` does not cancel a Task it is handed on Python 3.10/3.11
    (bpo-45984) and only does so on 3.12+ as an implementation detail. The shim
    pins the no-cancel semantics for the reap await so this reproduces the
    leak on every supported interpreter, and the runner's own cancel — not the
    stdlib's version-dependent behaviour — is what the test measures.
    """
    skill = _skill_dir(tmp_path)
    # The descendant runs in its own session, so the process-group kill misses
    # it, and it inherits the stdout/stderr pipes: after the group kill the
    # pipes never reach EOF and the reader stays pending.
    (skill / "scripts" / "runaway.py").write_text(
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(2)'],\n"
        "                 start_new_session=True)\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("nanobot.agent.skill_script._KILL_REAP_GRACE_S", 0.1)

    real_wait_for = asyncio.wait_for
    real_ensure_future = asyncio.ensure_future
    readers: list[asyncio.Task] = []

    async def no_cancel_wait_for(awaitable: Any, *, timeout: float | None = None) -> Any:
        if isinstance(awaitable, asyncio.Task) and timeout is not None:
            return await real_wait_for(asyncio.shield(awaitable), timeout=timeout)
        return await real_wait_for(awaitable, timeout=timeout)

    def spy_ensure_future(coro: Any, **kwargs: Any) -> asyncio.Task:
        task = real_ensure_future(coro, **kwargs)
        readers.append(task)
        return task

    monkeypatch.setattr(asyncio, "wait_for", no_cancel_wait_for)
    monkeypatch.setattr(asyncio, "ensure_future", spy_ensure_future)

    result = await run_skill_script(skill, "scripts/runaway.py", 1, {})

    assert result.timed_out and result.failed
    (reader,) = readers
    assert reader.done(), "communicate() task leaked past the timed-out reap"
    assert reader.cancelled()
    # Let the pipe-holding descendant exit so the transport closes its fds
    # while this loop is still alive (instead of at GC time, loop closed).
    await asyncio.sleep(2.4)


@pytest.mark.asyncio
async def test_timeout_is_clamped_to_the_executable_band(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    skill = _skill_dir(tmp_path)
    (skill / "scripts" / "quick.sh").write_text("echo ok\n")
    observed: list[float] = []
    real_wait_for = asyncio.wait_for

    async def spy_wait_for(awaitable: Any, *, timeout: float | None = None) -> Any:
        observed.append(timeout if timeout is not None else -1.0)
        return await real_wait_for(awaitable, timeout=timeout)

    monkeypatch.setattr(asyncio, "wait_for", spy_wait_for)
    await run_skill_script(skill, "scripts/quick.sh", 0, {})
    await run_skill_script(skill, "scripts/quick.sh", 100000, {})
    # 0 -> 1 s floor; 100000 -> 600 s ceiling (exec's hard per-call cap).
    assert 1.0 in observed
    assert 600.0 in observed


# ---------------------------------------------------------------------------
# Path confinement
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rel_path",
    [
        "/etc/passwd",
        "/home/owner/other_skill/x.py",
        "../sibling/evil.py",
        "scripts/../../outside.py",
        "..",
    ],
)
def test_absolute_and_dotdot_paths_are_refused(tmp_path: Path, rel_path: str) -> None:
    skill = _skill_dir(tmp_path)
    with pytest.raises(SkillScriptError):
        validate_script_rel_path(skill, rel_path)


def test_symlink_pointing_outside_the_skill_dir_is_refused(tmp_path: Path) -> None:
    skill = _skill_dir(tmp_path)
    outside = tmp_path / "outside.py"
    outside.write_text("print('escaped')\n")
    (skill / "scripts" / "link.py").symlink_to(outside)
    with pytest.raises(SkillScriptError):
        validate_script_rel_path(skill, "scripts/link.py")


def test_symlink_to_a_sibling_inside_the_skill_dir_is_allowed(tmp_path: Path) -> None:
    # Negative control: confinement is to the skill dir, not to real paths.
    skill = _skill_dir(tmp_path)
    (skill / "scripts" / "real.py").write_text("print('fine')\n")
    (skill / "scripts" / "alias.py").symlink_to(skill / "scripts" / "real.py")
    assert validate_script_rel_path(skill, "scripts/alias.py").name == "real.py"


@pytest.mark.asyncio
async def test_refused_path_raises_before_any_subprocess(tmp_path: Path) -> None:
    skill = _skill_dir(tmp_path)
    with pytest.raises(SkillScriptError):
        await run_skill_script(skill, "../escape.py", 5, {})


# ---------------------------------------------------------------------------
# Injection into the first model call (real runner, production call path)
# ---------------------------------------------------------------------------


def _make_provider(responses: list[LLMResponse]) -> tuple[MagicMock, list[dict[str, Any]]]:
    calls: list[dict[str, Any]] = []
    step = 0

    async def chat_stream_with_retry(**kwargs: Any) -> LLMResponse:
        nonlocal step
        calls.append(dict(kwargs))
        response = responses[min(step, len(responses) - 1)]
        step += 1
        return response

    provider = MagicMock(spec=LLMProvider)
    provider.chat_stream_with_retry = chat_stream_with_retry
    return provider, calls


@pytest.mark.asyncio
async def test_script_output_reaches_the_first_model_call() -> None:
    """The synthetic assistant tool-call + tool row ride into request #1."""
    provider, calls = _make_provider([LLMResponse(content="all good", tool_calls=[])])
    skill_messages = build_skill_script_messages(
        "fare-watch",
        "scripts/flights_watch.py",
        _result(exit_code=0, stdout="FARE SFO-NRT 312 USD", stderr=""),
        call_id="call_script_1",
    )
    hook = SkillScriptInjectionHook(skill_messages)
    tools = MagicMock()
    tools.get_definitions.return_value = []
    runner = AgentRunner()
    result = await runner.run(make_run_spec(
        provider,
        initial_messages=[{"role": "user", "content": "run the fare watch"}],
        tools=tools,
        model="test-model",
        max_iterations=6,
        max_tool_result_chars=AgentDefaults().max_tool_result_chars,
        hook=hook,
    ))
    assert result.final_content == "all good"
    assert len(calls) == 1, "test must exercise the FIRST model call"
    messages = calls[0]["messages"]
    assert [m["role"] for m in messages][-3:] == ["user", "assistant", "tool"]
    assistant, tool_row = messages[-2], messages[-1]
    (tool_call,) = assistant["tool_calls"]
    assert tool_call["id"] == "call_script_1"
    assert tool_call["function"]["name"] == SKILL_SCRIPT_TOOL_NAME
    assert tool_row["tool_call_id"] == "call_script_1"
    assert tool_row["name"] == SKILL_SCRIPT_TOOL_NAME
    assert "FARE SFO-NRT 312 USD" in tool_row["content"]
    assert "exit_code=0" in tool_row["content"]
    # Untrusted-output framing is part of the contract, not decoration.
    assert "untrusted" in tool_row["content"].lower()
    assert hook.injected is True


@pytest.mark.asyncio
async def test_injection_happens_once_and_only_in_iteration_zero() -> None:
    provider, _calls = _make_provider([LLMResponse(content="done", tool_calls=[])])
    hook = SkillScriptInjectionHook(build_skill_script_messages(
        "s", "scripts/a.py", _result(exit_code=0, stdout="x", stderr=""), call_id="c"
    ))
    for iteration in (0, 1, 2):
        context = AgentHookContext(iteration=iteration, messages=[])
        await hook.before_iteration(context)
        assert len(context.messages) == (2 if iteration == 0 else 0)
    assert hook.injected


def _result(*, exit_code: int | None, stdout: str, stderr: str):  # noqa: ANN202
    from nanobot.agent.skill_script import ScriptResult

    return ScriptResult(
        exit_code=exit_code,
        stdout_tail=stdout,
        stderr_tail=stderr,
        duration_s=1.25,
        timed_out=False,
    )


def test_tool_content_includes_exit_code_and_duration() -> None:
    messages = build_skill_script_messages(
        "fare",
        "scripts/f.py",
        _result(exit_code=None, stdout="partial", stderr="hangs"),
        call_id="c2",
    )
    content = messages[1]["content"]
    assert "exit_code=none" in content
    assert "timed_out=false" in content
    assert "duration_s=" in content
    assert "partial" in content and "hangs" in content


# ---------------------------------------------------------------------------
# The per-run cap reaches the runner (SR-18 change in loop.py)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_process_direct_max_iterations_reaches_the_runner_spec(
    tmp_path: Path,
) -> None:
    """``process_direct(max_iterations=6)`` bounds only that run's spec."""
    from agent.test_read_only_turns import _make_loop

    tool_call_response = LLMResponse(
        content=None,
        tool_calls=[ToolCallRequest(id="c1", name="ping", arguments={})],
    )

    class _PingTool(ToolLike):
        name = "ping"
        description = "ping"
        parameters = {"type": "object", "properties": {}, "required": []}

        async def execute(self, **kwargs: Any) -> str:
            return "pong"

    loop, calls = _make_loop(tmp_path, [tool_call_response])
    loop.tools.register(_PingTool())
    await loop.process_direct("run the skill", session_key="cron:job-1", max_iterations=2)
    # Without the override this loop would spin to the configured 200. The
    # two tool iterations ran (iteration 2 sees ping's tool result), then the
    # run stopped at the cap -- at most one extra budget/finalize call.
    assert 2 <= len(calls) <= 3, f"expected ~2 model calls, got {len(calls)}"
    assert any(m.get("role") == "tool" for m in calls[1]["messages"]), (
        "iteration 2 never saw the first tool result"
    )
