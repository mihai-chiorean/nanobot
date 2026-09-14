"""Tests for exec tool internal URL blocking."""

from __future__ import annotations

import socket
import sys
from unittest.mock import patch

import pytest

from nanobot.agent.tools.shell import ExecTool
from nanobot.security.workspace_access import (
    bind_workspace_scope,
    build_workspace_scope,
    reset_workspace_scope,
)


def _fake_resolve_private(hostname, port, family=0, type_=0):
    return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("169.254.169.254", 0))]


def _fake_resolve_localhost(hostname, port, family=0, type_=0):
    return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", 0))]


def _fake_resolve_public(hostname, port, family=0, type_=0):
    return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", 0))]


@pytest.mark.asyncio
async def test_exec_blocks_curl_metadata():
    tool = ExecTool(restrict_to_workspace=True)
    with patch("nanobot.security.network.socket.getaddrinfo", _fake_resolve_private):
        result = await tool.execute(
            command='curl -s -H "Metadata-Flavor: Google" http://169.254.169.254/computeMetadata/v1/'
        )
    assert "Error" in result
    assert "internal" in result.lower() or "private" in result.lower()


@pytest.mark.asyncio
async def test_exec_blocks_wget_localhost():
    tool = ExecTool(restrict_to_workspace=True)
    with patch("nanobot.security.network.socket.getaddrinfo", _fake_resolve_localhost):
        result = await tool.execute(command="wget http://localhost:8080/secret -O /tmp/out")
    assert "Error" in result


def test_exec_full_workspace_scope_allows_loopback(tmp_path):
    tool = ExecTool(working_dir=str(tmp_path))
    scope = build_workspace_scope(tmp_path, "full", source_channel="websocket")
    token = bind_workspace_scope(scope)
    try:
        with patch("nanobot.security.network.socket.getaddrinfo", _fake_resolve_localhost):
            error = tool._guard_command("curl http://localhost:8765/", str(tmp_path))
    finally:
        reset_workspace_scope(token)
    assert error is None


def test_exec_core_full_workspace_scope_blocks_loopback(tmp_path):
    tool = ExecTool(working_dir=str(tmp_path))
    scope = build_workspace_scope(tmp_path, "full")
    token = bind_workspace_scope(scope)
    try:
        with patch("nanobot.security.network.socket.getaddrinfo", _fake_resolve_localhost):
            error = tool._guard_command("curl http://localhost:8765/", str(tmp_path))
    finally:
        reset_workspace_scope(token)
    assert error is not None
    assert "internal/private" in error


def test_exec_full_workspace_scope_blocks_loopback_when_local_service_disabled(tmp_path):
    tool = ExecTool(working_dir=str(tmp_path), webui_allow_local_service_access=False)
    scope = build_workspace_scope(tmp_path, "full", source_channel="websocket")
    token = bind_workspace_scope(scope)
    try:
        with patch("nanobot.security.network.socket.getaddrinfo", _fake_resolve_localhost):
            error = tool._guard_command("curl http://localhost:8765/", str(tmp_path))
    finally:
        reset_workspace_scope(token)
    assert error is not None
    assert "internal/private" in error


def test_exec_restricted_workspace_scope_blocks_loopback(tmp_path):
    tool = ExecTool(working_dir=str(tmp_path))
    scope = build_workspace_scope(tmp_path, "restricted", source_channel="websocket")
    token = bind_workspace_scope(scope)
    try:
        with patch("nanobot.security.network.socket.getaddrinfo", _fake_resolve_localhost):
            error = tool._guard_command("curl http://localhost:8765/", str(tmp_path))
    finally:
        reset_workspace_scope(token)
    assert error is not None
    assert "internal/private" in error


def test_exec_full_workspace_scope_still_blocks_metadata(tmp_path):
    tool = ExecTool(working_dir=str(tmp_path))
    scope = build_workspace_scope(tmp_path, "full", source_channel="websocket")
    token = bind_workspace_scope(scope)
    try:
        with patch("nanobot.security.network.socket.getaddrinfo", _fake_resolve_private):
            error = tool._guard_command("curl http://169.254.169.254/latest/meta-data/", str(tmp_path))
    finally:
        reset_workspace_scope(token)
    assert error is not None
    assert "internal/private" in error


