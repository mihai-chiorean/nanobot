"""Production Acquire digest regressions without relaxing host-file boundaries."""

import pytest

from nanobot.agent.tools.shell import ExecTool


@pytest.mark.parametrize("redirect", ["> /dev/null", "2>/dev/null", ">>/dev/null", '< /dev/null', '>"/dev/null"', "> '/dev/null'"])
def test_null_redirect_with_workspace_file(tmp_path, redirect):
    tool = ExecTool(working_dir=str(tmp_path), restrict_to_workspace=True)
    assert tool._guard_command(f"cat {tmp_path}/payload.json {redirect}; date", str(tmp_path)) is None


@pytest.mark.parametrize("command", [
    "cat /etc/passwd > /dev/null",
    "echo data > /dev/null/file", "cat /tmp/outside.json 2>/dev/null",
])
def test_null_redirect_does_not_exempt_host_access(tmp_path, command):
    tool = ExecTool(working_dir=str(tmp_path), restrict_to_workspace=True)
    assert tool._guard_command(command, str(tmp_path)) is not None


@pytest.mark.parametrize("command", [
    "head -c 16 /dev/urandom | base64",
    "cat /dev/null",
    "cat /dev/zero",
    "cat /dev/full",
    "cat /dev/random",
    "cat /dev/stdin",
    "cat /dev/stdout",
    "cat /dev/stderr",
    "cat /dev/tty",
    "cat /dev/fd/0",
    "cat /dev/fd/1",
    "cat /dev/fd/2",
])
def test_benign_device_reads_are_not_workspace_blocked(tmp_path, command):
    tool = ExecTool(working_dir=str(tmp_path), restrict_to_workspace=True)
    assert tool._guard_command(command, str(tmp_path)) is None


@pytest.mark.parametrize("command", [
    "cat /dev/sda",
    "cat /dev/null/file",
    "cat /dev/fd/3",
    "cat /dev/fd/987",
    "cat /dev/fd/01",
    "cat /dev/fd/$FD",
    "cat /dev/fd/../../etc/passwd",
])
def test_device_allowlist_does_not_become_path_bypass(tmp_path, command):
    tool = ExecTool(working_dir=str(tmp_path), restrict_to_workspace=True)
    assert tool._guard_command(command, str(tmp_path)) is not None


@pytest.mark.parametrize(("path", "benign"), [
    ("/dev/fd/0", True),
    ("/dev/fd/1", True),
    ("/dev/fd/2", True),
    ("/dev/fd/3", False),
    ("/dev/fd/12", False),
    ("/dev/fd/01", False),
])
def test_only_standard_stream_fds_are_benign(path, benign):
    # The deployed snapshot allowed only the standard streams; fd 3+ is left to
    # the path boundary (after resolve()) rather than exempted by name.
    assert ExecTool._is_benign_device_path(path) is benign


def test_workspace_boundary_includes_sibling_of_working_directory(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    tool = ExecTool(working_dir=str(tmp_path), restrict_to_workspace=True)
    assert tool._guard_command(f"cat {tmp_path}/payload.json > /dev/null", str(project)) is None
    assert tool._guard_command(f"cat {tmp_path.parent}/outside.json", str(project)) is not None


def test_workspace_symlink_still_cannot_escape(tmp_path):
    (tmp_path / "outside").symlink_to(tmp_path.parent, target_is_directory=True)
    tool = ExecTool(working_dir=str(tmp_path), restrict_to_workspace=True)
    assert tool._guard_command(f"cat {tmp_path}/outside/private.json > /dev/null", str(tmp_path)) is not None


@pytest.mark.asyncio
async def test_executes_normal_null_redirect(tmp_path):
    tool = ExecTool(working_dir=str(tmp_path), restrict_to_workspace=True)
    result = await tool.execute("printf ignored > /dev/null; printf completed")
    assert "completed" in result and "Exit code: 0" in result
