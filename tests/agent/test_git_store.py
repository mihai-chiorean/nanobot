"""Tests for GitStore — git-backed version control for memory files."""

import os
from unittest.mock import patch

import pytest

from nanobot.utils.gitstore import CommitInfo, GitStore, GitStoreError

TRACKED = ["SOUL.md", "USER.md", "memory/MEMORY.md"]


@pytest.fixture
def git(tmp_path):
    """Uninitialized GitStore."""
    return GitStore(tmp_path, tracked_files=TRACKED)


@pytest.fixture
def git_ready(git):
    """Initialized GitStore with one initial commit."""
    git.init()
    return git


class TestInit:
    def test_not_initialized_by_default(self, git, tmp_path):
        assert not git.is_initialized()
        assert not (tmp_path / ".git").is_dir()

    def test_init_creates_git_dir(self, git, tmp_path):
        assert git.init()
        assert (tmp_path / ".git").is_dir()

    def test_init_idempotent(self, git_ready):
        assert not git_ready.init()

    def test_init_creates_gitignore(self, git_ready):
        gi = git_ready._workspace / ".gitignore"
        assert gi.exists()
        content = gi.read_text(encoding="utf-8")
        for f in TRACKED:
            assert f"!{f}" in content

    def test_init_touches_tracked_files(self, git_ready):
        for f in TRACKED:
            assert (git_ready._workspace / f).exists()

    def test_init_makes_initial_commit(self, git_ready):
        commits = git_ready.log()
        assert len(commits) == 1
        assert "init" in commits[0].message

    def test_init_failure_is_explicit(self, git):
        with patch("dulwich.porcelain.init", side_effect=OSError("cannot initialize")):
            with pytest.raises(GitStoreError, match="init failed"):
                git.init()


class TestBuildGitignore:
    def test_subdirectory_dirs(self, git):
        content = git._build_gitignore()
        assert "!memory/\n" in content
        for f in TRACKED:
            assert f"!{f}\n" in content
        assert content.startswith("/*\n")

    def test_root_level_files_no_dir_entries(self, tmp_path):
        gs = GitStore(tmp_path, tracked_files=["a.md", "b.md"])
        content = gs._build_gitignore()
        assert "!a.md\n" in content
        assert "!b.md\n" in content
        dir_lines = [
            line
            for line in content.split("\n")
            if line.startswith("!") and line.endswith("/")
        ]
        assert dir_lines == []


class TestAutoCommit:
    def test_returns_none_when_not_initialized(self, git):
        assert git.auto_commit("test") is None

    def test_commits_file_change(self, git_ready):
        (git_ready._workspace / "SOUL.md").write_text("updated", encoding="utf-8")
        sha = git_ready.auto_commit("update soul")
        assert sha is not None
        assert len(sha) == 8

    def test_returns_none_when_no_changes(self, git_ready):
        assert git_ready.auto_commit("no change") is None

    def test_commit_appears_in_log(self, git_ready):
        ws = git_ready._workspace
        (ws / "SOUL.md").write_text("v2", encoding="utf-8")
        sha = git_ready.auto_commit("update soul")
        commits = git_ready.log()
        assert len(commits) == 2
        assert commits[0].sha == sha

    def test_commits_same_size_rewrite_with_unchanged_mtime(self, git_ready):
        path = git_ready._workspace / "SOUL.md"
        path.write_text("v1", encoding="utf-8")
        git_ready.auto_commit("v1")
        previous_stat = path.stat()

        path.write_text("v2", encoding="utf-8")
        os.utime(path, ns=(previous_stat.st_atime_ns, previous_stat.st_mtime_ns))

        assert git_ready.auto_commit("v2") is not None
        assert [commit.message for commit in git_ready.log()[:2]] == ["v2", "v1"]

    def test_does_not_create_empty_commits(self, git_ready):
        git_ready.auto_commit("nothing 1")
        git_ready.auto_commit("nothing 2")
        assert len(git_ready.log()) == 1  # only init commit

    def test_status_failure_is_explicit(self, git_ready):
        with patch("dulwich.porcelain.status", side_effect=OSError("broken index")):
            with pytest.raises(GitStoreError, match="auto-commit failed"):
                git_ready.auto_commit("update")


