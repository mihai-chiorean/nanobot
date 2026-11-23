"""Tests for /changes, /undo and /restore over a real GitStore."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from nanobot.bus.events import InboundMessage
from nanobot.command.builtin import (
    build_help_text,
    builtin_command_palette,
    cmd_changes,
    cmd_dream_restore,
    cmd_restore,
    cmd_undo,
    register_builtin_commands,
)
from nanobot.command.router import CommandContext, CommandRouter
from nanobot.utils.gitstore import GitStore, history_dir_for

TRACKED = ["SOUL.md", "USER.md", "memory/MEMORY.md"]


def _bare_store(tmp_path) -> GitStore:
    workspace = tmp_path / "workspace"
    (workspace / "memory").mkdir(parents=True, exist_ok=True)
    return GitStore(
        workspace, tracked_files=TRACKED, git_dir=history_dir_for(workspace) / "workspace.git",
    )


def _make_store(tmp_path) -> GitStore:
    store = _bare_store(tmp_path)
    for rel in TRACKED:
        (store._workspace / rel).write_text("", encoding="utf-8")
    store.init()
    return store


def _make_ctx(tmp_path, raw: str, args: str = "") -> tuple:
    git = _make_store(tmp_path)
    msg = InboundMessage(channel="cli", sender_id="u1", chat_id="direct", content=raw)
    loop = SimpleNamespace(consolidator=SimpleNamespace(store=SimpleNamespace(git=git)))
    ctx = CommandContext(
        msg=msg, session=None, key=msg.session_key, raw=raw, args=args, loop=loop,
    )
    return ctx, git


@pytest.mark.asyncio
async def test_changes_lists_recent_commits_one_line_each(tmp_path) -> None:
    ctx, git = _make_ctx(tmp_path, "/changes")
    (git._workspace / "SOUL.md").write_text("soul", encoding="utf-8")
    sha = git.auto_commit("dream: edit soul")

    out = await cmd_changes(ctx)

    line = next(ln for ln in out.content.splitlines() if sha in ln)
    assert line.startswith(f"`{sha}` · ")
    assert "dream: edit soul" in line
    assert "SOUL.md" in line


@pytest.mark.asyncio
async def test_changes_caps_list_at_fifteen_commits(tmp_path) -> None:
    ctx, git = _make_ctx(tmp_path, "/changes")
    shas = []
    for i in range(18):
        (git._workspace / "SOUL.md").write_text(f"v{i}", encoding="utf-8")
        shas.append(git.auto_commit(f"commit {i}"))

    out = await cmd_changes(ctx)

    listed = [sha for sha in shas if f"`{sha}`" in out.content]
    assert len(listed) == 15
    assert shas[0] not in out.content  # oldest dropped
    assert shas[-1] in out.content    # newest kept


@pytest.mark.asyncio
async def test_changes_with_sha_shows_diff(tmp_path) -> None:
    ctx, git = _make_ctx(tmp_path, "/changes deadbeef", args="deadbeef")
    (git._workspace / "USER.md").write_text("unique content", encoding="utf-8")
    sha = git.auto_commit("edit user")
    ctx.args = sha

    out = await cmd_changes(ctx)

    assert "```diff" in out.content
    assert "unique content" in out.content
    assert sha in out.content


@pytest.mark.asyncio
async def test_changes_diff_truncated_at_6000_chars(tmp_path) -> None:
    ctx, git = _make_ctx(tmp_path, "/changes")
    (git._workspace / "SOUL.md").write_text("x\n" * 5000, encoding="utf-8")
    sha = git.auto_commit("huge")
    ctx.args = sha

    out = await cmd_changes(ctx)

    assert "...[diff truncated]" in out.content
    diff_body = out.content.split("```diff", 1)[1]
    assert len(diff_body) < 6200  # 6000 cap plus marker/fence overhead


@pytest.mark.asyncio
async def test_changes_unknown_sha_gives_guidance(tmp_path) -> None:
    ctx, git = _make_ctx(tmp_path, "/changes deadbeef", args="deadbeef")

    out = await cmd_changes(ctx)

    assert "Couldn't find change `deadbeef`" in out.content
    assert "/changes" in out.content


@pytest.mark.asyncio
async def test_undo_restores_and_reports_skipped(tmp_path) -> None:
    ctx, git = _make_ctx(tmp_path, "/undo")
    (git._workspace / "USER.md").write_text("user edit", encoding="utf-8")
    sha_a = git.auto_commit("edit user")
    (git._workspace / "SOUL.md").write_text("soul edit", encoding="utf-8")
    git.auto_commit("edit soul")
    # USER.md gets a later edit, SOUL.md stays as commit B wrote it.
    (git._workspace / "USER.md").write_text("edited later", encoding="utf-8")
    ctx.args = sha_a

    out = await cmd_undo(ctx)

    assert "Skipped `USER.md` — changed again later; use `/restore" in out.content
    assert (git._workspace / "USER.md").read_text(encoding="utf-8") == "edited later"
    assert (git._workspace / "SOUL.md").read_text(encoding="utf-8") == "soul edit"


@pytest.mark.asyncio
async def test_undo_restores_clean_commit(tmp_path) -> None:
    ctx, git = _make_ctx(tmp_path, "/undo")
    (git._workspace / "SOUL.md").write_text("soul edit", encoding="utf-8")
    sha = git.auto_commit("edit soul")
    ctx.args = sha

    out = await cmd_undo(ctx)

    assert "- Restored: `SOUL.md`" in out.content
    assert (git._workspace / "SOUL.md").read_text(encoding="utf-8") == ""
    assert git.log()[0].message == f"undo {sha}"


@pytest.mark.asyncio
async def test_undo_without_args_shows_usage(tmp_path) -> None:
    ctx, git = _make_ctx(tmp_path, "/undo")

    out = await cmd_undo(ctx)

    assert "/undo <sha>" in out.content
    assert "/changes" in out.content


@pytest.mark.asyncio
async def test_undo_unknown_sha(tmp_path) -> None:
    ctx, git = _make_ctx(tmp_path, "/undo", args="deadbeef")
    ctx.args = "deadbeef"

    out = await cmd_undo(ctx)

    assert "Couldn't find change `deadbeef`" in out.content


@pytest.mark.asyncio
async def test_restore_requires_confirm(tmp_path) -> None:
    """Preview must not touch the workspace and must ask for confirmation."""
    ctx, git = _make_ctx(tmp_path, "/restore")
    (git._workspace / "SOUL.md").write_text("old", encoding="utf-8")
    snapshot_sha = git.auto_commit("snapshot")
    (git._workspace / "SOUL.md").write_text("new", encoding="utf-8")
    git.auto_commit("later")
    ctx.args = snapshot_sha

    out = await cmd_restore(ctx)

    assert f"Confirm with `/restore {snapshot_sha} confirm`." in out.content
    assert "`SOUL.md`" in out.content
    # Nothing changed yet — the restore is gated on the confirm token.
    assert (git._workspace / "SOUL.md").read_text(encoding="utf-8") == "new"
    assert git.log()[0].message == "later"


@pytest.mark.asyncio
async def test_restore_confirm_performs_restore(tmp_path) -> None:
    ctx, git = _make_ctx(tmp_path, "/restore")
    (git._workspace / "SOUL.md").write_text("old", encoding="utf-8")
    (git._workspace / "USER.md").write_text("user old", encoding="utf-8")
    snapshot_sha = git.auto_commit("snapshot")
    (git._workspace / "SOUL.md").write_text("new", encoding="utf-8")
    git.auto_commit("later")
    ctx.args = f"{snapshot_sha} confirm"

    out = await cmd_restore(ctx)

    assert (git._workspace / "SOUL.md").read_text(encoding="utf-8") == "old"
    assert (git._workspace / "USER.md").read_text(encoding="utf-8") == "user old"
    assert "Restored memory to" in out.content
    assert "`SOUL.md`" in out.content


@pytest.mark.asyncio
async def test_restore_unknown_sha(tmp_path) -> None:
    ctx, git = _make_ctx(tmp_path, "/restore", args="deadbeef")
    ctx.args = "deadbeef"

    out = await cmd_restore(ctx)

    assert "Couldn't find change `deadbeef`" in out.content


@pytest.mark.asyncio
async def test_restore_when_already_matching_is_noop(tmp_path) -> None:
    ctx, git = _make_ctx(tmp_path, "/restore")
    (git._workspace / "SOUL.md").write_text("stable", encoding="utf-8")
    snapshot_sha = git.auto_commit("snapshot")
    ctx.args = f"{snapshot_sha} confirm"

    out = await cmd_restore(ctx)

    assert "already matches" in out.content
    assert git.log()[0].sha == snapshot_sha


@pytest.mark.asyncio
async def test_dream_restore_keeps_later_non_dream_edits(tmp_path) -> None:
    """MIT-1847: /dream-restore undoes only the Dream commit's own changes and
    never discards a later owner edit, to another file or to the same one."""
    ctx, git = _make_ctx(tmp_path, "/dream-restore")
    ws = git._workspace
    (ws / "memory" / "MEMORY.md").write_text("pre-dream memory", encoding="utf-8")
    (ws / "SOUL.md").write_text("pre-dream soul", encoding="utf-8")
    git.auto_commit("owner baseline")
    (ws / "memory" / "MEMORY.md").write_text("dream memory", encoding="utf-8")
    (ws / "SOUL.md").write_text("dream soul", encoding="utf-8")
    dream_sha = git.auto_commit("dream: consolidate")
    (ws / "SOUL.md").write_text("owner soul after dream", encoding="utf-8")
    (ws / "USER.md").write_text("owner user after dream", encoding="utf-8")
    git.auto_commit("owner edits")
    ctx.args = dream_sha

    out = await cmd_dream_restore(ctx)

    assert (ws / "memory" / "MEMORY.md").read_text(encoding="utf-8") == "pre-dream memory"
    assert (ws / "SOUL.md").read_text(encoding="utf-8") == "owner soul after dream"
    assert (ws / "USER.md").read_text(encoding="utf-8") == "owner user after dream"
    assert "Restored files: `memory/MEMORY.md`" in out.content
    assert f"Skipped `SOUL.md` — changed again later; use `/restore {dream_sha}`" in out.content


@pytest.mark.asyncio
async def test_undo_conflict_reports_nothing_changed(tmp_path) -> None:
    ctx, git = _make_ctx(tmp_path, "/undo")
    ws = git._workspace
    (ws / "SOUL.md").write_text("dream", encoding="utf-8")
    (ws / "USER.md").write_text("dream user", encoding="utf-8")
    sha = git.auto_commit("dream: edit")
    (ws / "SOUL.md").unlink()
    (ws / "SOUL.md").mkdir()
    ctx.args = sha

    out = await cmd_undo(ctx)

    assert out.content.startswith("Nothing changed")
    assert "`SOUL.md`" in out.content
    assert (ws / "USER.md").read_text(encoding="utf-8") == "dream user"


@pytest.mark.asyncio
async def test_commands_reject_uninitialized_store(tmp_path) -> None:
    git = _bare_store(tmp_path)
    msg = InboundMessage(channel="cli", sender_id="u1", chat_id="direct", content="/changes")
    loop = SimpleNamespace(consolidator=SimpleNamespace(store=SimpleNamespace(git=git)))
    ctx = CommandContext(
        msg=msg, session=None, key=msg.session_key, raw="/changes", args="", loop=loop,
    )

    out = await cmd_changes(ctx)

    assert "not initialized" in out.content


def test_history_commands_registered_in_router() -> None:
    router = CommandRouter()
    register_builtin_commands(router)

    assert router.is_dispatchable_command("/changes")
    assert router.is_dispatchable_command("/changes abcd1234")
    assert router.is_dispatchable_command("/undo abcd1234")
    assert router.is_dispatchable_command("/restore abcd1234")
    assert router.is_dispatchable_command("/restore abcd1234 confirm")


def test_history_commands_in_help_and_palette() -> None:
    palette = builtin_command_palette()
    for command, hint in [("/changes", "[sha]"), ("/undo", "<sha>"), ("/restore", "<sha> [confirm]")]:
        spec = next(item for item in palette if item["command"] == command)
        assert spec["accepts_args"] is True
        assert spec["arg_hint"] == hint
        assert f"{command} {hint}" in build_help_text()
