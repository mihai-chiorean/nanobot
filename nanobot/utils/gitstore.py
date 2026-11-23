"""Git-backed version control for memory files, using dulwich."""

from __future__ import annotations

import io
import os
import shutil
import stat
import tempfile
import time
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from loguru import logger

if TYPE_CHECKING:
    from dulwich.objects import Blob, Commit, ObjectID, Tree
    from dulwich.refs import Ref
    from dulwich.repo import Repo

# Cap on the unified-diff block embedded in Dream commit messages. Memory files
# are tiny in practice, but a pathological rewrite must not blow up the audit
# record. The structured per-file summary is always emitted in full regardless.
_WORKING_TREE_DIFF_MAX_CHARS = 6000

# SM-01 (MIT-1842): tracked-file patterns for the workspace history. Expanded
# at commit time relative to the workspace, so new skills/prompts/pipeline
# files are covered without re-instantiating the store. Deliberately excludes
# memory/history.jsonl (append-only, large; already in recall + backups) and
# memory/.dream_cursor (bookkeeping; restoring it would make Dream re-consume
# old history).
DEFAULT_TRACKED_PATTERNS: list[str] = [
    "AGENTS.md",
    "SOUL.md",
    "USER.md",
    "HEARTBEAT.md",
    "memory/MEMORY.md",
    "memory/archive.md",
    "memory/provenance.jsonl",
    "memory/pipelines/*.md",
    "prompts/*.md",
    "skills/**",
]

# Tracked files over this size are skipped (skill assets are the realistic
# case; anything this big in the instruction set is an accident).
_MAX_TRACKED_FILE_BYTES = 1024 * 1024

# Commit identity for bare (outside-the-workspace) history commits.
_BARE_COMMIT_IDENTITY = b"ziggy <ziggy@runtime>"


def history_dir_for(workspace: Path) -> Path:
    """Return the workspace's history directory: ``<workspace>/../history``.

    SM-01 (MIT-1842): the agent's undo history must live where its file tools
    cannot reach. Derived *only* from the workspace path — never from config
    or tool arguments — so no caller can point it at another workspace.
    """
    return Path(workspace).resolve().parent / "history"


# Workspace states that history cannot represent (see _workspace_state).
_CONFLICT = object()
_UNTRACKABLE = object()


class GitStoreError(RuntimeError):
    """Raised when the memory Git repository cannot complete an operation."""


@dataclass
class CommitInfo:
    sha: str  # Short SHA (8 chars)
    message: str
    timestamp: str  # Formatted datetime
    files: list[str] = field(default_factory=list)  # Paths the commit changed

    def subject(self) -> str:
        """First line of the commit message, or a placeholder if empty."""
        lines = self.message.splitlines()
        return lines[0] if lines else "(no message)"

    def format(self, diff: str = "") -> str:
        """Format this commit for display, optionally with a diff."""
        header = f"## {self.subject()}\n`{self.sha}` — {self.timestamp}\n"
        if diff:
            return f"{header}\n```diff\n{diff}\n```"
        return f"{header}\n(no file changes)"


@dataclass
class UndoResult:
    """Outcome of :meth:`GitStore.undo` / :meth:`GitStore.restore`.

    restored: paths written (or, for a preview, that would be).
    skipped: undo only — changed again after the commit; left alone.
    kept: no earlier version in history; left alone, never deleted.
    conflicts: file/directory or symlink conflicts; when non-empty the
    whole operation was refused and nothing changed.
    """

    restored: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    new_sha: str | None = None


