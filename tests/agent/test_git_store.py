"""Tests for GitStore — git-backed version control for memory files."""

import os
import subprocess
from unittest.mock import patch

import pytest
from dulwich.repo import Repo

from nanobot.utils.gitstore import (
    DEFAULT_TRACKED_PATTERNS,
    CommitInfo,
    GitStore,
    GitStoreError,
    UndoResult,
    history_dir_for,
)

TRACKED = ["SOUL.md", "USER.md", "memory/MEMORY.md"]


@pytest.fixture
def git(tmp_path):
    """Uninitialized GitStore (bare history next to the workspace)."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return GitStore(workspace, tracked_files=TRACKED, git_dir=history_dir_for(workspace) / "workspace.git")


@pytest.fixture
def git_ready(git):
    """Initialized GitStore with one initial commit."""
    git.init()
    return git


class TestInit:
    def test_not_initialized_by_default(self, git):
        assert not git.is_initialized()
        assert not git._git_dir.exists()

    def test_init_creates_bare_repo(self, git):
        assert git.init()
        assert (git._git_dir / "HEAD").is_file()
        assert not (git._workspace / ".git").exists()

    def test_init_idempotent(self, git_ready):
        assert not git_ready.init()

    def test_init_makes_initial_commit(self, git_ready):
        commits = git_ready.log()
        assert len(commits) == 1
        assert "init" in commits[0].message

    def test_init_failure_is_explicit(self, git):
        with patch("dulwich.repo.Repo.init_bare", side_effect=OSError("cannot initialize")):
            with pytest.raises(GitStoreError, match="init failed"):
                git.init()


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


def _seed(ws, **files):
    for rel, text in files.items():
        path = ws / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


@pytest.fixture
def seeded(git):
    """Initialized store whose init commit already holds the tracked files."""
    _seed(git._workspace, **{"SOUL.md": "", "USER.md": "", "memory/MEMORY.md": ""})
    git.init()
    return git


class TestUndo:
    def test_returns_empty_when_not_initialized(self, git):
        assert git.undo("abc") == UndoResult()

    def test_undoes_commit_changes(self, seeded):
        ws = seeded._workspace
        (ws / "SOUL.md").write_text("v1 content", encoding="utf-8")
        seeded.auto_commit("v1")
        (ws / "SOUL.md").write_text("v2 content", encoding="utf-8")
        sha = seeded.auto_commit("v2")

        result = seeded.undo(sha)

        assert result.restored == ["SOUL.md"]
        assert result.new_sha is not None
        assert seeded.log()[0].message == f"undo {sha}"
        assert (ws / "SOUL.md").read_text(encoding="utf-8") == "v1 content"

    def test_undo_keeps_later_edits_to_other_files(self, seeded):
        """MIT-1847: undoing commit A must not discard commit B's later edit."""
        ws = seeded._workspace
        (ws / "USER.md").write_text("user edit", encoding="utf-8")
        sha_a = seeded.auto_commit("dream: edit user")
        (ws / "SOUL.md").write_text("soul edit", encoding="utf-8")
        seeded.auto_commit("edit soul")

        result = seeded.undo(sha_a)

        assert result.restored == ["USER.md"]
        assert (ws / "USER.md").read_text(encoding="utf-8") == ""
        assert (ws / "SOUL.md").read_text(encoding="utf-8") == "soul edit"

    def test_undo_skips_file_changed_later(self, seeded):
        """MIT-1847: a later edit to the same file (committed or not) survives."""
        ws = seeded._workspace
        (ws / "USER.md").write_text("dream value", encoding="utf-8")
        (ws / "SOUL.md").write_text("dream soul", encoding="utf-8")
        sha = seeded.auto_commit("dream: edit")
        (ws / "USER.md").write_text("edited again later", encoding="utf-8")
        seeded.auto_commit("owner edit")

        result = seeded.undo(sha)

        assert result.restored == ["SOUL.md"]
        assert result.skipped == ["USER.md"]
        assert (ws / "USER.md").read_text(encoding="utf-8") == "edited again later"
        assert (ws / "SOUL.md").read_text(encoding="utf-8") == ""

    def test_undo_restores_deleted_file(self, seeded):
        ws = seeded._workspace
        (ws / "SOUL.md").write_text("soul", encoding="utf-8")
        seeded.auto_commit("write soul")
        (ws / "SOUL.md").unlink()
        delete_sha = seeded.auto_commit("delete soul")

        result = seeded.undo(delete_sha)

        assert result.restored == ["SOUL.md"]
        assert (ws / "SOUL.md").read_text(encoding="utf-8") == "soul"

    def test_undo_keeps_file_added_by_commit(self, seeded):
        """SM-01 invariant: a path missing from the parent tree is never
        deleted — its prior state is unknown."""
        ws = seeded._workspace
        (ws / "memory" / "MEMORY.md").unlink()
        seeded.auto_commit("delete memory file")
        (ws / "memory" / "MEMORY.md").write_text("new file", encoding="utf-8")
        add_sha = seeded.auto_commit("add memory file back")

        result = seeded.undo(add_sha)

        assert result.kept == ["memory/MEMORY.md"]
        assert result.restored == [] and result.new_sha is None
        assert (ws / "memory" / "MEMORY.md").read_text(encoding="utf-8") == "new file"

    def test_root_commit_is_refused(self, seeded):
        commits = seeded.log()
        assert len(commits) == 1
        assert seeded.undo(commits[0].sha) == UndoResult()

    def test_invalid_sha_returns_empty(self, seeded):
        assert seeded.undo("deadbeef") == UndoResult()

    def test_message_prefix_rejects_unrelated_commit_without_changing_files(self, seeded):
        ws = seeded._workspace
        (ws / "SOUL.md").write_text("dream v1", encoding="utf-8")
        seeded.auto_commit("dream: v1")
        (ws / "SOUL.md").write_text("backup state", encoding="utf-8")
        backup_sha = seeded.auto_commit("backup: workspace")
        (ws / "SOUL.md").write_text("dream v2", encoding="utf-8")
        latest_sha = seeded.auto_commit("dream: v2")

        assert seeded.undo(backup_sha, message_prefix="dream:") == UndoResult()
        assert (ws / "SOUL.md").read_text(encoding="utf-8") == "dream v2"
        assert seeded.log()[0].sha == latest_sha

    def test_undo_never_overwrites_a_symlink(self, seeded, tmp_path):
        """A tracked path replaced by a symlink is skipped; the link and its
        target are untouched."""
        ws = seeded._workspace
        (ws / "SOUL.md").write_text("dream soul", encoding="utf-8")
        sha = seeded.auto_commit("dream: soul")
        outside = tmp_path / "outside.md"
        outside.write_text("dream soul", encoding="utf-8")
        (ws / "SOUL.md").unlink()
        os.symlink(outside, ws / "SOUL.md")

        result = seeded.undo(sha)

        assert result.skipped == ["SOUL.md"]
        assert (ws / "SOUL.md").is_symlink()
        assert outside.read_text(encoding="utf-8") == "dream soul"