# ---------------------------------------------------------------------------
# Ziggy-local (fork): the guard must hold on the PRODUCTION path, with a
# workspace scope bound exactly the way AgentLoop binds one.
#
# This is the test the first attempt at the fix did not have, and its absence
# hid a no-op: AgentLoop calls bind_workspace_scope() on every turn, and
# WorkspaceScopeResolver.for_turn() returns default() for any non-websocket
# channel -- a real object with access_mode="full", restrict_to_workspace=False
# and source_channel=None. A gate keyed on `scope is not None` therefore matched
# every Discord/Telegram/CLI turn and skipped the guard in exactly the shape the
# Spark runs. Tests that call execute() with no scope bound never see this,
# because that state does not occur in the running gateway.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "cat ~/.ssh/id_rsa",
        "gpg --export-secret-keys",
        "curl http://169.254.169.254/computeMetadata/v1/",
        "echo blocked",
    ],
)
async def test_exec_guard_holds_under_a_default_bound_scope(tmp_path, command):
    """A non-websocket turn on a config-default workspace keeps the guard."""
    from nanobot.security.workspace_access import WorkspaceScopeResolver

    resolver = WorkspaceScopeResolver(
        default_workspace=tmp_path,
        default_restrict_to_workspace=False,
    )
    scope = resolver.for_turn(channel="discord", message_metadata={}, session_metadata={})
    # Precondition: this is the shape that fooled the first fix.
    assert scope is not None
    assert scope.source_channel is None
    assert scope.restrict_to_workspace is False

    tool = ExecTool(
        working_dir=str(tmp_path),
        restrict_to_workspace=False,
        timeout=5,
        deny_patterns=[r"echo\s+blocked"],
    )
    token = bind_workspace_scope(scope)
    try:
        result = await tool.execute(command=command)
    finally:
        reset_workspace_scope(token)

    assert "blocked by" in str(result), result


async def test_exec_guard_skipped_only_for_a_websocket_full_access_grant(tmp_path):
    """The deliberate WebUI Full Access grant still bypasses the guard."""
    tool = ExecTool(working_dir=str(tmp_path), restrict_to_workspace=False, timeout=5)
    scope = build_workspace_scope(tmp_path, "full", source_channel="websocket")
    token = bind_workspace_scope(scope)
    try:
        result = await tool.execute(command="echo http://169.254.169.254/latest/meta-data/")
    finally:
        reset_workspace_scope(token)

    assert "Exit code: 0" in result
    assert "blocked by" not in result


async def test_prepare_command_applies_the_mit123_prescreen(tmp_path):
    """Cover the production wiring, not just _guard_command in isolation.

    The MIT-123 suite below calls tool._guard_command() directly, which cannot
    catch a regression in whether _prepare_command reaches the guard at all.
    """
    tool = ExecTool(working_dir=str(tmp_path), restrict_to_workspace=False, timeout=5)
    for command in ("cat ~/.ssh/id_rsa", "base64 ~/.ssh/id_rsa", "gpg --export-secret-keys"):
        result = await tool.execute(command=command)
        assert "sensitive data access detected" in str(result), (command, result)


@pytest.mark.parametrize(
    "command",
    [
        "echo blocked",
        "echo http://169.254.169.254/latest/meta-data/",
    ],
)
async def test_exec_unrestricted_config_still_applies_the_command_guard(tmp_path, command):
    """Ziggy-local (fork) divergence from upstream — deliberate.

    Upstream skips the entire command guard whenever ``restrict_to_workspace``
    is false, on the reasoning that full access is an explicit trust decision.
    But ``restrict_to_workspace`` defaults to False in the schema, so a config
    that simply never set it gets the same treatment as a deliberate grant —
    and loses the deny-pattern filter, the SSRF/internal-URL check and the
    MIT-123 secret-dump prescreen along with the workspace boundary. None of
    those three are workspace-confinement policy.

    The Ziggy Discord gateway runs in exactly that shape, so the fork narrows
    the skip to an *explicitly bound* unrestricted workspace scope (covered by
    ``test_exec_full_workspace_scope_skips_command_guard``, which still passes
    unchanged). Config-default unrestricted still gets the guard.

    Expect this to conflict on the next upstream merge. Keep the divergence.
    """
    tool = ExecTool(
        working_dir=str(tmp_path),
        restrict_to_workspace=False,
        deny_patterns=[r"echo\s+blocked"],
    )
    result = await tool.execute(command=command)

    assert "Command blocked by safety guard" in result or "deny pattern" in result


