"""Production Acquire digest regressions without relaxing host-file boundaries."""

import pytest

from nanobot.agent.tools.shell import ExecTool


@pytest.mark.parametrize("redirect", ["> /dev/null", "2>/dev/null", ">>/dev/null", '< /dev/null', '>"/dev/null"', "> '/dev/null'"])
def test_null_redirect_with_workspace_file(tmp_path, redirect):
    tool = ExecTool(working_dir=str(tmp_path), restrict_to_workspace=True)
    assert tool._guard_command(f"cat {tmp_path}/payload.json {redirect}; date", str(tmp_path)) is None


@pytest.mark.parametrize("command", [
    "cat /etc/passwd > /dev/null", "rm /dev/null", "chmod 777 /dev/null",
    "echo data > /dev/null/file", "echo data > /dev/zero", "cat /tmp/outside.json 2>/dev/null",
])
def test_null_redirect_does_not_exempt_host_access(tmp_path, command):
    tool = ExecTool(working_dir=str(tmp_path), restrict_to_workspace=True)
    assert tool._guard_command(command, str(tmp_path)) is not None


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