class TestRestore:
    def test_returns_empty_when_not_initialized(self, git):
        assert git.restore("abc") == UndoResult()
        assert git.restore_preview("abc") == UndoResult()

    def test_restore_sets_every_recorded_path(self, seeded):
        ws = seeded._workspace
        _seed(ws, **{"SOUL.md": "soul old", "USER.md": "user old"})
        snapshot_sha = seeded.auto_commit("snapshot")
        _seed(ws, **{"SOUL.md": "soul new", "USER.md": "user new", "memory/MEMORY.md": "mem new"})
        seeded.auto_commit("later edits")

        result = seeded.restore(snapshot_sha)

        assert result.restored == ["SOUL.md", "USER.md", "memory/MEMORY.md"]
        assert (ws / "SOUL.md").read_text(encoding="utf-8") == "soul old"
        assert (ws / "USER.md").read_text(encoding="utf-8") == "user old"
        assert (ws / "memory" / "MEMORY.md").read_text(encoding="utf-8") == ""
        assert seeded.log()[0].message == f"restore {snapshot_sha}"

    def test_restore_snapshots_uncommitted_edits_first(self, seeded):
        """Overwritten content must stay recoverable from history."""
        ws = seeded._workspace
        snapshot_sha = seeded.log()[0].sha
        (ws / "SOUL.md").write_text("uncommitted edit", encoding="utf-8")

        result = seeded.restore(snapshot_sha)

        assert result.restored == ["SOUL.md"]
        assert (ws / "SOUL.md").read_text(encoding="utf-8") == ""
        messages = [c.message for c in seeded.log()]
        assert messages[:2] == [f"restore {snapshot_sha}", f"snapshot: before restore {snapshot_sha}"]
        undo = seeded.undo(seeded.log()[0].sha)
        assert (ws / "SOUL.md").read_text(encoding="utf-8") == "uncommitted edit", undo

    def test_restore_keeps_tracked_files_added_later(self, seeded):
        ws = seeded._workspace
        (ws / "memory" / "MEMORY.md").unlink()
        deleted_sha = seeded.auto_commit("delete memory file")
        (ws / "memory" / "MEMORY.md").write_text("added later", encoding="utf-8")
        seeded.auto_commit("add memory file back")

        assert seeded.restore_preview(deleted_sha).kept == ["memory/MEMORY.md"]
        result = seeded.restore(deleted_sha)

        assert result.kept == ["memory/MEMORY.md"]
        assert result.restored == []
        assert (ws / "memory" / "MEMORY.md").read_text(encoding="utf-8") == "added later"

    def test_restore_preview_lists_only_changed_paths(self, seeded):
        ws = seeded._workspace
        (ws / "SOUL.md").write_text("soul old", encoding="utf-8")
        snapshot_sha = seeded.auto_commit("snapshot")
        (ws / "SOUL.md").write_text("soul new", encoding="utf-8")

        assert seeded.restore_preview(snapshot_sha).restored == ["SOUL.md"]
        assert (ws / "SOUL.md").read_text(encoding="utf-8") == "soul new"
        assert seeded.restore(snapshot_sha).restored == ["SOUL.md"]
        assert seeded.restore_preview(snapshot_sha).restored == []

    def test_restore_unknown_sha(self, seeded):
        assert seeded.restore_preview("deadbeef") == UndoResult()
        assert seeded.restore("deadbeef") == UndoResult()

    def test_restore_refuses_to_overwrite_symlink(self, seeded, tmp_path):
        ws = seeded._workspace
        snapshot_sha = seeded.log()[0].sha
        outside = tmp_path / "outside.md"
        outside.write_text("not in history", encoding="utf-8")
        (ws / "SOUL.md").unlink()
        os.symlink(outside, ws / "SOUL.md")
        (ws / "USER.md").write_text("user later", encoding="utf-8")
        head = seeded.log()[0].sha

        result = seeded.restore(snapshot_sha)

        assert result.conflicts == ["SOUL.md"] and result.restored == []
        assert (ws / "SOUL.md").is_symlink()
        assert outside.read_text(encoding="utf-8") == "not in history"
        assert (ws / "USER.md").read_text(encoding="utf-8") == "user later"
        assert seeded.log()[0].sha == head


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

    def test_memory_store_uses_bare_history_outside_workspace(self, tmp_path):
        """SM-01: MemoryStore's GitStore points at <ws>/../history/workspace.git."""
        from nanobot.agent.memory import MemoryStore
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        store = MemoryStore(workspace)
        assert store.git._git_dir == history_dir_for(workspace) / "workspace.git"