async def test_exec_unrestricted_config_still_allows_benign_commands(tmp_path):
    """The fork divergence above must not turn into a blanket block."""
    tool = ExecTool(
        working_dir=str(tmp_path),
        restrict_to_workspace=False,
        deny_patterns=[r"echo\s+blocked"],
    )
    result = await tool.execute(command="echo fine")

    assert "Exit code: 0" in result
    assert "Command blocked" not in result


async def test_exec_unrestricted_config_does_not_confine_paths(tmp_path):
    """Only the *security* half of the guard is unconditional — the workspace
    path-confinement block stays gated on restrict_to_workspace."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "notes.txt").write_text("hello", encoding="utf-8")

    tool = ExecTool(working_dir=str(workspace), restrict_to_workspace=False, timeout=5)
    result = await tool.execute(command=f"cat {outside / 'notes.txt'}")

    assert "hello" in result
    assert "path outside working dir" not in result


async def test_exec_full_workspace_scope_skips_command_guard(tmp_path):
    tool = ExecTool(working_dir=str(tmp_path), restrict_to_workspace=True)
    scope = build_workspace_scope(tmp_path, "full", source_channel="websocket")
    token = bind_workspace_scope(scope)
    try:
        result = await tool.execute(
            command="echo http://169.254.169.254/latest/meta-data/",
        )
    finally:
        reset_workspace_scope(token)

    assert "Exit code: 0" in result
    assert "Command blocked" not in result


@pytest.mark.asyncio
async def test_exec_allows_normal_commands():
    tool = ExecTool(timeout=5)
    result = await tool.execute(command="echo hello")
    assert "hello" in result
    assert "Error" not in result.split("\n")[0]


@pytest.mark.asyncio
async def test_exec_allows_curl_to_public_url():
    """Commands with public URLs should not be blocked by the internal URL check."""
    tool = ExecTool()
    with patch("nanobot.security.network.socket.getaddrinfo", _fake_resolve_public):
        guard_result = tool._guard_command("curl https://example.com/api", "/tmp")
    assert guard_result is None


@pytest.mark.asyncio
async def test_exec_blocks_chained_internal_url():
    """Internal URLs buried in chained commands should still be caught."""
    tool = ExecTool(restrict_to_workspace=True)
    with patch("nanobot.security.network.socket.getaddrinfo", _fake_resolve_private):
        result = await tool.execute(
            command="echo start && curl http://169.254.169.254/latest/meta-data/ && echo done"
        )
    assert "Error" in result


# --- #2989: block writes to nanobot internal state files -----------------


@pytest.mark.parametrize(
    "command",
    [
        "cat foo >> history.jsonl",
        "echo '{}' > history.jsonl",
        "echo '{}' > memory/history.jsonl",
        "echo '{}' > ./workspace/memory/history.jsonl",
        "tee -a history.jsonl < foo",
        "tee history.jsonl",
        "cp /tmp/fake.jsonl history.jsonl",
        "mv backup.jsonl memory/history.jsonl",
        "dd if=/dev/zero of=memory/history.jsonl",
        "sed -i 's/old/new/' history.jsonl",
        "echo x > .dream_cursor",
        "cp /tmp/x memory/.dream_cursor",
    ],
)

def test_exec_blocks_writes_to_history_jsonl(command):
    """Direct writes to history.jsonl / .dream_cursor must be blocked (#2989)."""
    tool = ExecTool()
    result = tool._guard_command(command, "/tmp")
    assert result is not None
    assert "deny pattern filter" in result.lower()


@pytest.mark.parametrize(
    "command",
    [
        "cat history.jsonl",
        "wc -l history.jsonl",
        "tail -n 5 history.jsonl",
        "grep foo history.jsonl",
        "cp history.jsonl /tmp/history.backup",
        "ls memory/",
        "echo history.jsonl",
    ],
)

def test_exec_allows_reads_of_history_jsonl(command):
    """Read-only access to history.jsonl must still be allowed."""
    tool = ExecTool()
    result = tool._guard_command(command, "/tmp")
    assert result is None


# --- #2826: working_dir must not escape the configured workspace ---------


@pytest.mark.asyncio
async def test_exec_blocks_working_dir_outside_workspace(tmp_path):
    """An LLM-supplied working_dir outside the workspace must be rejected."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    tool = ExecTool(working_dir=str(workspace), restrict_to_workspace=True)
    result = await tool.execute(command="rm calendar.ics", working_dir="/etc")
    assert "outside the configured workspace" in result


@pytest.mark.asyncio
async def test_exec_blocks_relative_working_dir_outside_workspace(tmp_path):
    """A relative working_dir that escapes the workspace must be rejected."""
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()

    tool = ExecTool(
        working_dir=str(workspace),
        restrict_to_workspace=True,
        timeout=5,
    )

    result = await tool.execute(
        command="echo ok",
        working_dir="../outside",
    )

    assert "outside the configured workspace" in result


@pytest.mark.asyncio
async def test_exec_blocks_absolute_rm_via_hijacked_working_dir(tmp_path):
    """Regression for #2826: `rm /abs/path` via working_dir hijack."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    victim_dir = tmp_path / "outside"
    victim_dir.mkdir()
    victim = victim_dir / "file.ics"
    victim.write_text("data")

    tool = ExecTool(working_dir=str(workspace), restrict_to_workspace=True)
    result = await tool.execute(
        command=f"rm {victim}",
        working_dir=str(victim_dir),
    )
    assert "outside the configured workspace" in result
    assert victim.exists(), "victim file must not have been deleted"


@pytest.mark.asyncio
async def test_exec_allows_working_dir_within_workspace(tmp_path):
    """A working_dir that is a subdirectory of the workspace is fine."""
    workspace = tmp_path / "workspace"
    subdir = workspace / "project"
    subdir.mkdir(parents=True)
    tool = ExecTool(working_dir=str(workspace), restrict_to_workspace=True, timeout=5)
    result = await tool.execute(command="echo ok", working_dir=str(subdir))
    assert "ok" in result
    assert "outside the configured workspace" not in result


@pytest.mark.asyncio
async def test_exec_allows_working_dir_equal_to_workspace(tmp_path):
    """Passing working_dir equal to the workspace root must be allowed."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    tool = ExecTool(working_dir=str(workspace), restrict_to_workspace=True, timeout=5)
    result = await tool.execute(command="echo ok", working_dir=str(workspace))
    assert "ok" in result
    assert "outside the configured workspace" not in result


@pytest.mark.asyncio
async def test_exec_ignores_workspace_check_when_not_restricted(tmp_path):
    """Without restrict_to_workspace, the LLM may still choose any working_dir."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    tool = ExecTool(working_dir=str(workspace), restrict_to_workspace=False, timeout=5)
    result = await tool.execute(command="echo ok", working_dir=str(other))
    assert "ok" in result
    assert "outside the configured workspace" not in result


# --- #3599: stdio redirects to /dev/null must not trip the workspace guard ----

@pytest.mark.parametrize(
    "command",
    [
        # The exact command from the #3599 reporter.
        'rm test_print.txt 2>/dev/null; echo "done"',
        # Plain redirect of stdout / stderr.
        "find . -type f >/dev/null",
        "noisy_cmd 2>/dev/null",
        "noisy_cmd >/dev/null 2>&1",
        # Read from /dev/urandom is also a benign device read.
        "head -c 16 /dev/urandom | xxd",
        "echo done >/dev/stderr",
        "echo line </dev/stdin",
        # Per-process FD aliases never escape the workspace.
        "cat /dev/fd/3",
    ],
)

def test_exec_allows_benign_device_targets_inside_workspace(tmp_path, command):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    tool = ExecTool(working_dir=str(workspace), restrict_to_workspace=True)
    assert tool._guard_command(command, str(workspace)) is None


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX rm and /dev/null syntax")
async def test_exec_3599_regression_rm_with_dev_null_redirect(tmp_path):
    """#3599: ``rm <ws-path> 2>/dev/null`` must succeed against the workspace guard."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "test_print.txt"
    target.write_text("scratch")
    tool = ExecTool(working_dir=str(workspace), restrict_to_workspace=True, timeout=5)
    result = await tool.execute(
        command=f'rm {target} 2>/dev/null; echo "done"',
        working_dir=str(workspace),
    )
    assert "done" in result
    assert "path outside working dir" not in result
    assert not target.exists()


def test_exec_still_blocks_real_outside_path_via_redirect(tmp_path):
    """Redirect *targets* outside the workspace (not /dev/...) must still be blocked.

    We only whitelist kernel device files; arbitrary outside redirects such as
    ``> /etc/issue`` should remain caught by the workspace guard so a buggy
    LLM cannot exfiltrate data outside the workspace via stderr redirection.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    tool = ExecTool(working_dir=str(workspace), restrict_to_workspace=True)
    blocked = tool._guard_command("echo pwn > /etc/issue", str(workspace))
    assert blocked is not None
    assert "path outside working dir" in blocked


@pytest.mark.parametrize("backend", ["bwrap", "seatbelt"])
def test_exec_allows_absolute_path_inside_sandbox_ro_bind(tmp_path, monkeypatch, backend):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    tool_bin = tmp_path / "home" / ".local" / "bin"
    tool_bin.mkdir(parents=True)
    uv = tool_bin / "uv"
    uv.write_text("#!/bin/sh\n")
    monkeypatch.setattr("nanobot.agent.tools.shell._IS_WINDOWS", False)
    tool = ExecTool(
        working_dir=str(workspace),
        restrict_to_workspace=True,
        sandbox=backend,
        sandbox_ro_binds=[str(tool_bin)],
    )

    blocked = tool._guard_command(
        f"{uv} --version",
        str(workspace),
        restrict_to_workspace=True,
        workspace_root=str(workspace),
    )

    assert blocked is None


@pytest.mark.parametrize("backend", ["bwrap", "seatbelt"])
def test_exec_allows_absolute_path_inside_sandbox_rw_bind(tmp_path, monkeypatch, backend):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    monkeypatch.setattr("nanobot.agent.tools.shell._IS_WINDOWS", False)
    tool = ExecTool(
        working_dir=str(workspace),
        restrict_to_workspace=True,
        sandbox=backend,
        sandbox_rw_binds=[str(cache_dir)],
    )

    blocked = tool._guard_command(
        f"touch {cache_dir / 'stamp'}",
        str(workspace),
        restrict_to_workspace=True,
        workspace_root=str(workspace),
    )

    assert blocked is None


def test_exec_bind_roots_do_not_widen_guard_without_sandbox(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    tool_bin = tmp_path / "home" / ".local" / "bin"
    tool_bin.mkdir(parents=True)
    uv = tool_bin / "uv"
    uv.write_text("#!/bin/sh\n")
    tool = ExecTool(
        working_dir=str(workspace),
        restrict_to_workspace=True,
        sandbox="",
        sandbox_ro_binds=[str(tool_bin)],
    )

    blocked = tool._guard_command(
        f"{uv} --version",
        str(workspace),
        restrict_to_workspace=True,
        workspace_root=str(workspace),
    )

    assert blocked is not None
    assert "path outside working dir" in blocked


@pytest.mark.parametrize("backend", ["bwrap", "seatbelt"])
def test_exec_sandbox_bind_parent_does_not_widen_workspace_guard(tmp_path, monkeypatch, backend):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    secret = tmp_path / "config.json"
    secret.write_text("secret")
    monkeypatch.setattr("nanobot.agent.tools.shell._IS_WINDOWS", False)
    tool = ExecTool(
        working_dir=str(workspace),
        restrict_to_workspace=True,
        sandbox=backend,
        sandbox_ro_binds=[str(tmp_path)],
    )

    blocked = tool._guard_command(
        f"cat {secret}",
        str(workspace),
        restrict_to_workspace=True,
        workspace_root=str(workspace),
    )

    assert blocked is not None
    assert "path outside working dir" in blocked


# --- format command blocking -----------------------------------------------

# ---------------------------------------------------------------------------
# MIT-123: shell command pre-screen against the secret-dump denylist.
#
# `_guard_command` is the single safety chokepoint for `ExecTool`. It must
# reject commands whose purpose is to dump environment variables, read SSH /
# GPG keys, cat credential files, base64 key material for exfil, etc.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        # env-dumpers
        "printenv",
        "env",
        "env | grep SECRET",
        "export -p",
        # SSH / key file reads
        "cat ~/.ssh/id_rsa",
        "less ~/.ssh/id_rsa.pub",
        # shadow / credential file reads
        "cat /etc/shadow",
        "cat ./.env",
        "cat app/.env.production",
        "cat /root/credentials.json",
        # exfil via base64 / xxd
        "base64 ~/.ssh/id_rsa",
        "base64 server.pem",
        "xxd ~/.ssh/id_ed25519",
        # key management tools
        "ssh-add -l",
        "gpg --export-secret-keys",
    ],
)
def test_exec_prescreens_secret_dump_commands(command):
    """Every command in the sensitive-data denylist must be blocked."""
    tool = ExecTool()
    result = tool._guard_command(command, "/tmp")
    assert result is not None, f"expected block for: {command!r}"
    # Error phrasing must match the rest of _guard_command ("safety guard"),
    # not the raw "security policy" wording from sensitive.py. This alignment
    # is the whole reason for translating the result locally.
    assert result.startswith("Error: Command blocked by safety guard"), result
    assert "sensitive data" in result.lower()