class TestLog:
    def test_empty_when_not_initialized(self, git):
        assert git.log() == []

    def test_newest_first(self, git_ready):
        ws = git_ready._workspace
        for i in range(3):
            (ws / "SOUL.md").write_text(f"v{i}", encoding="utf-8")
            git_ready.auto_commit(f"commit {i}")

        commits = git_ready.log()
        assert len(commits) == 4  # init + 3
        assert "commit 2" in commits[0].message
        assert "init" in commits[-1].message

    def test_max_entries(self, git_ready):
        ws = git_ready._workspace
        for i in range(10):
            (ws / "SOUL.md").write_text(f"v{i}", encoding="utf-8")
            git_ready.auto_commit(f"c{i}")
        assert len(git_ready.log(max_entries=3)) == 3

    def test_message_prefix_skips_unrelated_commits_before_counting_limit(self, git_ready):
        ws = git_ready._workspace
        messages = ["dream: older", "backup: first", "dream: latest", "backup: newest"]
        for i, message in enumerate(messages):
            (ws / "SOUL.md").write_text(f"v{i}", encoding="utf-8")
            git_ready.auto_commit(message)

        commits = git_ready.log(max_entries=2, message_prefix="dream:")

        assert [commit.message for commit in commits] == ["dream: latest", "dream: older"]

    def test_commit_info_fields(self, git_ready):
        c = git_ready.log()[0]
        assert isinstance(c, CommitInfo)
        assert len(c.sha) == 8
        assert c.timestamp
        assert c.message


class TestDiffCommits:
    def test_empty_when_not_initialized(self, git):
        assert git.diff_commits("a", "b") == ""

    def test_diff_between_two_commits(self, git_ready):
        ws = git_ready._workspace
        (ws / "SOUL.md").write_text("original", encoding="utf-8")
        git_ready.auto_commit("v1")
        (ws / "SOUL.md").write_text("modified", encoding="utf-8")
        git_ready.auto_commit("v2")

        commits = git_ready.log()
        diff = git_ready.diff_commits(commits[1].sha, commits[0].sha)
        assert "modified" in diff

    def test_invalid_sha_returns_empty(self, git_ready):
        assert git_ready.diff_commits("deadbeef", "cafebabe") == ""


class TestShowCommitDiff:
    def test_returns_commit_with_diff(self, git_ready):
        ws = git_ready._workspace
        (ws / "SOUL.md").write_text("content", encoding="utf-8")
        sha = git_ready.auto_commit("add content")
        result = git_ready.show_commit_diff(sha)
        assert result is not None
        commit, diff = result
        assert commit.sha == sha
        assert "content" in diff

    def test_first_commit_has_empty_diff(self, git_ready):
        init_sha = git_ready.log()[-1].sha
        result = git_ready.show_commit_diff(init_sha)
        assert result is not None
        _, diff = result
        assert diff == ""

    def test_returns_none_for_unknown(self, git_ready):
        assert git_ready.show_commit_diff("deadbeef") is None

    def test_message_prefix_finds_commit_beyond_unrelated_history_window(self, git_ready):
        ws = git_ready._workspace
        (ws / "SOUL.md").write_text("dream content", encoding="utf-8")
        dream_sha = git_ready.auto_commit("dream: latest")
        for i in range(20):
            (ws / "SOUL.md").write_text(f"backup {i}", encoding="utf-8")
            git_ready.auto_commit(f"backup: {i}")

        result = git_ready.show_commit_diff(
            dream_sha,
            max_entries=1,
            message_prefix="dream:",
        )

        assert result is not None
        commit, diff = result
        assert commit.sha == dream_sha
        assert "dream content" in diff