class TestBareWorkspaceHistory:
    """SM-01 (MIT-1842): bare history repo outside the workspace, byte blobs,
    pattern-tracked files, legacy migration."""

    @pytest.fixture
    def ws(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        return workspace

    @pytest.fixture
    def bare(self, ws):
        return GitStore(
            ws,
            tracked_files=DEFAULT_TRACKED_PATTERNS,
            git_dir=history_dir_for(ws) / "workspace.git",
        )

    def _tree_at_head(self, store: GitStore) -> tuple[Repo, object]:
        """Open the bare repo and return (repo, HEAD tree). Caller closes repo."""
        repo = Repo(str(store._git_dir))
        return repo, repo[repo[repo.refs[b"HEAD"]].tree]

    def test_history_dir_for_derives_only_from_workspace(self, tmp_path):
        ws = tmp_path / "tenant" / "workspace"
        ws.mkdir(parents=True)
        assert history_dir_for(ws) == ws.resolve().parent / "history"

    def test_bare_store_lives_outside_workspace(self, ws):
        store = GitStore(
            ws,
            tracked_files=DEFAULT_TRACKED_PATTERNS,
            git_dir=history_dir_for(ws) / "workspace.git",
        )
        assert store.init() is True
        (ws / "SOUL.md").write_text("# soul", encoding="utf-8")
        assert store.auto_commit("turn t1") is not None
        assert not (ws / ".git").exists()
        assert not (ws / ".gitignore").exists()
        assert (ws.parent / "history" / "workspace.git" / "HEAD").exists()

    def test_bare_is_initialized_checks_git_dir(self, bare, ws):
        assert not bare.is_initialized()
        bare.init()
        assert bare.is_initialized()
        assert not (ws / ".git").is_dir()

    def test_tracks_skills_and_binary_assets(self, bare, ws):
        binary = b"\x89PNG\r\n\x1a\n\xff\xfe\x00\x01\x80\x00"
        (ws / "skills" / "x" / "assets").mkdir(parents=True)
        (ws / "skills" / "x" / "assets" / "a.bin").write_bytes(binary)
        (ws / "skills" / "_proposed" / "draft").mkdir(parents=True)
        (ws / "skills" / "_proposed" / "draft" / "SKILL.md").write_text(
            "draft skill", encoding="utf-8"
        )
        (ws / "prompts").mkdir()
        (ws / "prompts" / "dream.md").write_text("dream", encoding="utf-8")
        (ws / "prompts" / "notes.txt").write_text("not tracked", encoding="utf-8")
        (ws / "memory").mkdir()
        bare.init()

        repo, tree = self._tree_at_head(bare)
        try:
            assert GitStore._read_blob_from_tree(repo, tree, "skills/x/assets/a.bin") == binary
            assert (
                GitStore._read_blob_from_tree(
                    repo, tree, "skills/_proposed/draft/SKILL.md"
                )
                == b"draft skill"
            )
            assert GitStore._read_blob_from_tree(repo, tree, "prompts/dream.md") == b"dream"
            # glob is *.md only
            assert GitStore._read_blob_from_tree(repo, tree, "prompts/notes.txt") is None
        finally:
            repo.close()

    def test_untracked_files_are_excluded(self, bare, ws):
        (ws / "memory").mkdir()
        (ws / "memory" / "history.jsonl").write_text("append-only", encoding="utf-8")
        (ws / "memory" / ".dream_cursor").write_text("42", encoding="utf-8")
        (ws / "scratch.txt").write_text("random", encoding="utf-8")
        bare.init()
        repo, tree = self._tree_at_head(bare)
        try:
            assert GitStore._read_blob_from_tree(repo, tree, "memory/history.jsonl") is None
            assert GitStore._read_blob_from_tree(repo, tree, "memory/.dream_cursor") is None
            assert GitStore._read_blob_from_tree(repo, tree, "scratch.txt") is None
        finally:
            repo.close()

    def test_symlinks_and_oversized_files_are_skipped(self, bare, ws):
        (ws / "skills").mkdir()
        target = ws / "outside.bin"
        target.write_bytes(b"real")
        (ws / "skills" / "link.bin").symlink_to(target)
        (ws / "skills" / "huge.md").write_bytes(b"x" * (1024 * 1024 + 1))
        (ws / "SOUL.md").write_text("soul", encoding="utf-8")
        bare.init()
        repo, tree = self._tree_at_head(bare)
        try:
            assert GitStore._read_blob_from_tree(repo, tree, "skills/link.bin") is None
            assert GitStore._read_blob_from_tree(repo, tree, "skills/huge.md") is None
            assert GitStore._read_blob_from_tree(repo, tree, "SOUL.md") == b"soul"
        finally:
            repo.close()

    def test_no_commit_when_unchanged(self, bare, ws):
        (ws / "SOUL.md").write_text("v1", encoding="utf-8")
        assert bare.init() is True
        # Same content as the init commit: no second commit.
        assert bare.auto_commit("nothing 1") is None
        assert bare.auto_commit("nothing 2") is None
        assert len(bare.log()) == 1
        (ws / "SOUL.md").write_text("v2", encoding="utf-8")
        sha = bare.auto_commit("changed")
        assert sha is not None and len(sha) == 8
        assert bare.auto_commit("changed again") is None
        assert len(bare.log()) == 2
        assert bare.log()[0].message == "changed"

    def test_same_size_rewrite_with_unchanged_mtime_still_commits(self, bare, ws):
        path = ws / "SOUL.md"
        path.write_text("v1", encoding="utf-8")
        bare.init()
        previous = path.stat()
        path.write_text("v2", encoding="utf-8")
        os.utime(path, ns=(previous.st_atime_ns, previous.st_mtime_ns))
        assert bare.auto_commit("v2") is not None

    def test_removed_files_drop_out_of_tree(self, bare, ws):
        (ws / "prompts").mkdir()
        (ws / "prompts" / "gone.md").write_text("bye", encoding="utf-8")
        bare.init()
        (ws / "prompts" / "gone.md").unlink()
        assert bare.auto_commit("drop prompt") is not None
        repo, tree = self._tree_at_head(bare)
        try:
            assert GitStore._read_blob_from_tree(repo, tree, "prompts/gone.md") is None
        finally:
            repo.close()

    def test_log_and_diff_and_show_work_against_bare_repo(self, bare, ws):
        (ws / "SOUL.md").write_text("original", encoding="utf-8")
        bare.init()
        (ws / "SOUL.md").write_text("modified", encoding="utf-8")
        sha = bare.auto_commit("turn t2")
        assert isinstance(sha, str)

        commits = bare.log()
        assert len(commits) == 2
        assert commits[0].message == "turn t2"
        assert commits[1].message == "init: workspace history"

        diff = bare.diff_commits(commits[1].sha, commits[0].sha)
        assert "modified" in diff

        result = bare.show_commit_diff(sha)
        assert result is not None
        commit, commit_diff = result
        assert commit.sha == sha
        assert "modified" in commit_diff

    def test_bare_history_is_readable_by_git_cli(self, bare, ws):
        """The design requires the host's git to read the dulwich bare repo."""
        (ws / "SOUL.md").write_text("# soul", encoding="utf-8")
        bare.init()
        out = subprocess.check_output(
            ["git", "--git-dir", str(bare._git_dir), "log", "--format=%an|%ae|%s"],
            text=True,
        )
        assert "ziggy|ziggy@runtime|init: workspace history" in out

    def test_migrates_legacy_dot_git(self, ws):
        # A legacy store (the pre-SM-01 layout) inside the workspace.
        (ws / "SOUL.md").write_text("# old soul", encoding="utf-8")
        subprocess.run(["git", "init", "-q", str(ws)], check=True)
        subprocess.run(["git", "-C", str(ws), "add", "SOUL.md"], check=True)
        subprocess.run(
            ["git", "-C", str(ws), "-c", "user.name=nanobot", "-c", "user.email=n@d",
             "commit", "-q", "-m", "init: nanobot memory store"],
            check=True,
        )
        legacy_head = (ws / ".git" / "HEAD").read_text(encoding="utf-8")
        (ws / ".gitignore").write_text("/*\n", encoding="utf-8")
        (ws / "SOUL.md").write_text("# soul", encoding="utf-8")

        store = GitStore(
            ws,
            tracked_files=DEFAULT_TRACKED_PATTERNS,
            git_dir=history_dir_for(ws) / "workspace.git",
        )
        assert store.init() is True

        assert not (ws / ".git").exists()
        # Old history parked outside the workspace, still readable.
        legacy = ws.parent / "history" / "legacy-dot-git"
        assert (legacy / "HEAD").read_text(encoding="utf-8") == legacy_head
        out = subprocess.run(
            ["git", "--git-dir", str(legacy), "log", "--format=%s"],
            capture_output=True, text=True, check=True,
        )
        assert "init: nanobot memory store" in out.stdout
        # The harmless workspace .gitignore is left alone.
        assert (ws / ".gitignore").read_text(encoding="utf-8") == "/*\n"
        # The new bare repo committed the current files.
        assert store.log()[-1].message == "init: workspace history"
        repo, tree = self._tree_at_head(store)
        try:
            assert GitStore._read_blob_from_tree(repo, tree, "SOUL.md") == b"# soul"
        finally:
            repo.close()

    def test_init_is_noop_when_bare_repo_exists(self, ws):
        store = GitStore(
            ws,
            tracked_files=DEFAULT_TRACKED_PATTERNS,
            git_dir=history_dir_for(ws) / "workspace.git",
        )
        assert store.init() is True
        # A leftover legacy .git after the bare repo exists is not touched.
        (ws / ".git").mkdir()
        assert store.init() is False
        assert (ws / ".git").is_dir()

    def test_sync_templates_does_not_recreate_dot_git(self, tmp_path):
        from nanobot.utils.helpers import sync_workspace_templates

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        sync_workspace_templates(workspace, silent=True)
        assert not (workspace / ".git").exists()
        assert not (workspace / ".gitignore").exists()
        assert (tmp_path / "history" / "workspace.git" / "HEAD").exists()
        # And again, simulating every start.
        sync_workspace_templates(workspace, silent=True)
        assert not (workspace / ".git").exists()

    def test_auto_commit_failure_is_explicit(self, bare, ws, monkeypatch):
        bare.init()
        monkeypatch.setattr(
            "dulwich.index.commit_tree",
            lambda *args, **kwargs: (_ for _ in ()).throw(OSError("object store broken")),
        )
        with pytest.raises(GitStoreError, match="auto-commit failed"):
            bare.auto_commit("boom")

    def test_undo_restores_wildcard_tracked_files(self, bare, ws):
        """Codex P2: revert must restore skills/**, prompts/*.md and pipeline
        state (not only literal paths)."""
        (ws / "skills" / "s").mkdir(parents=True)
        (ws / "prompts").mkdir()
        (ws / "memory" / "pipelines").mkdir(parents=True)
        files = {
            "SOUL.md": "soul v1",
            "skills/s/SKILL.md": "skill v1",
            "prompts/p.md": "prompt v1",
            "memory/pipelines/run.md": "pipeline v1",
        }
        for rel, text in files.items():
            (ws / rel).write_text(text, encoding="utf-8")
        bare.init()

        for rel in files:
            (ws / rel).write_text("v2", encoding="utf-8")
        (ws / "skills" / "added").mkdir()
        (ws / "skills" / "added" / "SKILL.md").write_text("new", encoding="utf-8")
        sha = bare.auto_commit("dream: rewrite")

        assert bare.undo(sha, message_prefix="dream:").new_sha is not None
        for rel, text in files.items():
            assert (ws / rel).read_text(encoding="utf-8") == text, rel
        # Added by the commit: prior state unknown, so kept (never deleted).
        assert (ws / "skills" / "added" / "SKILL.md").read_text(encoding="utf-8") == "new"
        # The restored state is itself committed: nothing left to commit.
        assert bare.auto_commit("after undo") is None

    def test_undo_leaves_files_the_commit_did_not_touch(self, bare, ws):
        (ws / "SOUL.md").write_text("soul v1", encoding="utf-8")
        bare.init()
        (ws / "SOUL.md").write_text("soul v2", encoding="utf-8")
        sha = bare.auto_commit("dream: soul")
        (ws / "skills" / "later").mkdir(parents=True)
        (ws / "skills" / "later" / "SKILL.md").write_text("later", encoding="utf-8")
        bare.auto_commit("skill install")

        assert bare.undo(sha, message_prefix="dream:").new_sha is not None
        assert (ws / "SOUL.md").read_text(encoding="utf-8") == "soul v1"
        assert (ws / "skills" / "later" / "SKILL.md").read_text(encoding="utf-8") == "later"

    def test_oversize_file_is_tracked_again_after_it_shrinks(self, bare, ws):
        """Codex P2: the oversize log-once set must not gate eligibility."""
        (ws / "skills").mkdir()
        big = ws / "skills" / "notes.md"
        big.write_bytes(b"x" * (1024 * 1024 + 1))
        bare.init()
        big.write_text("small now", encoding="utf-8")
        assert bare.auto_commit("shrunk") is not None
        repo, tree = self._tree_at_head(bare)
        try:
            assert GitStore._read_blob_from_tree(repo, tree, "skills/notes.md") == b"small now"
        finally:
            repo.close()

    def test_binary_asset_round_trips_through_undo(self, bare, ws):
        """Restore a binary file from history and compare bytes on disk."""
        binary = b"\x89PNG\r\n\x1a\n\xff\xfe\x00\x01\x80\x00" + bytes(range(256))
        asset = ws / "skills" / "x" / "assets" / "a.bin"
        asset.parent.mkdir(parents=True)
        asset.write_bytes(binary)
        bare.init()
        asset.write_bytes(b"\x00corrupted\xff")
        sha = bare.auto_commit("dream: clobber asset")
        assert bare.undo(sha).new_sha is not None
        assert asset.read_bytes() == binary

    def test_migration_falls_back_to_copy_across_filesystems(self, ws, monkeypatch):
        """history/ on another filesystem: rename raises EXDEV, move copies."""
        import errno

        (ws / ".git").mkdir()
        (ws / ".git" / "HEAD").write_text("ref: refs/heads/master\n", encoding="utf-8")
        real_rename = os.rename

        def exdev_rename(src, dst, *args, **kwargs):
            if str(src) == str(ws / ".git"):
                raise OSError(errno.EXDEV, "Invalid cross-device link")
            return real_rename(src, dst, *args, **kwargs)

        monkeypatch.setattr(os, "rename", exdev_rename)
        store = GitStore(
            ws,
            tracked_files=DEFAULT_TRACKED_PATTERNS,
            git_dir=history_dir_for(ws) / "workspace.git",
        )
        assert store.init() is True
        assert not (ws / ".git").exists()
        parked = ws.parent / "history" / "legacy-dot-git" / "HEAD"
        assert parked.read_text(encoding="utf-8") == "ref: refs/heads/master\n"

    def test_undo_never_deletes_a_file_excluded_by_the_size_cap(self, bare, ws):
        """Codex P2 r2: a path absent from the parent tree may have existed
        but been excluded (oversize); revert must not delete it."""
        asset = ws / "skills" / "x" / "big.bin"
        asset.parent.mkdir(parents=True)
        asset.write_bytes(b"x" * (1024 * 1024 + 1))
        (ws / "SOUL.md").write_text("soul v1", encoding="utf-8")
        bare.init()
        asset.write_bytes(b"shrunk")
        (ws / "SOUL.md").write_text("soul v2", encoding="utf-8")
        sha = bare.auto_commit("dream: shrink")
        assert bare.undo(sha).new_sha is not None
        assert asset.read_bytes() == b"shrunk"
        assert (ws / "SOUL.md").read_text(encoding="utf-8") == "soul v1"

    def test_undo_refuses_file_dir_conflict_without_touching_anything(self, bare, ws):
        """Codex P2 r2: all-or-nothing when a file became a directory."""
        (ws / "skills").mkdir()
        (ws / "skills" / "helper").write_text("helper file", encoding="utf-8")
        (ws / "SOUL.md").write_text("soul v1", encoding="utf-8")
        bare.init()
        (ws / "skills" / "helper").unlink()
        (ws / "skills" / "helper").mkdir()
        (ws / "skills" / "helper" / "SKILL.md").write_text("dir now", encoding="utf-8")
        (ws / "SOUL.md").write_text("soul v2", encoding="utf-8")
        sha = bare.auto_commit("dream: restructure")
        head_before = bare.log()[0].sha
        assert bare.undo(sha).conflicts
        assert (ws / "SOUL.md").read_text(encoding="utf-8") == "soul v2"
        assert (ws / "skills" / "helper" / "SKILL.md").read_text(encoding="utf-8") == "dir now"
        assert bare.log()[0].sha == head_before

    def test_undo_refuses_dir_to_file_conflict(self, bare, ws):
        (ws / "skills" / "helper").mkdir(parents=True)
        (ws / "skills" / "helper" / "SKILL.md").write_text("in dir", encoding="utf-8")
        (ws / "SOUL.md").write_text("soul v1", encoding="utf-8")
        bare.init()
        (ws / "skills" / "helper" / "SKILL.md").unlink()
        (ws / "skills" / "helper").rmdir()
        (ws / "skills" / "helper").write_text("file now", encoding="utf-8")
        (ws / "SOUL.md").write_text("soul v2", encoding="utf-8")
        sha = bare.auto_commit("dream: flatten")
        assert bare.undo(sha).conflicts
        assert (ws / "SOUL.md").read_text(encoding="utf-8") == "soul v2"
        assert (ws / "skills" / "helper").read_text(encoding="utf-8") == "file now"

    def test_undo_refuses_symlinked_parent_into_history(self, bare, ws):
        """A tracked directory swapped for a symlink (e.g. into history/) is
        never written through; the whole undo is refused."""
        (ws / "skills" / "s").mkdir(parents=True)
        (ws / "skills" / "s" / "SKILL.md").write_text("v1", encoding="utf-8")
        (ws / "SOUL.md").write_text("soul v1", encoding="utf-8")
        bare.init()
        (ws / "skills" / "s" / "SKILL.md").write_text("v2", encoding="utf-8")
        (ws / "SOUL.md").write_text("soul v2", encoding="utf-8")
        sha = bare.auto_commit("dream: edit")
        decoy = ws.parent / "history" / "s"
        decoy.mkdir()
        (decoy / "SKILL.md").write_text("v2", encoding="utf-8")
        (ws / "skills" / "s" / "SKILL.md").unlink()
        (ws / "skills" / "s").rmdir()
        os.symlink(decoy, ws / "skills" / "s")

        result = bare.undo(sha)

        assert result.conflicts == ["skills/s/SKILL.md"]
        assert result.restored == []
        assert (decoy / "SKILL.md").read_text(encoding="utf-8") == "v2"
        assert (ws / "SOUL.md").read_text(encoding="utf-8") == "soul v2"

    def test_restore_refuses_symlinked_parent_into_history(self, bare, ws):
        (ws / "skills" / "s").mkdir(parents=True)
        (ws / "skills" / "s" / "SKILL.md").write_text("v1", encoding="utf-8")
        bare.init()
        target = bare.log()[0].sha
        decoy = ws.parent / "history" / "s"
        decoy.mkdir()
        (decoy / "SKILL.md").write_text("decoy", encoding="utf-8")
        (ws / "skills" / "s" / "SKILL.md").unlink()
        (ws / "skills" / "s").rmdir()
        os.symlink(decoy, ws / "skills" / "s")

        result = bare.restore(target)

        assert result.conflicts == ["skills/s/SKILL.md"]
        assert (decoy / "SKILL.md").read_text(encoding="utf-8") == "decoy"

    def test_restore_never_overwrites_an_oversize_file(self, bare, ws):
        """An oversize file is not in history, so overwriting it would lose
        data for good: restore refuses with nothing changed."""
        (ws / "skills" / "x").mkdir(parents=True)
        (ws / "skills" / "x" / "notes.md").write_text("small", encoding="utf-8")
        (ws / "SOUL.md").write_text("soul v1", encoding="utf-8")
        bare.init()
        target = bare.log()[0].sha
        big = b"y" * (1024 * 1024 + 1)
        (ws / "skills" / "x" / "notes.md").write_bytes(big)
        (ws / "SOUL.md").write_text("soul v2", encoding="utf-8")
        head = bare.auto_commit("grow")

        assert bare.restore_preview(target).conflicts == ["skills/x/notes.md"]
        result = bare.restore(target)

        assert result.conflicts == ["skills/x/notes.md"]
        assert (ws / "skills" / "x" / "notes.md").read_bytes() == big
        assert (ws / "SOUL.md").read_text(encoding="utf-8") == "soul v2"
        assert bare.log()[0].sha == head

    def test_restore_refuses_file_dir_conflict(self, bare, ws):
        (ws / "skills").mkdir()
        (ws / "skills" / "helper").write_text("helper file", encoding="utf-8")
        (ws / "SOUL.md").write_text("soul v1", encoding="utf-8")
        bare.init()
        target = bare.log()[0].sha
        (ws / "skills" / "helper").unlink()
        (ws / "skills" / "helper").mkdir()
        (ws / "skills" / "helper" / "SKILL.md").write_text("dir now", encoding="utf-8")
        (ws / "SOUL.md").write_text("soul v2", encoding="utf-8")

        result = bare.restore(target)

        assert result.conflicts == ["skills/helper"]
        assert (ws / "SOUL.md").read_text(encoding="utf-8") == "soul v2"
        assert (ws / "skills" / "helper" / "SKILL.md").read_text(encoding="utf-8") == "dir now"

    def test_restore_restores_wildcard_tracked_files(self, bare, ws):
        files = {"skills/s/SKILL.md": "skill v1", "prompts/p.md": "prompt v1"}
        for rel, text in files.items():
            (ws / rel).parent.mkdir(parents=True, exist_ok=True)
            (ws / rel).write_text(text, encoding="utf-8")
        bare.init()
        target = bare.log()[0].sha
        for rel in files:
            (ws / rel).write_text("v2", encoding="utf-8")

        result = bare.restore(target)

        assert result.restored == sorted(files)
        for rel, text in files.items():
            assert (ws / rel).read_text(encoding="utf-8") == text

    def test_write_failure_mid_apply_changes_nothing(self, bare, ws, monkeypatch):
        """Writes are staged first: a failure while staging the second file
        leaves the first untouched (no partial application)."""
        import tempfile

        (ws / "SOUL.md").write_text("soul v1", encoding="utf-8")
        (ws / "USER.md").write_text("user v1", encoding="utf-8")
        bare.init()
        (ws / "SOUL.md").write_text("soul v2", encoding="utf-8")
        (ws / "USER.md").write_text("user v2", encoding="utf-8")
        sha = bare.auto_commit("dream: both")
        real_mkstemp = tempfile.mkstemp
        calls = []

        def flaky_mkstemp(*args, **kwargs):
            calls.append(1)
            if len(calls) == 2:
                raise OSError("disk full")
            return real_mkstemp(*args, **kwargs)

        monkeypatch.setattr(tempfile, "mkstemp", flaky_mkstemp)
        with pytest.raises(GitStoreError):
            bare.undo(sha)

        assert (ws / "SOUL.md").read_text(encoding="utf-8") == "soul v2"
        assert (ws / "USER.md").read_text(encoding="utf-8") == "user v2"
        assert sorted(p.name for p in ws.iterdir()) == ["SOUL.md", "USER.md"]

    def test_rename_failure_mid_apply_rolls_back(self, bare, ws, monkeypatch):
        """Codex P2: a failure in the rename phase (second os.replace) rolls
        back the first rename and leaves no temp or backup files."""
        (ws / "SOUL.md").write_text("soul v1", encoding="utf-8")
        (ws / "USER.md").write_text("user v1", encoding="utf-8")
        bare.init()
        (ws / "SOUL.md").write_text("soul v2", encoding="utf-8")
        (ws / "USER.md").write_text("user v2", encoding="utf-8")
        sha = bare.auto_commit("dream: both")
        head = bare.log()[0].sha
        real_replace = os.replace
        calls = []

        def flaky_replace(src, dst, *args, **kwargs):
            calls.append(dst)
            if len(calls) == 2:
                raise OSError("rename failed")
            return real_replace(src, dst, *args, **kwargs)

        monkeypatch.setattr(os, "replace", flaky_replace)
        with pytest.raises(GitStoreError):
            bare.undo(sha)

        assert len(calls) >= 2
        assert (ws / "SOUL.md").read_text(encoding="utf-8") == "soul v2"
        assert (ws / "USER.md").read_text(encoding="utf-8") == "user v2"
        assert sorted(p.name for p in ws.iterdir()) == ["SOUL.md", "USER.md"]
        assert bare.log()[0].sha == head

    def test_restore_keeps_execute_bit(self, bare, ws):
        """Codex P2: a 0755 skill script stays executable after restore/undo."""
        script = ws / "skills" / "s" / "run.sh"
        script.parent.mkdir(parents=True)
        script.write_text("#!/bin/sh\necho v1\n", encoding="utf-8")
        os.chmod(script, 0o755)
        bare.init()
        target = bare.log()[0].sha
        script.write_text("#!/bin/sh\necho v2\n", encoding="utf-8")
        sha = bare.auto_commit("dream: script")

        assert bare.undo(sha).restored == ["skills/s/run.sh"]
        assert script.read_text(encoding="utf-8") == "#!/bin/sh\necho v1\n"
        assert os.stat(script).st_mode & 0o777 == 0o755
        script.write_text("#!/bin/sh\necho v3\n", encoding="utf-8")
        assert bare.restore(target).restored == ["skills/s/run.sh"]
        assert os.stat(script).st_mode & 0o777 == 0o755

    def test_failed_rollback_keeps_backup_and_reports_it(self, bare, ws, monkeypatch):
        """Codex P2: ENOSPC on the second rename and on the rollback rename.
        The backup is then the only copy of the original: it must survive and
        its path must be in the error."""
        import errno

        (ws / "SOUL.md").write_text("soul v1", encoding="utf-8")
        (ws / "USER.md").write_text("user v1", encoding="utf-8")
        bare.init()
        (ws / "SOUL.md").write_text("soul v2", encoding="utf-8")
        (ws / "USER.md").write_text("user v2", encoding="utf-8")
        sha = bare.auto_commit("dream: both")
        real_replace = os.replace
        calls = []

        def enospc_replace(src, dst, *args, **kwargs):
            calls.append(dst)
            if len(calls) in (2, 3):
                raise OSError(errno.ENOSPC, "No space left on device")
            return real_replace(src, dst, *args, **kwargs)

        monkeypatch.setattr(os, "replace", enospc_replace)
        with pytest.raises(GitStoreError, match="Rollback failed") as info:
            bare.undo(sha)

        backups = [p for p in ws.iterdir() if p.name.endswith(".bak")]
        assert len(backups) == 1
        assert backups[0].read_text(encoding="utf-8") == "soul v2"
        assert str(backups[0]) in str(info.value)
        assert (ws / "USER.md").read_text(encoding="utf-8") == "user v2"
