"""Tests for GitStore core operations."""

import subprocess
from pathlib import Path

import pytest

from nanobot.utils.gitstore import GitStore


def _store(workspace: Path, tracked: list[str]) -> GitStore:
    return GitStore(workspace, tracked_files=tracked, git_dir=workspace / "_history.git")


@pytest.fixture
def git(tmp_path):
    """Create an initialized GitStore tracking MEMORY.md and SOUL.md."""
    (tmp_path / "MEMORY.md").write_text("", encoding="utf-8")
    (tmp_path / "SOUL.md").write_text("", encoding="utf-8")
    g = _store(tmp_path, ["MEMORY.md", "SOUL.md"])
    g.init()
    return g


class TestSummarizeWorkingTree:
    """Ground-truth diff summary used to keep Dream audit records honest."""

    def test_empty_when_not_initialized(self, tmp_path):
        git = _store(tmp_path, ["MEMORY.md"])
        assert git.summarize_working_tree(["MEMORY.md"]) == ""

    def test_empty_when_no_changes(self, git):
        assert git.summarize_working_tree(["MEMORY.md", "SOUL.md"]) == ""

    def test_summarizes_real_change(self, git, tmp_path):
        (tmp_path / "MEMORY.md").write_text("# Memory\n- new fact\n", encoding="utf-8")
        summary = git.summarize_working_tree(["MEMORY.md"])
        assert "MEMORY.md: +2 -0" in summary
        assert "new fact" in summary
        assert "1 file changed, 2 insertions(+), 0 deletions(-)" in summary

    def test_only_reports_requested_paths(self, git, tmp_path):
        # MEMORY.md changes, but we only ask about the unchanged SOUL.md.
        (tmp_path / "MEMORY.md").write_text("changed\n", encoding="utf-8")
        assert git.summarize_working_tree(["SOUL.md"]) == ""

    def test_counts_additions_and_removals(self, git, tmp_path):
        (tmp_path / "MEMORY.md").write_text("# M\n- keep\n- new\n", encoding="utf-8")
        summary = git.summarize_working_tree(["MEMORY.md"])
        assert "MEMORY.md: +3 -0" in summary

    def test_detects_deletion(self, git, tmp_path):
        # File removed from the working tree (must have content first; the
        # fixture's tracked files start empty, so an empty-file delete is a no-op).
        (tmp_path / "MEMORY.md").write_text("has content\n", encoding="utf-8")
        git.auto_commit("add content")
        (tmp_path / "MEMORY.md").unlink()
        summary = git.summarize_working_tree(["MEMORY.md"])
        assert summary  # a removal is still a change
        assert "deletion" in summary

    def test_non_utf8_file_marked_binary_without_replacement_chars(self, git, tmp_path):
        # Invalid UTF-8 must not leak replacement chars into the audit record.
        (tmp_path / "MEMORY.md").write_bytes(b"\x89PNG\r\n\x1a\n\xff\xfe\x00\x01")
        summary = git.summarize_working_tree(["MEMORY.md"])
        assert "MEMORY.md: binary or non-UTF-8 file changed" in summary
        assert "\ufffd" not in summary  # no U+FFFD replacement chars leaked


class TestCommitIdEncoding:
    """Commit ids must be usable with git, not hex-of-hex."""

    def test_auto_commit_returns_the_real_short_sha(self, git, tmp_path):
        (tmp_path / "MEMORY.md").write_text("- a fact\n", encoding="utf-8")
        sha = git.auto_commit("memory update")
        expected = subprocess.run(
            ["git", "--git-dir", str(git._git_dir), "log", "-1", "--format=%h", "--abbrev=8"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert sha == expected

    def test_a_real_git_sha_resolves(self, git, tmp_path):
        (tmp_path / "MEMORY.md").write_text("- a fact\n", encoding="utf-8")
        git.auto_commit("memory update")
        real = subprocess.run(
            ["git", "--git-dir", str(git._git_dir), "log", "-1", "--format=%h", "--abbrev=8"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert git._resolve_sha(real) is not None
