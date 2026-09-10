"""Private Markdown publication tool for WebSocket conversations.

The model supplies only a source path.  The current conversation is bound by
the agent loop through a context variable, so it cannot select a session (or
turn an arbitrary file id into a download grant).
"""

from __future__ import annotations

import contextvars
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.schema import StringSchema, tool_parameters_schema

if TYPE_CHECKING:
    from nanobot.session.manager import SessionManager


MAX_PUBLISHED_FILE_BYTES = 2 * 1024 * 1024


@dataclass
class PublishFileTurn:
    """Per-turn publication provenance, owned by the runtime not the model."""

    session_manager: SessionManager
    session_key: str
    enabled: bool = True
    publications: dict[str, str] = field(default_factory=dict)

    def remember(self, file_id: str, filename: str) -> None:
        self.publications.setdefault(file_id, filename)


_current_turn: contextvars.ContextVar[PublishFileTurn | None] = contextvars.ContextVar(
    "publish_file_turn", default=None
)


def bind_publish_file_turn(turn: PublishFileTurn):
    """Bind a publication turn for one agent-run task."""
    return _current_turn.set(turn)


def reset_publish_file_turn(token: contextvars.Token[PublishFileTurn | None]) -> None:
    _current_turn.reset(token)


def _markdown_label(filename: str) -> str:
    """Escape a filename used as Markdown link text."""
    clean = "".join(ch for ch in filename if ord(ch) >= 32 and ch != "\x7f")
    return clean.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")


def _read_workspace_markdown(workspace: Path, user_path: str) -> tuple[str, bytes]:
    """Read one regular Markdown file through descriptor-relative traversal.

    ``Path.resolve`` is intentionally absent: each directory component and
    the leaf are opened with ``O_NOFOLLOW`` from the approved workspace fd.
    That prevents a check/use race from redirecting publication through a
    symlink after validation.
    """
    if not isinstance(user_path, str) or not user_path or "\x00" in user_path:
        raise ValueError("path is invalid")
    supplied = Path(user_path)
    if any(part == ".." for part in supplied.parts):
        raise ValueError("path traversal is not allowed")
    root = workspace.resolve(strict=True)
    try:
        relative = supplied.relative_to(root) if supplied.is_absolute() else supplied
    except ValueError as exc:
        raise ValueError("path is outside the workspace") from exc
    parts = relative.parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("path is invalid")
    filename = parts[-1]
    if not filename.endswith(".md"):
        raise ValueError("only .md files can be published")

    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    # O_NONBLOCK prevents opening a FIFO from stalling before fstat can reject
    # it. It has no effect on normal regular-file reads.
    file_flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
    root_fd = os.open(root, directory_flags)
    fd = root_fd
    try:
        for component in parts[:-1]:
            next_fd = os.open(component, directory_flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        leaf_fd = os.open(filename, file_flags, dir_fd=fd)
        try:
            info = os.fstat(leaf_fd)
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("only regular files can be published")
            if info.st_size > MAX_PUBLISHED_FILE_BYTES:
                raise ValueError("file exceeds the 2 MiB publication limit")
            chunks: list[bytes] = []
            remaining = MAX_PUBLISHED_FILE_BYTES + 1
            while remaining:
                chunk = os.read(leaf_fd, min(64 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            payload = b"".join(chunks)
            if len(payload) > MAX_PUBLISHED_FILE_BYTES:
                raise ValueError("file exceeds the 2 MiB publication limit")
            return filename, payload
        finally:
            os.close(leaf_fd)
    except OSError as exc:
        raise ValueError("file could not be safely opened") from exc
    finally:
        os.close(fd)


@tool_parameters(
    tool_parameters_schema(
        path=StringSchema("Markdown file path inside the current workspace"),
        required=["path"],
    )
)
class PublishFileTool(Tool):
    """Snapshot a workspace Markdown file for the current private session."""

    def __init__(self, workspace: Path):
        self._workspace = workspace

    @property
    def name(self) -> str:
        return "publish_file"

    @property
    def description(self) -> str:
        return (
            "Whenever you refer a reader to a generated .md report or file, use "
            "this tool to publish it from the current workspace as a private "
            "download. Include the returned Markdown link unchanged in your final "
            "answer; never substitute a raw filesystem path."
        )

    async def execute(self, path: str, **kwargs: Any) -> str:
        turn = _current_turn.get()
        if turn is None or not turn.enabled:
            return "Error: File publication is unavailable in this conversation."
        try:
            filename, payload = _read_workspace_markdown(self._workspace, path)
            file_id = turn.session_manager.store_published_snapshot(filename, payload)
        except ValueError as exc:
            return f"Error: Cannot publish file: {exc}"
        except OSError:
            return "Error: Cannot publish file safely."
        turn.remember(file_id, filename)
        url = turn.session_manager.published_file_url(turn.session_key, file_id)
        return (
            f"Published [{_markdown_label(filename)}]({url}). "
            "Preserve this exact link in your final answer so the user can download it."
        )
