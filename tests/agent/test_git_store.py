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


class TestRevert:
    def test_returns_none_when_not_initialized(self, git):
        assert git.revert("abc") is None

    def test_undoes_commit_changes(self, git_ready):
        """revert(sha) should undo the given commit by restoring to its parent."""
        ws = git_ready._workspace
        (ws / "SOUL.md").write_text("v1 content", encoding="utf-8")
        git_ready.auto_commit("v1")
        (ws / "SOUL.md").write_text("v2 content", encoding="utf-8")
        git_ready.auto_commit("v2")

        commits = git_ready.log()
        # Revert v2 → SOUL.md back to v1.
        new_sha = git_ready.revert(commits[0].sha)
        assert new_sha is not None
        assert (ws / "SOUL.md").read_text(encoding="utf-8") == "v1 content"

    def test_root_commit_returns_none(self, git_ready):
        """Cannot revert the root commit (no parent to restore to)."""
        commits = git_ready.log()
        assert len(commits) == 1
        assert git_ready.revert(commits[0].sha) is None

    def test_invalid_sha_returns_none(self, git_ready):
        assert git_ready.revert("deadbeef") is None

    def test_message_prefix_rejects_unrelated_commit_without_changing_files(self, git_ready):
        ws = git_ready._workspace
        (ws / "SOUL.md").write_text("dream v1", encoding="utf-8")
        git_ready.auto_commit("dream: v1")
        (ws / "SOUL.md").write_text("backup state", encoding="utf-8")
        backup_sha = git_ready.auto_commit("backup: workspace")
        (ws / "SOUL.md").write_text("dream v2", encoding="utf-8")
        latest_sha = git_ready.auto_commit("dream: v2")

        assert git_ready.revert(backup_sha, message_prefix="dream:") is None
        assert (ws / "SOUL.md").read_text(encoding="utf-8") == "dream v2"
        assert git_ready.log()[0].sha == latest_sha


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

    def test_revert_restores_wildcard_tracked_files(self, bare, ws):
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

        assert bare.revert(sha, message_prefix="dream:") is not None
        for rel, text in files.items():
            assert (ws / rel).read_text(encoding="utf-8") == text, rel
        # Added by the commit: prior state unknown, so kept (never deleted).
        assert (ws / "skills" / "added" / "SKILL.md").read_text(encoding="utf-8") == "new"
        # The restored state is itself committed: nothing left to commit.
        assert bare.auto_commit("after revert") is None

    def test_revert_leaves_files_the_commit_did_not_touch(self, bare, ws):
        (ws / "SOUL.md").write_text("soul v1", encoding="utf-8")
        bare.init()
        (ws / "SOUL.md").write_text("soul v2", encoding="utf-8")
        sha = bare.auto_commit("dream: soul")
        (ws / "skills" / "later").mkdir(parents=True)
        (ws / "skills" / "later" / "SKILL.md").write_text("later", encoding="utf-8")
        bare.auto_commit("skill install")

        assert bare.revert(sha, message_prefix="dream:") is not None
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

    def test_binary_asset_round_trips_through_revert(self, bare, ws):
        """Restore a binary file from history and compare bytes on disk."""
        binary = b"\x89PNG\r\n\x1a\n\xff\xfe\x00\x01\x80\x00" + bytes(range(256))
        asset = ws / "skills" / "x" / "assets" / "a.bin"
        asset.parent.mkdir(parents=True)
        asset.write_bytes(binary)
        bare.init()
        asset.write_bytes(b"\x00corrupted\xff")
        sha = bare.auto_commit("dream: clobber asset")
        assert bare.revert(sha) is not None
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

    def test_revert_never_deletes_a_file_excluded_by_the_size_cap(self, bare, ws):
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
        assert bare.revert(sha) is not None
        assert asset.read_bytes() == b"shrunk"
        assert (ws / "SOUL.md").read_text(encoding="utf-8") == "soul v1"

    def test_revert_refuses_file_dir_conflict_without_touching_anything(self, bare, ws):
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
        assert bare.revert(sha) is None
        assert (ws / "SOUL.md").read_text(encoding="utf-8") == "soul v2"
        assert (ws / "skills" / "helper" / "SKILL.md").read_text(encoding="utf-8") == "dir now"
        assert bare.log()[0].sha == head_before

    def test_revert_refuses_dir_to_file_conflict(self, bare, ws):
        (ws / "skills" / "helper").mkdir(parents=True)
        (ws / "skills" / "helper" / "SKILL.md").write_text("in dir", encoding="utf-8")
        (ws / "SOUL.md").write_text("soul v1", encoding="utf-8")
        bare.init()
        (ws / "skills" / "helper" / "SKILL.md").unlink()
        (ws / "skills" / "helper").rmdir()
        (ws / "skills" / "helper").write_text("file now", encoding="utf-8")
        (ws / "SOUL.md").write_text("soul v2", encoding="utf-8")
        sha = bare.auto_commit("dream: flatten")
        assert bare.revert(sha) is None
        assert (ws / "SOUL.md").read_text(encoding="utf-8") == "soul v2"
        assert (ws / "skills" / "helper").read_text(encoding="utf-8") == "file now"