class TestCommitInfoFormat:
    def test_format_with_diff(self):
        from nanobot.utils.gitstore import CommitInfo
        c = CommitInfo(sha="abcd1234", message="test commit\nsecond line", timestamp="2026-04-02 12:00")
        result = c.format(diff="some diff")
        assert "test commit" in result
        assert "`abcd1234`" in result
        assert "some diff" in result

    def test_format_without_diff(self):
        from nanobot.utils.gitstore import CommitInfo
        c = CommitInfo(sha="abcd1234", message="test", timestamp="2026-04-02 12:00")
        result = c.format()
        assert "(no file changes)" in result

    def test_format_empty_message(self):
        from nanobot.utils.gitstore import CommitInfo
        c = CommitInfo(sha="abcd1234", message="", timestamp="2026-04-02 12:00")
        result = c.format()
        assert "(no message)" in result
        assert "`abcd1234`" in result
        assert c.subject() == "(no message)"


class TestUndo:
    def test_returns_empty_when_not_initialized(self, git):
        result = git.undo("abc")
        assert (result.restored, result.skipped, result.new_sha) == ([], [], None)

    def test_undo_keeps_later_edits_to_other_files(self, git_ready):
        """Undoing a commit must not discard edits from other commits."""
        ws = git_ready._workspace
        (ws / "USER.md").write_text("user edit", encoding="utf-8")
        sha_a = git_ready.auto_commit("edit user")
        (ws / "SOUL.md").write_text("soul edit", encoding="utf-8")
        git_ready.auto_commit("edit soul")

        result = git_ready.undo(sha_a)

        assert result.restored == ["USER.md"]
        assert result.skipped == []
        assert result.new_sha is not None
        assert (ws / "USER.md").read_text(encoding="utf-8") == ""
        # Commit B's later edit must survive undoing commit A.
        assert (ws / "SOUL.md").read_text(encoding="utf-8") == "soul edit"

    def test_undo_skips_file_changed_later(self, git_ready):
        ws = git_ready._workspace
        (ws / "USER.md").write_text("committed value", encoding="utf-8")
        sha = git_ready.auto_commit("edit user")
        # Later edit on top of the commit — undo must refuse to clobber it.
        (ws / "USER.md").write_text("edited again later", encoding="utf-8")

        result = git_ready.undo(sha)

        assert result.restored == []
        assert result.skipped == ["USER.md"]
        assert result.new_sha is None
        assert (ws / "USER.md").read_text(encoding="utf-8") == "edited again later"

    def test_undo_deletes_added_file(self, git_ready):
        """Undoing a commit that added a file deletes the file again."""
        ws = git_ready._workspace
        (ws / "memory" / "MEMORY.md").unlink()
        git_ready.auto_commit("delete memory file")
        (ws / "memory" / "MEMORY.md").write_text("new file", encoding="utf-8")
        add_sha = git_ready.auto_commit("add memory file back")

        result = git_ready.undo(add_sha)

        assert result.restored == ["memory/MEMORY.md"]
        assert result.new_sha is not None
        assert not (ws / "memory" / "MEMORY.md").exists()

    def test_undo_restores_deleted_file(self, git_ready):
        ws = git_ready._workspace
        (ws / "SOUL.md").write_text("soul", encoding="utf-8")
        git_ready.auto_commit("write soul")
        (ws / "SOUL.md").unlink()
        delete_sha = git_ready.auto_commit("delete soul")

        result = git_ready.undo(delete_sha)

        assert result.restored == ["SOUL.md"]
        assert (ws / "SOUL.md").read_text(encoding="utf-8") == "soul"

    def test_undo_creates_named_commit(self, git_ready):
        ws = git_ready._workspace
        (ws / "SOUL.md").write_text("v2 content", encoding="utf-8")
        sha = git_ready.auto_commit("v2")

        result = git_ready.undo(sha)

        assert result.new_sha is not None
        assert git_ready.log()[0].message == f"undo {sha}"
        assert (ws / "SOUL.md").read_text(encoding="utf-8") == ""

    def test_root_commit_is_refused(self, git_ready):
        commits = git_ready.log()
        assert len(commits) == 1

        result = git_ready.undo(commits[0].sha)

        assert (result.restored, result.skipped, result.new_sha) == ([], [], None)

    def test_invalid_sha_returns_empty(self, git_ready):
        result = git_ready.undo("deadbeef")
        assert (result.restored, result.skipped, result.new_sha) == ([], [], None)

    def test_undo_replaces_symlink_with_parent_state(self, git_ready):
        """A symlink added over a tracked file is undone to the parent's file."""
        ws = git_ready._workspace
        (ws / "SOUL.md").unlink()
        os.symlink("../link-target.md", ws / "SOUL.md")
        link_sha = git_ready.auto_commit("symlink soul")

        result = git_ready.undo(link_sha)

        assert result.restored == ["SOUL.md"]
        assert not (ws / "SOUL.md").is_symlink()
        assert (ws / "SOUL.md").read_text(encoding="utf-8") == ""

    def test_undo_skips_symlink_edited_later(self, git_ready):
        ws = git_ready._workspace
        (ws / "SOUL.md").unlink()
        os.symlink("../link-target.md", ws / "SOUL.md")
        link_sha = git_ready.auto_commit("symlink soul")
        (ws / "SOUL.md").unlink()
        os.symlink("../elsewhere.md", ws / "SOUL.md")

        result = git_ready.undo(link_sha)

        assert result.skipped == ["SOUL.md"]
        assert os.readlink(ws / "SOUL.md") == "../elsewhere.md"

    def test_undo_deletes_symlink_added_by_commit(self, git_ready):
        ws = git_ready._workspace
        (ws / "SOUL.md").unlink()
        git_ready.auto_commit("drop soul")
        os.symlink("../link-target.md", ws / "SOUL.md")
        add_sha = git_ready.auto_commit("link soul")

        result = git_ready.undo(add_sha)

        assert result.restored == ["SOUL.md"]
        assert not (ws / "SOUL.md").is_symlink()
        assert not (ws / "SOUL.md").exists()

    def test_undo_uses_parent_of_first_parent_only_for_touched_files(self, git_ready):
        """A commit's undo must not touch tracked files it never modified."""
        ws = git_ready._workspace
        (ws / "SOUL.md").write_text("soul v1", encoding="utf-8")
        git_ready.auto_commit("soul v1")
        (ws / "USER.md").write_text("user v1", encoding="utf-8")
        user_sha = git_ready.auto_commit("user v1")
        (ws / "SOUL.md").write_text("soul v2", encoding="utf-8")
        git_ready.auto_commit("soul v2")

        git_ready.undo(user_sha)

        assert (ws / "SOUL.md").read_text(encoding="utf-8") == "soul v2"