@pytest.mark.parametrize(
    "command",
    [
        "format C: /q",
        "format D: /fs:ntfs",
        "&& format",
        "| format",
        "&format",
        ";format",
        "|format",
    ],
)

def test_exec_blocks_format_command(command):
    """The Windows ``format`` disk command must be denied."""
    tool = ExecTool()
    result = tool._guard_command(command, "/tmp")
    assert result is not None
    assert "deny pattern filter" in result.lower()


@pytest.mark.parametrize(
    "command",
    [
        # URL parameter &format= must NOT be blocked (regression).
        'curl -s "wttr.in/xxx?lang=zh&format=%l:+%c+%t+%h+%w&1"',
        'curl -s "wttr.in/xxx?format=%l:+%c+%t+%h+%w&1"',
        # format as a non-command word in a normal argument.
        "echo format",
        "echo reformat",
    ],
)

def test_exec_allows_format_in_url_and_args(command):
    """``format`` inside URL parameters or as a non-command arg must be allowed."""
    tool = ExecTool()
    result = tool._guard_command(command, "/tmp")
    assert result is None


# --- workspace_root allows paths inside workspace but outside cwd ----------


def test_exec_allows_workspace_paths_from_subdirectory(tmp_path):
    """Absolute paths inside the workspace root must be allowed even when cwd
    is a subdirectory.  This is the scenario reported in the issue: git
    commands in ``~/.nanobot/workspace/obsidian_notes`` reference paths
    under the broader workspace that are outside the subdirectory cwd."""
    workspace = tmp_path / "workspace"
    subdir = workspace / "obsidian_notes"
    subdir.mkdir(parents=True)
    sibling = workspace / "other_project"
    sibling.mkdir()

    tool = ExecTool(working_dir=str(workspace), restrict_to_workspace=True)

    # A command run from the subdirectory that references a sibling path
    # inside the workspace should be allowed.
    result = tool._guard_command(
        f"git clone {sibling}",
        str(subdir),
        workspace_root=str(workspace),
    )
    assert result is None


