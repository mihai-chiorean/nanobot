"""Git-backed version control for memory files, using dulwich."""

from __future__ import annotations

import io
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

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
# way; anything this big in the instruction set is an accident).
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


class GitStoreError(RuntimeError):
    """Raised when the memory Git repository cannot complete an operation."""


@dataclass
class CommitInfo:
    sha: str  # Short SHA (8 chars)
    message: str
    timestamp: str  # Formatted datetime

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


class GitStore:
    """Git-backed version control for memory files.

    Two layouts:

    * legacy (``git_dir=None``): a repo at ``<workspace>/.git`` using the
      dulwich index and working tree. Kept for callers that want the old
      behaviour.
    * bare (``git_dir`` set, SM-01/MIT-1842): a bare repo outside the
      workspace (``history_dir_for(workspace) / "workspace.git"``), so the
      agent's file tools cannot rewrite its own undo history. Commits read
      the tracked files as bytes and write blobs + a tree straight into the
      object store — no index, no checkout, no ``<workspace>/.git`` and no
      ``.gitignore``.
    """

    def __init__(self, workspace: Path, tracked_files: list[str], git_dir: Path | None = None):
        self._workspace = workspace
        self._tracked_files = list(tracked_files)
        self._git_dir = Path(git_dir) if git_dir is not None else None
        # Rate-limit the "tracked file too large" warning: one line per path.
        self._oversize_logged: set[str] = set()

    @property
    def _repo_path(self) -> Path:
        """Where dulwich opens the repo: git_dir in bare mode, workspace otherwise."""
        return self._git_dir if self._git_dir is not None else self._workspace

    def is_initialized(self) -> bool:
        """Check if the git repo has been initialized."""
        if self._git_dir is not None:
            return self._git_dir.is_dir()
        return (self._workspace / ".git").is_dir()

    # -- init ------------------------------------------------------------------

    def init(self) -> bool:
        """Initialize a git repo if not already initialized.

        Legacy layout: creates .gitignore and makes an initial commit.
        Bare layout (MIT-1842): initializes the bare repo at git_dir, first
        migrating any legacy ``<workspace>/.git`` out of the workspace to
        ``<history>/legacy-dot-git``. Never touches the workspace itself.
        Returns True if a new repo was created, False if already exists.
        """
        if self.is_initialized():
            return False

        if self._git_dir is not None:
            return self._init_bare()

        if self._is_inside_git_repo():
            logger.warning(
                "Workspace {} is already inside a git repo; "
                "skipping nested repo initialization",
                self._workspace,
            )
            return False

        try:
            from dulwich import porcelain

            porcelain.init(str(self._workspace))

            # Write .gitignore (merge with existing if present)
            gitignore = self._workspace / ".gitignore"
            dream_entries = self._build_gitignore()
            if gitignore.exists():
                existing = gitignore.read_text(encoding="utf-8")
                existing_lines = set(existing.splitlines())
                new_lines = [
                    line
                    for line in dream_entries.splitlines()
                    if line not in existing_lines
                ]
                if new_lines:
                    merged = existing.rstrip("\n") + "\n" + "\n".join(new_lines) + "\n"
                    gitignore.write_text(merged, encoding="utf-8")
            else:
                gitignore.write_text(dream_entries, encoding="utf-8")

            # Ensure tracked files exist (touch them if missing) so the initial
            # commit has something to track.
            for rel in self._tracked_files:
                p = self._workspace / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                if not p.exists():
                    p.write_text("", encoding="utf-8")

            # Initial commit
            porcelain.add(
                str(self._workspace),
                paths=self._staging_paths(".gitignore", *self._tracked_files),
            )
            porcelain.commit(
                str(self._workspace),
                message=b"init: nanobot memory store",
                author=b"nanobot <nanobot@dream>",
                committer=b"nanobot <nanobot@dream>",
            )
            logger.info("Git store initialized at {}", self._workspace)
            return True
        except Exception as exc:
            raise GitStoreError(f"Git store init failed for {self._workspace}") from exc

    def _init_bare(self) -> bool:
        """Initialize the bare repo at git_dir (MIT-1842).

        Migrates a legacy ``<workspace>/.git`` (the old in-workspace store the
        agent could reach) to ``<history>/legacy-dot-git`` so the old Dream
        history stays readable with the host's ``git``. The workspace
        ``.gitignore`` the old store wrote is harmless and left alone. Then
        makes an initial commit of the tracked patterns as they are now.
        """
        assert self._git_dir is not None
        try:
            from dulwich.repo import Repo

            history = self._git_dir.parent
            history.mkdir(parents=True, exist_ok=True)
            os.chmod(history, 0o700)

            legacy = self._workspace / ".git"
            if legacy.exists():
                os.replace(legacy, history / "legacy-dot-git")
                logger.info(
                    "Migrated legacy workspace git repo {} to {}",
                    legacy,
                    history / "legacy-dot-git",
                )

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
                    if rel in self._oversize_logged:
                        continue
                    size = candidate.stat().st_size
                    if size > _MAX_TRACKED_FILE_BYTES:
                        self._oversize_logged.add(rel)
                        logger.warning(
                            "Git store: skipping tracked file {} ({} bytes > {} limit)",
                            rel,
                            size,
                            _MAX_TRACKED_FILE_BYTES,
                        )
                        continue
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
        assert self._git_dir is not None
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
        """Stage tracked memory files and commit if there are changes.

        Returns the short commit SHA, or None if nothing to commit.
        """
        if not self.is_initialized():
            return None

        if self._git_dir is not None:
            try:
                sha = self._bare_commit(message)
                if sha is None:
                    return None
                # worktree.commit returns the full id as ASCII bytes; decode
                # before slicing or the short sha is a bytes repr no git
                # command can resolve (same trap as the legacy path below).
                short = sha.decode()[:8]
                logger.debug("Git auto-commit: {} ({})", short, message)
                return short
            except Exception as exc:
                raise GitStoreError(f"Git auto-commit failed: {message}") from exc

        try:
            from dulwich import porcelain

            # Stage first so Dulwich refreshes the content hashes.  A status
            # check can miss rapid same-size rewrites when the filesystem also
            # preserves the file's mtime.
            porcelain.add(str(self._workspace), paths=self._staging_paths(*self._tracked_files))
            st = porcelain.status(str(self._workspace))
            staged = cast(dict[object, list[object]], st.staged)
            if not any(staged.values()):
                return None

            message_value = cast(object, message)
            msg_bytes = (
                message_value.encode("utf-8")
                if isinstance(message_value, str)
                else cast(bytes, message_value)
            )
            sha_bytes = porcelain.commit(
                str(self._workspace),
                message=msg_bytes,
                author=b"nanobot <nanobot@dream>",
                committer=b"nanobot <nanobot@dream>",
            )
            if cast(object, sha_bytes) is None:
                return None
            # porcelain.commit returns the id as a 40-char hex string that is
            # already encoded to bytes; .hex() would encode those ASCII bytes
            # again and produce an id no git command can resolve.
            sha = sha_bytes.decode()[:8]
            logger.debug("Git auto-commit: {} ({})", sha, message)
            return sha
        except Exception as exc:
            raise GitStoreError(f"Git auto-commit failed: {message}") from exc

    # -- internal helpers ------------------------------------------------------

    def _staging_paths(self, *paths: str) -> list[str]:
        """Return absolute paths without resolving tracked-file symlinks."""
        return [str((self._workspace / path).absolute()) for path in paths]

    def _resolve_sha(self, short_sha: str) -> bytes | None:
        """Resolve a short SHA prefix to the full SHA bytes."""
        try:
            from dulwich.repo import Repo

            with Repo(str(self._repo_path)) as repo:
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

    def _is_inside_git_repo(self) -> bool:
        """Check if self._workspace is already inside a git repository.

        Walks up from self._workspace to the filesystem root, returning True
        if any parent directory contains a .git entry.

        Git worktrees and submodules can use a ``.git`` file instead of a
        directory, so we must treat either form as "already inside a repo".
        """
        current = self._workspace.resolve()
        while current != current.parent:
            if (current / ".git").exists():
                return True
            current = current.parent
        return False

    def _build_gitignore(self) -> str:
        """Generate .gitignore content from tracked files."""
        dirs: set[str] = set()
        for f in self._tracked_files:
            parent = str(Path(f).parent)
            if parent != ".":
                dirs.add(parent)
        lines = ["/*"]
        for d in sorted(dirs):
            lines.append(f"!{d}/")
        for f in self._tracked_files:
            lines.append(f"!{f}")
        lines.append("!.gitignore")
        return "\n".join(lines) + "\n"

    # -- query -----------------------------------------------------------------

    def log(
        self,
        max_entries: int = 20,
        message_prefix: str | None = None,
    ) -> list[CommitInfo]:
        """Return simplified commit log, optionally filtered by message prefix.

        When filtering, *max_entries* counts matching commits rather than every
        commit traversed in the repository.
        """
        if not self.is_initialized():
            return []

        try:
            from dulwich.repo import Repo

            entries: list[CommitInfo] = []
            with Repo(str(self._repo_path)) as repo:
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
                str(self._repo_path),
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
            with Repo(str(self._repo_path)) as repo:
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
                    with Repo(str(self._repo_path)) as repo:
                        commit = cast("Commit", repo[full_sha])
                        parent = commit.parents[0] if commit.parents else None
                    diff = self.diff_commits(parent.decode()[:8], c.sha) if parent else ""
                    return c, diff
            return None
        except Exception as exc:
            raise GitStoreError(f"Git commit display failed for {short_sha}") from exc

    # -- restore ---------------------------------------------------------------

    def revert(self, commit: str, *, message_prefix: str | None = None) -> str | None:
        """Revert (undo) the changes introduced by the given commit.

        Restores all tracked memory files to the state at the commit's parent,
        then creates a new commit recording the revert. When *message_prefix*
        is provided, commits outside that history are rejected before any files
        are changed.

        Returns the new commit SHA, or ``None`` when the commit cannot be reverted.
        Repository and filesystem failures raise :class:`GitStoreError`.
        """
        if not self.is_initialized():
            return None

        try:
            from dulwich.repo import Repo

            full_sha = self._resolve_sha(commit)
            if not full_sha:
                logger.warning("Git revert: SHA not found: {}", commit)
                return None

            with Repo(str(self._repo_path)) as repo:
                commit_obj = repo[full_sha]
                if commit_obj.type_name != b"commit":
                    return None
                typed_commit = cast("Commit", commit_obj)

                commit_message = typed_commit.message.decode(
                    "utf-8",
                    errors="replace",
                ).strip()
                if message_prefix is not None and not commit_message.startswith(message_prefix):
                    logger.warning(
                        "Git revert: commit {} does not match message prefix {!r}",
                        commit,
                        message_prefix,
                    )
                    return None

                if not typed_commit.parents:
                    logger.warning("Git revert: cannot revert root commit {}", commit)
                    return None

                # Use the parent's tree — this undoes the commit's changes
                parent_obj = cast("Commit", repo[typed_commit.parents[0]])
                tree = cast("Tree", repo[parent_obj.tree])

                restored: list[str] = []
                for filepath in self._tracked_files:
                    content = self._read_blob_from_tree(repo, tree, filepath)
                    if content is not None:
                        dest = self._workspace / filepath
                        dest.write_bytes(content)
                        restored.append(filepath)

            if not restored:
                return None

            # Commit the restored state
            msg = f"revert: undo {commit}"
            return self.auto_commit(msg)
        except Exception as exc:
            raise GitStoreError(f"Git revert failed for {commit}") from exc

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