class TestRestore:
    def test_returns_empty_when_not_initialized(self, git):
        assert git.restore("abc") == []
        assert git.restore_preview("abc") == []

    def test_restore_sets_every_tracked_path(self, git_ready):
        ws = git_ready._workspace
        (ws / "SOUL.md").write_text("soul old", encoding="utf-8")
        (ws / "USER.md").write_text("user old", encoding="utf-8")
        snapshot_sha = git_ready.auto_commit("snapshot")
        (ws / "SOUL.md").write_text("soul new", encoding="utf-8")
        (ws / "USER.md").write_text("user new", encoding="utf-8")
        (ws / "memory" / "MEMORY.md").write_text("memory new", encoding="utf-8")
        git_ready.auto_commit("later edits")

        changed = git_ready.restore(snapshot_sha)

        assert changed == ["SOUL.md", "USER.md", "memory/MEMORY.md"]
        assert (ws / "SOUL.md").read_text(encoding="utf-8") == "soul old"
        assert (ws / "USER.md").read_text(encoding="utf-8") == "user old"
        assert (ws / "memory" / "MEMORY.md").read_text(encoding="utf-8") == ""

    def test_restore_deletes_tracked_files_added_later(self, git_ready):
        """Files that did not exist at the target commit are removed."""
        ws = git_ready._workspace
        (ws / "memory" / "MEMORY.md").unlink()
        deleted_sha = git_ready.auto_commit("delete memory file")
        (ws / "memory" / "MEMORY.md").write_text("added later", encoding="utf-8")
        git_ready.auto_commit("add memory file back")

        changed = git_ready.restore(deleted_sha)

        assert "memory/MEMORY.md" in changed
        assert not (ws / "memory" / "MEMORY.md").exists()

    def test_restore_creates_commit(self, git_ready):
        ws = git_ready._workspace
        (ws / "SOUL.md").write_text("snapshot", encoding="utf-8")
        snapshot_sha = git_ready.auto_commit("snapshot")
        (ws / "SOUL.md").write_text("drift", encoding="utf-8")
        git_ready.auto_commit("drift")

        git_ready.restore(snapshot_sha)

        assert git_ready.log()[0].message == f"restore {snapshot_sha}"

    def test_restore_preview_lists_only_changed_paths(self, git_ready):
        ws = git_ready._workspace
        (ws / "SOUL.md").write_text("soul old", encoding="utf-8")
        snapshot_sha = git_ready.auto_commit("snapshot")
        (ws / "SOUL.md").write_text("soul new", encoding="utf-8")

        assert git_ready.restore_preview(snapshot_sha) == ["SOUL.md"]
        assert git_ready.restore(snapshot_sha) == ["SOUL.md"]
        assert git_ready.restore_preview(snapshot_sha) == []

    def test_restore_preview_unknown_sha(self, git_ready):
        assert git_ready.restore_preview("deadbeef") == []
        assert git_ready.restore("deadbeef") == []

    def test_restore_recreates_symlink_instead_of_clobbering(self, git_ready):
        """A symlinked tracked file is restored as a symlink, not as a file
        whose content is the link target."""
        ws = git_ready._workspace
        (ws / "SOUL.md").unlink()
        os.symlink("../link-target.md", ws / "SOUL.md")
        snapshot_sha = git_ready.auto_commit("symlink soul")
        (ws / "SOUL.md").unlink()
        (ws / "SOUL.md").write_text("clobbered", encoding="utf-8")

        assert git_ready.restore_preview(snapshot_sha) == ["SOUL.md"]
        changed = git_ready.restore(snapshot_sha)

        assert changed == ["SOUL.md"]
        assert (ws / "SOUL.md").is_symlink()
        assert os.readlink(ws / "SOUL.md") == "../link-target.md"