def test_exec_blocks_outside_paths_from_subdirectory(tmp_path):
    """Paths truly outside the workspace must still be blocked even when
    workspace_root is provided."""
    workspace = tmp_path / "workspace"
    subdir = workspace / "project"
    subdir.mkdir(parents=True)
    outside = tmp_path / "secrets"
    outside.mkdir()

    tool = ExecTool(working_dir=str(workspace), restrict_to_workspace=True)

    result = tool._guard_command(
        f"cat {outside / 'key.pem'}",
        str(subdir),
        workspace_root=str(workspace),
    )
    assert result is not None
    assert "path outside working dir" in result

def test_exec_blocks_outside_paths_with_redirection_and_delimiters(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "secrets"
    outside.mkdir()

    tool = ExecTool(working_dir=str(workspace), restrict_to_workspace=True)

    for cmd in (
        f"cat<{outside / 'key.pem'}",
        f"cat <{outside / 'key.pem'}",
        f"({outside / 'key.pem'})",
        f"cat {{{outside / 'key.pem'}}}",
    ):
        result = tool._guard_command(cmd, str(workspace), workspace_root=str(workspace))
        assert result is not None, f"Expected {cmd} to be blocked"
        assert "path outside working dir" in result


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink and quoting semantics")
@pytest.mark.parametrize("quoted", [True, False])
def test_exec_does_not_truncate_parentheses_in_symlink_paths(tmp_path, quoted):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("secret")
    link = workspace / "linked)dir"
    link.symlink_to(outside, target_is_directory=True)
    escaped_link = str(link).replace(")", r"\)")
    rendered = f'"{link}/secret.txt"' if quoted else f"{escaped_link}/secret.txt"
    command = f"cat {rendered}"
    tool = ExecTool(working_dir=str(workspace), restrict_to_workspace=True)

    assert f"{link}/secret.txt" in tool._extract_absolute_paths(command)
    result = tool._guard_command(command, str(workspace), workspace_root=str(workspace))

    assert result is not None
    assert "path outside working dir" in result


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX command substitution semantics")
def test_exec_checks_leaf_symlink_inside_command_substitution(tmp_path):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    link = workspace / "secret-link"
    link.symlink_to(outside, target_is_directory=True)
    command = f'cat "$(printf %s {link})"'
    tool = ExecTool(working_dir=str(workspace), restrict_to_workspace=True)

    assert str(link) in tool._extract_absolute_paths(command)
    result = tool._guard_command(command, str(workspace), workspace_root=str(workspace))

    assert result is not None
    assert "path outside working dir" in result


@pytest.mark.parametrize(
    ("command", "not_a_posix_path"),
    [
        ("curl https://example.com/outside/file", "/outside/file"),
        ("curl 'https://example.com/?next=/etc/passwd'", "/etc/passwd"),
        ("curl --url=https://example.com/?next=/etc/passwd", "/etc/passwd"),
        ("scp host:/etc/passwd .", "/etc/passwd"),
        ("echo C:/Windows/System32", "/Windows/System32"),
    ],
)
def test_exec_does_not_misclassify_nonlocal_slash_strings(command, not_a_posix_path):
    assert not_a_posix_path not in ExecTool._extract_absolute_paths(command)


def test_exec_extracts_quoted_path_with_shell_punctuation():
    path = "/tmp/a file)/with, punctuation"

    assert ExecTool._extract_absolute_paths(f'cat "{path}"') == [path]


@pytest.mark.parametrize("uri", ["file:///etc/passwd", "file://localhost/%65tc/passwd"])
def test_exec_extracts_local_file_uri(uri):
    assert "/etc/passwd" in ExecTool._extract_absolute_paths(f"curl {uri}")


def test_exec_blocks_file_uri_outside_workspace(tmp_path):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside file.txt"
    workspace.mkdir()
    outside.write_text("secret")
    tool = ExecTool(working_dir=str(workspace), restrict_to_workspace=True)

    result = tool._guard_command(
        f"curl {outside.as_uri()}",
        str(workspace),
        workspace_root=str(workspace),
    )

    assert result is not None
    assert "path outside working dir" in result


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX command substitution semantics")
def test_exec_checks_file_uri_leaf_symlink_inside_command_substitution(tmp_path):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    link = workspace / "secret-link"
    link.symlink_to(outside, target_is_directory=True)
    command = f'curl "$(printf file://{link})"'
    tool = ExecTool(working_dir=str(workspace), restrict_to_workspace=True)

    assert str(link) in tool._extract_absolute_paths(command)
    result = tool._guard_command(command, str(workspace), workspace_root=str(workspace))

    assert result is not None
    assert "path outside working dir" in result


def test_exec_keeps_quoted_parenthesis_path_inside_workspace_allowed(tmp_path):
    workspace = tmp_path / "workspace"
    inside = workspace / "linked)dir" / "file.txt"
    inside.parent.mkdir(parents=True)
    inside.write_text("safe")
    tool = ExecTool(working_dir=str(workspace), restrict_to_workspace=True)

    assert tool._guard_command(
        f'cat "{inside}"',
        str(workspace),
        workspace_root=str(workspace),
    ) is None


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink and assignment semantics")
def test_exec_keeps_quoted_assignment_punctuation_inside_workspace_allowed(tmp_path):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (workspace / "linked").symlink_to(outside, target_is_directory=True)
    inside = workspace / "linked;dir" / "file.txt"
    inside.parent.mkdir()
    inside.write_text("safe")
    tool = ExecTool(working_dir=str(workspace), restrict_to_workspace=True)

    assert tool._guard_command(
        f'x="{inside}"; cat "$x"',
        str(workspace),
        workspace_root=str(workspace),
    ) is None


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell command-string semantics")
def test_exec_recursively_checks_compact_shell_command_string(tmp_path):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    link = workspace / "secret-link"
    link.symlink_to(outside, target_is_directory=True)
    command = f'sh -c "x={link};cat \\"$x\\""'
    tool = ExecTool(working_dir=str(workspace), restrict_to_workspace=True)

    assert str(link) in tool._extract_absolute_paths(command)
    result = tool._guard_command(command, str(workspace), workspace_root=str(workspace))

    assert result is not None
    assert "path outside working dir" in result


def test_exec_malformed_quote_still_extracts_path():
    assert "/etc/passwd" in ExecTool._extract_absolute_paths('cat "/etc/passwd')


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX double-slash path semantics")
@pytest.mark.parametrize("path", ["//etc/passwd", "///etc/passwd"])
def test_exec_blocks_double_slash_absolute_paths(tmp_path, path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    tool = ExecTool(working_dir=str(workspace), restrict_to_workspace=True)

    assert path in tool._extract_absolute_paths(f"cat {path}")
    result = tool._guard_command(
        f"cat {path}",
        str(workspace),
        workspace_root=str(workspace),
    )

    assert result is not None
    assert "path outside working dir" in result


@pytest.mark.parametrize(
    "command",
    [
        # env with an inline variable assignment is a command runner, not a dump
        "env FOO=bar make build",
        # cat of regular files is fine
        "cat README.md",
        "cat src/main.py",
        # ls / grep of normal paths is fine
        "ls ~/projects",
        "grep TODO notes.txt",
        # base64 of a regular payload
        "echo hello | base64",
        # curl to public URL (not a secret dumper)
        "curl https://example.com/api",
    ],
)
def test_exec_prescreen_allows_legitimate_commands(command):
    """Legitimate commands that happen to mention env/cat/base64 must pass."""
    tool = ExecTool()
    result = tool._guard_command(command, "/tmp")
    # Result may be None (allowed) or blocked for an unrelated reason, but
    # must not be blocked with the sensitive-data message.
    if result is not None:
        assert "sensitive data" not in result.lower(), (
            f"command was wrongly flagged as sensitive-data access: {command!r} -> {result!r}"
        )