class GitStore:
    """Git-backed version control for workspace instruction and memory files.

    SM-01 (MIT-1842): a bare repo outside the workspace
    (``history_dir_for(workspace) / "workspace.git"``), so the agent's file
    tools cannot rewrite its own undo history. Commits read the tracked files
    as bytes and write blobs + a tree straight into the object store; there is
    no index, no checkout, no ``<workspace>/.git`` and no ``.gitignore``.
    """

    def __init__(self, workspace: Path, tracked_files: list[str], git_dir: Path):
        self._workspace = workspace
        self._tracked_files = list(tracked_files)
        self._git_dir = Path(git_dir)
        # Rate-limit the "tracked file too large" warning: one line per path.
        self._oversize_logged: set[str] = set()

    def is_initialized(self) -> bool:
        """Check if the git repo has been initialized."""
        return self._git_dir.is_dir()

    # -- init ------------------------------------------------------------------

    def init(self) -> bool:
        """Initialize the bare repo at git_dir if not already initialized.

        First moves any legacy ``<workspace>/.git`` (the old in-workspace store
        the agent could reach) out of the workspace to
        ``<history>/legacy-dot-git``. Returns True if a new repo was created,
        False if it already exists.
        """
        if self.is_initialized():
            return False
        return self._init_bare()

    def _init_bare(self) -> bool:
        """Initialize the bare repo at git_dir (MIT-1842).

        Migrates a legacy ``<workspace>/.git`` (the old in-workspace store the
        agent could reach) to ``<history>/legacy-dot-git`` so the old Dream
        history stays readable with the host's ``git``. The workspace
        ``.gitignore`` the old store wrote is harmless and left alone. Then
        makes an initial commit of the tracked patterns as they are now.
        """
        try:
            from dulwich.repo import Repo

            history = self._git_dir.parent
            history.mkdir(parents=True, exist_ok=True)
            os.chmod(history, 0o700)

            legacy = self._workspace / ".git"
            parked = history / "legacy-dot-git"
            if legacy.is_dir() and not legacy.is_symlink() and not parked.exists():
                # shutil.move renames, and falls back to copy + remove-old
                # when history/ is on another filesystem (EXDEV).
                shutil.move(str(legacy), str(parked))
                logger.info("Migrated legacy workspace git repo {} to {}", legacy, parked)

            self._git_dir.mkdir(parents=True, exist_ok=True)
            os.chmod(self._git_dir, 0o700)
            Repo.init_bare(str(self._git_dir))
            self._bare_commit("init: workspace history")
            logger.info("Git store initialized at {}", self._git_dir)
            return True
        except Exception as exc:
            raise GitStoreError(f"Git store init failed for {self._workspace}") from exc

    # -- bare-mode tracked-file expansion and committing ------------------------

    def _expand_tracked_files(self) -> dict[str, bytes]:
        """Expand tracked-file patterns against the workspace, reading bytes.

        Skips symlinks, files over ``_MAX_TRACKED_FILE_BYTES`` (logged once
        per path) and missing files. Files that were removed simply drop out
        of the resulting tree.
        """
        files: dict[str, bytes] = {}
        for pattern in self._tracked_files:
            for candidate in self._expand_pattern(pattern):
                try:
                    if candidate.is_symlink() or not candidate.is_file():
                        continue
                    rel = candidate.relative_to(self._workspace).as_posix()
                    size = candidate.stat().st_size
                    if size > _MAX_TRACKED_FILE_BYTES:
                        # The set only rate-limits the warning; eligibility is
                        # re-checked every commit so a shrunk file comes back.
                        if rel not in self._oversize_logged:
                            self._oversize_logged.add(rel)
                            logger.warning(
                                "Git store: skipping tracked file {} ({} bytes > {} limit)",
                                rel,
                                size,
                                _MAX_TRACKED_FILE_BYTES,
                            )
                        continue
                    self._oversize_logged.discard(rel)
                    files[rel] = candidate.read_bytes()
                except OSError:
                    continue
        return files

    def _expand_pattern(self, pattern: str) -> list[Path]:
        """All candidate paths for one tracked-file pattern."""
        if pattern.endswith("**"):
            base = self._workspace / pattern.rstrip("*").rstrip("/")
            out: list[Path] = []
            if base.is_dir() and not base.is_symlink():
                for root, dirs, names in os.walk(base, followlinks=False):
                    # Never descend through a symlinked directory.
                    dirs[:] = sorted(
                        d for d in dirs if not os.path.islink(os.path.join(root, d))
                    )
                    for name in sorted(names):
                        out.append(Path(root) / name)
            return out
        if any(ch in pattern for ch in "*?["):
            parent_rel = Path(pattern).parent
            name = Path(pattern).name
            parent = self._workspace / parent_rel
            if not parent.is_dir() or parent.is_symlink():
                return []
            return [parent / match for match in sorted(parent.glob(name))]
        return [self._workspace / pattern]

    def _bare_build_tree(self, repo: Repo) -> bytes:
        """Write blobs for every tracked file and return the root tree id."""
        from dulwich.index import commit_tree
        from dulwich.objects import Blob

        entries: list[tuple[bytes, bytes, int]] = []
        for rel, data in self._expand_tracked_files().items():
            blob = Blob.from_string(data)
            repo.object_store.add_object(blob)
            entries.append((rel.encode("utf-8"), blob.id, 0o100644))
        return cast(bytes, commit_tree(repo.object_store, sorted(entries)))

    def _bare_commit(self, message: str) -> bytes | None:
        """Commit the current tracked files to the bare repo.

        Returns the full commit id (ASCII bytes), or None when the tree
        matches HEAD (nothing changed).
        """
        from dulwich.repo import Repo

        msg_bytes = message.encode("utf-8") if isinstance(message, str) else message
        with Repo(str(self._git_dir)) as repo:
            tree_id = self._bare_build_tree(repo)
            try:
                head_sha: ObjectID | None = repo.refs[cast("Ref", b"HEAD")]
            except KeyError:
                head_sha = None
            if head_sha is not None:
                head_obj = repo[head_sha]
                if head_obj.type_name == b"commit" and cast("Commit", head_obj).tree == tree_id:
                    return None
            sha = repo.get_worktree().commit(
                tree=tree_id,
                message=msg_bytes,
                author=_BARE_COMMIT_IDENTITY,
                committer=_BARE_COMMIT_IDENTITY,
            )
        return sha

    # -- daily operations ------------------------------------------------------

    def auto_commit(self, message: str) -> str | None:
        """Commit the tracked files if they differ from HEAD.

        Returns the short commit SHA, or None if nothing to commit.
        """
        if not self.is_initialized():
            return None

        try:
            sha = self._bare_commit(message)
            if sha is None:
                return None
            # worktree.commit returns the full id as ASCII bytes; decode
            # before slicing or the short sha is a bytes repr no git command
            # can resolve.
            short = sha.decode()[:8]
            logger.debug("Git auto-commit: {} ({})", short, message)
            return short
        except Exception as exc:
            raise GitStoreError(f"Git auto-commit failed: {message}") from exc

    def snapshot_uncommitted_paths(self, message: str) -> str | None:
        """Commit the tracked files if any exists on disk but not in HEAD.

        Revert never deletes a path missing from the parent tree, so a file
        whose pre-existing content never entered history (e.g. an
        ``memory/archive.md`` written before it was tracked) could not be
        restored after its first edit. Snapshotting before that edit puts the
        original content in history. Returns the short sha, or None when
        every on-disk tracked file is already in HEAD.
        """
        if not self.is_initialized():
            return None
        try:
            from dulwich.repo import Repo

            with Repo(str(self._git_dir)) as repo:
                head_tree = self._head_tree(repo)
                committed = (
                    set(self._tree_blobs(repo, head_tree.id)) if head_tree is not None else set()
                )
            if set(self._expand_tracked_files()) <= committed:
                return None
        except Exception as exc:
            raise GitStoreError(f"Git snapshot check failed: {message}") from exc
        return self.auto_commit(message)

    # -- internal helpers ------------------------------------------------------

    def _resolve_sha(self, short_sha: str) -> bytes | None:
        """Resolve a short SHA prefix to the full SHA bytes."""
        try:
            from dulwich.repo import Repo

            with Repo(str(self._git_dir)) as repo:
                try:
                    sha: ObjectID | None = repo.refs[cast("Ref", b"HEAD")]
                except KeyError:
                    return None

                while sha:
                    if sha.decode().startswith(short_sha):
                        return sha
                    commit_obj = repo[sha]
                    if commit_obj.type_name != b"commit":
                        break
                    commit = cast("Commit", commit_obj)
                    sha = commit.parents[0] if commit.parents else None
            return None
        except Exception as exc:
            raise GitStoreError(f"Git SHA resolution failed: {short_sha}") from exc

    # -- query -----------------------------------------------------------------

    def log(
        self,
        max_entries: int = 20,
        message_prefix: str | None = None,
        include_files: bool = False,
    ) -> list[CommitInfo]:
        """Return simplified commit log, optionally filtered by message prefix.

        When filtering, *max_entries* counts matching commits rather than every
        commit traversed in the repository. With *include_files*, each entry
        lists the paths the commit changed against its first parent.
        """
        if not self.is_initialized():
            return []

        try:
            from dulwich.repo import Repo

            entries: list[CommitInfo] = []
            with Repo(str(self._git_dir)) as repo:
                try:
                    head = repo.refs[cast("Ref", b"HEAD")]
                except KeyError:
                    return []

                sha: ObjectID | None = head
                while sha and len(entries) < max_entries:
                    commit_obj = repo[sha]
                    if commit_obj.type_name != b"commit":
                        break
                    commit = cast("Commit", commit_obj)
                    ts = time.strftime(
                        "%Y-%m-%d %H:%M",
                        time.localtime(commit.commit_time),
                    )
                    msg = commit.message.decode("utf-8", errors="replace").strip()
                    if message_prefix is None or msg.startswith(message_prefix):
                        entries.append(CommitInfo(
                            sha=sha.decode()[:8],
                            message=msg,
                            timestamp=ts,
                            files=self._changed_paths(repo, commit) if include_files else [],
                        ))
                    sha = commit.parents[0] if commit.parents else None

            return entries
        except Exception as exc:
            raise GitStoreError("Git log failed") from exc

    def diff_commits(self, sha1: str, sha2: str) -> str:
        """Show diff between two commits."""
        if not self.is_initialized():
            return ""

        try:
            from dulwich import porcelain

            full1 = self._resolve_sha(sha1)
            full2 = self._resolve_sha(sha2)
            if not full1 or not full2:
                return ""

            out = io.BytesIO()
            porcelain.diff(
                str(self._git_dir),
                commit=full1,
                commit2=full2,
                outstream=out,
            )
            return out.getvalue().decode("utf-8", errors="replace")
        except Exception as exc:
            raise GitStoreError(f"Git diff failed for {sha1}..{sha2}") from exc

    def summarize_working_tree(self, paths: list[str]) -> str:
        """Structured summary of working-tree changes vs HEAD for *paths*.

        Pure filesystem/git ground truth — never LLM narrative — suitable as a
        truthful audit record. Returns "" when the repo is not initialized or
        none of *paths* differ from HEAD.

        Format::

            SOUL.md: +3 -1
            memory/MEMORY.md: +12 -8

            2 files changed, 15 insertions(+), 9 deletions(-)

            ```diff
            --- SOUL.md
            +++ SOUL.md
            @@ ...
            - old
            + new
            ```
        """
        if not self.is_initialized():
            return ""

        try:
            import difflib

            from dulwich.repo import Repo
        except ImportError as exc:
            raise GitStoreError("Git working-tree summary dependencies are unavailable") from exc

        summary_lines: list[str] = []
        diff_lines: list[str] = []
        total_added = 0
        total_removed = 0
        changed = 0

        try:
            with Repo(str(self._git_dir)) as repo:
                head_tree = self._head_tree(repo)
                for path in paths:
                    head_bytes = (
                        self._read_blob_from_tree(repo, head_tree, path)
                        if head_tree is not None
                        else None
                    )
                    # Same replacement-decode the str-returning reader used, so
                    # binary head content never leaks U+FFFD via the wt-side
                    # binary guard below.
                    head_text = (
                        head_bytes.decode("utf-8", errors="replace")
                        if head_bytes is not None
                        else ""
                    )
                    wt_path = self._workspace / path
                    try:
                        wt_text = (
                            wt_path.read_bytes().decode("utf-8")
                            if wt_path.exists()
                            else ""
                        )
                    except UnicodeDecodeError:
                        # Non-UTF-8 (binary/corrupt) working-tree file: record
                        # the change without a unified diff, which would
                        # otherwise be polluted with replacement characters and
                        # misrepresent the audit record.
                        changed += 1
                        summary_lines.append(f"{path}: binary or non-UTF-8 file changed")
                        continue
                    # Treat CRLF and LF as equivalent without hiding other
                    # newline changes, such as a missing final newline.
                    if head_text.replace("\r\n", "\n") == wt_text.replace("\r\n", "\n"):
                        continue
                    head_lines = head_text.splitlines()
                    wt_lines = wt_text.splitlines()
                    changed += 1
                    hunks = list(difflib.unified_diff(
                        head_lines,
                        wt_lines,
                        fromfile=path,
                        tofile=path,
                        lineterm="",
                    ))
                    added = sum(1 for line in hunks if line.startswith("+") and not line.startswith("+++"))
                    removed = sum(1 for line in hunks if line.startswith("-") and not line.startswith("---"))
                    total_added += added
                    total_removed += removed
                    summary_lines.append(f"{path}: +{added} -{removed}")
                    diff_lines.extend(hunks)
        except Exception as exc:
            raise GitStoreError("Git working-tree summary failed") from exc

        if changed == 0:
            return ""

        diff_text = "\n".join(diff_lines)
        if len(diff_text) > _WORKING_TREE_DIFF_MAX_CHARS:
            diff_text = diff_text[:_WORKING_TREE_DIFF_MAX_CHARS] + "\n...[diff truncated]"

        body = "\n".join(summary_lines)
        body += (
            f"\n{changed} file{'s' if changed != 1 else ''} changed, "
            f"{total_added} insertion{'s' if total_added != 1 else ''}(+), "
            f"{total_removed} deletion{'s' if total_removed != 1 else ''}(-)"
        )
        if diff_lines:
            body += f"\n\n```diff\n{diff_text}\n```"
        return body

    @staticmethod
    def _head_tree(repo: "Repo") -> "Tree | None":
        """Return the tree object at HEAD, or None if there are no commits."""
        try:
            head = repo.refs[cast("Ref", b"HEAD")]
        except KeyError:
            return None
        commit_obj = repo[head]
        if commit_obj.type_name != b"commit":
            return None
        commit = cast("Commit", commit_obj)
        return cast("Tree", repo[commit.tree])

    def show_commit_diff(
        self,
        short_sha: str,
        max_entries: int = 20,
        message_prefix: str | None = None,
    ) -> tuple[CommitInfo, str] | None:
        """Find a commit and return it with its diff vs its actual parent."""
        try:
            from dulwich.repo import Repo

            commits = self.log(max_entries=max_entries, message_prefix=message_prefix)
            for c in commits:
                if c.sha.startswith(short_sha):
                    full_sha = self._resolve_sha(c.sha)
                    if not full_sha:
                        return None
                    with Repo(str(self._git_dir)) as repo:
                        commit = cast("Commit", repo[full_sha])
                        parent = commit.parents[0] if commit.parents else None
                    diff = self.diff_commits(parent.decode()[:8], c.sha) if parent else ""
                    return c, diff
            return None
        except Exception as exc:
            raise GitStoreError(f"Git commit display failed for {short_sha}") from exc

    # -- undo / restore --------------------------------------------------------
    #
    # MIT-1847 (SM-03), on top of the SM-01 invariants:
    # - paths come from history trees, so wildcard-tracked files are covered;
    # - a path missing from the source tree is never deleted (it may have
    #   existed but been excluded: oversize, symlink, not yet tracked);
    # - a file the operation would overwrite must already be in history
    #   (undo: it still matches the undone commit; restore: snapshot first),
    #   and anything history cannot hold (directory, symlink, oversize file)
    #   is never overwritten;
    # - a file/directory conflict or a symlinked parent directory refuses the
    #   whole operation before anything is written;
    # - writes are staged to temp files first, then renamed into place.

    def has_commit(self, sha: str) -> bool:
        """Return whether *sha* resolves to a commit reachable from HEAD."""
        if not self.is_initialized():
            return False
        return self._resolve_sha(sha) is not None

    def undo(self, sha: str, *, message_prefix: str | None = None) -> UndoResult:
        """Undo the changes one commit introduced, per file.

        A changed path goes back to the commit's parent version only while the
        workspace file still matches the commit's version; files changed again
        later are skipped, so later edits are never discarded. Paths the commit
        added are kept (prior state unknown). Commits ``undo <sha>`` when
        anything was restored. Root commits, unknown SHAs and commits outside
        *message_prefix* return an empty result with nothing changed.
        """
        if not self.is_initialized():
            return UndoResult()
        try:
            from dulwich.repo import Repo

            with Repo(str(self._git_dir)) as repo:
                commit, full_sha = self._lookup_commit(repo, sha, message_prefix)
                if commit is None or not commit.parents:
                    if commit is not None:
                        logger.warning("Git undo: cannot undo root commit {}", sha)
                    return UndoResult()
                parent = cast("Commit", repo[commit.parents[0]])
                before = self._tree_blobs(repo, parent.tree)
                after = self._tree_blobs(repo, commit.tree)
                result = UndoResult()
                writes: list[tuple[str, bytes]] = []
                for path in sorted(before.keys() | after.keys()):
                    if before.get(path) == after.get(path):
                        continue
                    state = self._workspace_state(path)
                    if state is _CONFLICT:
                        result.conflicts.append(path)
                        continue
                    expected = (
                        cast("Blob", repo[after[path]]).data if path in after else None
                    )
                    if state != expected:
                        result.skipped.append(path)
                    elif path not in before:
                        result.kept.append(path)
                    else:
                        writes.append((path, cast("Blob", repo[before[path]]).data))
            if result.conflicts:
                logger.warning(
                    "Git undo: refusing {}; file/directory conflict at {}",
                    sha,
                    ", ".join(result.conflicts),
                )
                return UndoResult(conflicts=result.conflicts)
            self._apply_writes(writes)
            result.restored = [path for path, _ in writes]
            if result.restored:
                result.new_sha = self.auto_commit(f"undo {full_sha[:8]}")
            return result
        except Exception as exc:
            raise GitStoreError(f"Git undo failed for {sha}") from exc

    def restore_preview(self, sha: str) -> UndoResult:
        """What :meth:`restore` would do, without changing anything."""
        if not self.is_initialized():
            return UndoResult()
        try:
            plan, _writes, _full = self._plan_restore(sha)
            return plan
        except Exception as exc:
            raise GitStoreError(f"Git restore preview failed for {sha}") from exc

    def restore(self, sha: str) -> UndoResult:
        """Set every path recorded at *sha* back to its content there, then commit.

        Current files are snapshotted into history first, so every overwrite
        is itself undoable. Tracked files absent at *sha* are kept, never
        deleted. Any conflict refuses the whole restore with nothing changed.
        """
        if not self.is_initialized():
            return UndoResult()
        try:
            plan, writes, full_sha = self._plan_restore(sha)
            if plan.conflicts or not writes:
                return plan
            self.auto_commit(f"snapshot: before restore {full_sha[:8]}")
            self._apply_writes(writes)
            plan.new_sha = self.auto_commit(f"restore {full_sha[:8]}")
            return plan
        except Exception as exc:
            raise GitStoreError(f"Git restore failed for {sha}") from exc

    def _plan_restore(self, sha: str) -> tuple[UndoResult, list[tuple[str, bytes]], str]:
        from dulwich.repo import Repo

        plan = UndoResult()
        writes: list[tuple[str, bytes]] = []
        with Repo(str(self._git_dir)) as repo:
            commit, full_sha = self._lookup_commit(repo, sha, None)
            if commit is None:
                return plan, writes, ""
            target = self._tree_blobs(repo, commit.tree)
            current = set(self._expand_tracked_files())
            for path in sorted(target.keys() | current):
                state = self._workspace_state(path)
                if path not in target:
                    if state is not None:
                        plan.kept.append(path)
                    continue
                data = cast("Blob", repo[target[path]]).data
                if state == data:
                    continue
                if state is _CONFLICT or state is _UNTRACKABLE:
                    plan.conflicts.append(path)
                    continue
                writes.append((path, data))
        if plan.conflicts:
            return UndoResult(conflicts=plan.conflicts), [], full_sha
        plan.restored = [path for path, _ in writes]
        return plan, writes, full_sha

    def _lookup_commit(
        self, repo: "Repo", sha: str, message_prefix: str | None
    ) -> tuple["Commit | None", str]:
        full = self._resolve_sha(sha)
        if not full:
            logger.warning("Git store: SHA not found: {}", sha)
            return None, ""
        obj = repo[full]
        if obj.type_name != b"commit":
            return None, ""
        commit = cast("Commit", obj)
        if message_prefix is not None and not commit.message.decode(
            "utf-8", errors="replace"
        ).strip().startswith(message_prefix):
            logger.warning("Git store: commit {} does not match {!r}", sha, message_prefix)
            return None, ""
        return commit, full.decode()

    def _workspace_state(self, rel: str) -> "bytes | None | object":
        """Current state of a tracked path, for comparison with history.

        ``None`` when absent, the bytes for a regular file history can hold,
        ``_CONFLICT`` when the path is a directory or a parent is a symlink or
        not a directory (never write through it: it could lead outside the
        workspace, e.g. into history/), and ``_UNTRACKABLE`` for a symlink,
        oversize or unreadable file (content history does not have).
        """
        parts = Path(rel).parts
        if not parts or Path(rel).is_absolute() or ".." in parts:
            return _CONFLICT
        node = self._workspace
        for part in parts[:-1]:
            node = node / part
            if node.is_symlink() or (node.exists() and not node.is_dir()):
                return _CONFLICT
        dest = self._workspace / rel
        if dest.is_symlink():
            return _UNTRACKABLE
        if not dest.exists():
            return None
        if dest.is_dir():
            return _CONFLICT
        try:
            if not dest.is_file() or dest.stat().st_size > _MAX_TRACKED_FILE_BYTES:
                return _UNTRACKABLE
            return dest.read_bytes()
        except OSError:
            return _UNTRACKABLE

    def _apply_writes(self, writes: list[tuple[str, bytes]]) -> None:
        """Write every file or none.

        Stage each file to a temp file (keeping the destination's mode, so an
        executable skill script stays executable) and hard-link a backup of
        any file it replaces; then rename the temp files in. Any failure rolls
        back the renames already done. Temp files and backups never outlive
        the call.
        """
        staged: list[list[Any]] = []  # [tmp, dest, backup | None]
        done: list[list[Any]] = []
        try:
            for rel, data in writes:
                dest = self._workspace / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                fd, tmp = tempfile.mkstemp(dir=str(dest.parent), prefix=f".{dest.name}.", suffix=".tmp")
                entry: list[Any] = [tmp, dest, None]
                staged.append(entry)
                with os.fdopen(fd, "wb") as handle:
                    handle.write(data)
                try:
                    mode = stat.S_IMODE(dest.stat().st_mode)
                except FileNotFoundError:
                    mode = 0o644
                os.chmod(tmp, mode)
                if dest.exists():
                    entry[2] = f"{tmp}.bak"
                    os.link(dest, entry[2])
            for entry in staged:
                os.replace(entry[0], entry[1])
                done.append(entry)
        except BaseException:
            for _tmp, dest, backup in reversed(done):
                with suppress(OSError):
                    if backup is not None:
                        os.replace(backup, dest)
                    else:
                        os.unlink(dest)
            raise
        finally:
            for tmp, _dest, backup in staged:
                for leftover in (tmp, backup):
                    if leftover is not None:
                        with suppress(OSError):
                            os.unlink(leftover)

    @classmethod
    def _changed_paths(cls, repo: "Repo", commit: "Commit") -> list[str]:
        """Paths the commit changed compared with its first parent."""
        after = cls._tree_blobs(repo, commit.tree)
        before = (
            cls._tree_blobs(repo, cast("Commit", repo[commit.parents[0]]).tree)
            if commit.parents
            else {}
        )
        return sorted(p for p in before.keys() | after.keys() if before.get(p) != after.get(p))

    @staticmethod
    def _tree_blobs(repo: "Repo", tree_id: bytes, prefix: str = "") -> dict[str, bytes]:
        """Map every blob path in a tree (recursively) to its blob id."""
        out: dict[str, bytes] = {}
        tree = cast("Tree", repo[tree_id])
        for entry in tree.iteritems():
            path = f"{prefix}{entry.path.decode('utf-8')}"
            obj = repo[entry.sha]
            if obj.type_name == b"tree":
                out.update(GitStore._tree_blobs(repo, entry.sha, f"{path}/"))
            elif obj.type_name == b"blob":
                out[path] = entry.sha
        return out

    @staticmethod
    def _read_blob_from_tree(
        repo: "Repo",
        tree: "Tree",
        filepath: str,
    ) -> bytes | None:
        """Read a blob's raw bytes from a tree object by walking path parts.

        Returns bytes (not str) so binary skill assets round-trip exactly
        through history (MIT-1842); text callers decode themselves.
        """
        parts = Path(filepath).parts
        current = tree
        for part in parts:
            try:
                entry = current[part.encode()]
            except KeyError:
                return None
            obj = repo[entry[1]]
            if obj.type_name == b"blob":
                blob = cast("Blob", obj)
                return blob.data
            if obj.type_name == b"tree":
                current = cast("Tree", obj)
            else:
                return None
        return None