class TestHasCommit:
    def test_false_when_not_initialized(self, git):
        assert git.has_commit("abc") is False

    def test_true_for_known_false_for_unknown(self, git_ready):
        ws = git_ready._workspace
        (ws / "SOUL.md").write_text("v", encoding="utf-8")
        sha = git_ready.auto_commit("v")
        assert git_ready.has_commit(sha) is True
        assert git_ready.has_commit("deadbeef") is False


class TestLogIncludeFiles:
    def test_files_listed_per_commit(self, git_ready):
        ws = git_ready._workspace
        (ws / "SOUL.md").write_text("soul", encoding="utf-8")
        (ws / "USER.md").write_text("user", encoding="utf-8")
        git_ready.auto_commit("both")

        commits = git_ready.log(max_entries=1, include_files=True)

        assert commits[0].files == ["SOUL.md", "USER.md"]

    def test_files_empty_by_default(self, git_ready):
        ws = git_ready._workspace
        (ws / "SOUL.md").write_text("soul", encoding="utf-8")
        git_ready.auto_commit("soul")
        assert git_ready.log(max_entries=1)[0].files == []


class TestMemoryStoreGitProperty:
    def test_git_property_exposes_gitstore(self, tmp_path):
        from nanobot.agent.memory import MemoryStore
        store = MemoryStore(tmp_path)
        assert isinstance(store.git, GitStore)

    def test_git_property_is_same_object(self, tmp_path):
        from nanobot.agent.memory import MemoryStore
        store = MemoryStore(tmp_path)
        assert store.git is store._git
