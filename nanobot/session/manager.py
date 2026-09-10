"""Session management for conversation history."""

import hashlib
import json
import os
import re
import secrets
import shutil
import stat
from contextlib import suppress
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from loguru import logger

from nanobot.config.paths import get_legacy_sessions_dir, is_default_workspace
from nanobot.utils.helpers import (
    ensure_dir,
    estimate_message_tokens,
    find_legal_message_start,
    image_placeholder_text,
    safe_filename,
)

FILE_MAX_MESSAGES = 2000
SESSION_PREVIEW_MAX_BYTES = 256 * 1024
SESSION_PREVIEW_MAX_LINES = 128
SESSION_PREVIEW_MAX_CHARS = 160
SESSION_SEARCH_MAX_RESULTS = 50
SESSION_SEARCH_MATCHES_PER_SESSION = 5
SESSION_SEARCH_SNIPPET_CHARS = 240
_PUBLISHED_FILE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_PUBLISHED_GRANTS_KEY = "published_file_grants"
_PUBLISHED_PROVENANCE_KEY = "published_file_provenance"
_PUBLISHED_MESSAGE_ID_KEY = "_published_message_id"


@dataclass
class Session:
    """A conversation session."""

    key: str  # channel:chat_id
    messages: list[dict[str, Any]] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    metadata: dict[str, Any] = field(default_factory=dict)
    last_consolidated: int = 0  # Number of messages already consolidated to files

    @staticmethod
    def _annotate_message_time(message: dict[str, Any], content: Any) -> Any:
        """Expose persisted turn timestamps to the model for relative-date reasoning.

        Annotating *every* assistant turn trains the model (via in-context
        demonstrations) to start its own replies with the same
        ``[Message Time: ...]`` prefix, which leaks metadata back to the user.
        We therefore only annotate:

        * ``user`` turns — needed so the model can pin the conversation in time.
        * proactive deliveries (``_channel_delivery=True``) — cron / heartbeat
          assistant pushes that may sit hours away from the next user reply,
          and are too infrequent to act as parroting demonstrations.
        """
        timestamp = message.get("timestamp")
        if not timestamp or not isinstance(content, str):
            return content
        role = message.get("role")
        if role == "user":
            pass
        elif role == "assistant" and message.get("_channel_delivery"):
            pass
        else:
            return content
        return f"[Message Time: {timestamp}]\n{content}"

    def add_message(self, role: str, content: str, **kwargs: Any) -> None:
        """Add a message to the session."""
        msg = {
            "role": role,
            "content": content,
            "timestamp": datetime.now().isoformat(),
            **kwargs
        }
        self.messages.append(msg)
        self.updated_at = datetime.now()

    def get_history(
        self,
        max_messages: int = 120,
        *,
        max_tokens: int = 0,
        include_timestamps: bool = False,
    ) -> list[dict[str, Any]]:
        """Return unconsolidated messages for LLM input.

        History is sliced by message count first (``max_messages``), then by
        token budget from the tail (``max_tokens``) when provided.
        """
        unconsolidated = self.messages[self.last_consolidated:]
        max_messages = max_messages if max_messages > 0 else 120
        sliced = unconsolidated[-max_messages:]

        # Avoid starting mid-turn when possible, except for proactive
        # assistant deliveries that the user may be replying to.
        for i, message in enumerate(sliced):
            if message.get("role") == "user":
                start = i
                if i > 0 and sliced[i - 1].get("_channel_delivery"):
                    start = i - 1
                sliced = sliced[start:]
                break

        # Drop orphan tool results at the front.
        start = find_legal_message_start(sliced)
        if start:
            sliced = sliced[start:]

        out: list[dict[str, Any]] = []
        for message in sliced:
            content = message.get("content", "")
            participant = message.get("participant_display_name")
            if (
                message.get("role") == "user"
                and isinstance(content, str)
                and isinstance(participant, str)
                and participant.strip()
            ):
                content = f"{participant.strip()}: {content}"
            # Synthesize an ``[image: path]`` breadcrumb from the persisted
            # ``media`` kwarg so LLM replay still sees *something* where the
            # image used to be. Without this, an image-only user turn
            # replays as an empty user message — the assistant's reply then
            # looks like it's responding to nothing.
            media = message.get("media")
            if isinstance(media, list) and media and isinstance(content, str):
                breadcrumbs = "\n".join(
                    image_placeholder_text(p) for p in media if isinstance(p, str) and p
                )
                content = f"{content}\n{breadcrumbs}" if content else breadcrumbs
            if include_timestamps:
                content = self._annotate_message_time(message, content)
            entry: dict[str, Any] = {"role": message["role"], "content": content}
            for key in ("tool_calls", "tool_call_id", "name", "reasoning_content", "thinking_blocks"):
                if key in message:
                    entry[key] = message[key]
            out.append(entry)

        if max_tokens > 0 and out:
            kept: list[dict[str, Any]] = []
            used = 0
            for message in reversed(out):
                tokens = estimate_message_tokens(message)
                if kept and used + tokens > max_tokens:
                    break
                kept.append(message)
                used += tokens
            kept.reverse()

            # Keep history aligned to the first visible user turn.
            first_user = next((i for i, m in enumerate(kept) if m.get("role") == "user"), None)
            if first_user is not None:
                kept = kept[first_user:]
            else:
                # Tight token budgets can otherwise leave assistant-only tails.
                # If a user turn exists in the unsliced output, recover the
                # nearest one even if it slightly exceeds the token budget.
                recovered_user = next(
                    (i for i in range(len(out) - 1, -1, -1) if out[i].get("role") == "user"),
                    None,
                )
                if recovered_user is not None:
                    kept = out[recovered_user:]

            # And keep a legal tool-call boundary at the front.
            start = find_legal_message_start(kept)
            if start:
                kept = kept[start:]
            out = kept
        return out

    def clear(self) -> None:
        """Clear all messages and reset session to initial state."""
        self.messages = []
        self.last_consolidated = 0
        self.updated_at = datetime.now()

    def retain_recent_legal_suffix(self, max_messages: int) -> None:
        """Keep a legal recent suffix constrained by a hard message cap."""
        if max_messages <= 0:
            self.clear()
            return
        if len(self.messages) <= max_messages:
            return

        retained = list(self.messages[-max_messages:])

        # Prefer starting at a user turn when one exists within the tail.
        first_user = next((i for i, m in enumerate(retained) if m.get("role") == "user"), None)
        if first_user is not None:
            retained = retained[first_user:]
        else:
            # If the tail is assistant/tool-only, anchor to the latest user in
            # the full session and take a capped forward window from there.
            latest_user = next(
                (i for i in range(len(self.messages) - 1, -1, -1)
                 if self.messages[i].get("role") == "user"),
                None,
            )
            if latest_user is not None:
                retained = list(self.messages[latest_user: latest_user + max_messages])

        # Mirror get_history(): avoid persisting orphan tool results at the front.
        start = find_legal_message_start(retained)
        if start:
            retained = retained[start:]

        # Hard-cap guarantee: never keep more than max_messages.
        if len(retained) > max_messages:
            retained = retained[-max_messages:]
            start = find_legal_message_start(retained)
            if start:
                retained = retained[start:]

        dropped = len(self.messages) - len(retained)
        self.messages = retained
        self.last_consolidated = max(0, self.last_consolidated - dropped)
        self.updated_at = datetime.now()

    def enforce_file_cap(
        self,
        on_archive: Any = None,
        limit: int = FILE_MAX_MESSAGES,
    ) -> None:
        """Bound session message growth by archiving and trimming old prefixes."""
        if limit <= 0 or len(self.messages) <= limit:
            return

        before = list(self.messages)
        before_last_consolidated = self.last_consolidated
        before_count = len(before)
        self.retain_recent_legal_suffix(limit)
        dropped_count = before_count - len(self.messages)
        if dropped_count <= 0:
            return

        dropped = before[:dropped_count]
        already_consolidated = min(before_last_consolidated, dropped_count)
        archive_chunk = dropped[already_consolidated:]
        if archive_chunk and on_archive:
            on_archive(archive_chunk)
        logger.info(
            "Session file cap hit for {}: dropped {}, raw-archived {}, kept {}",
            self.key,
            dropped_count,
            len(archive_chunk),
            len(self.messages),
        )


class SessionManager:
    """
    Manages conversation sessions.

    Sessions are stored as JSONL files in the sessions directory.
    """

    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.sessions_dir = ensure_dir(self.workspace / "sessions")
        # Snapshots deliberately do not live below the agent workspace.  The
        # workspace is where an agent writes reports; this runtime-owned,
        # stable location holds immutable bytes that have already been
        # published.  Its deterministic name lets a new process serve grants
        # persisted in JSONL after restart.
        store_key = hashlib.sha256(str(self.workspace.resolve()).encode("utf-8")).hexdigest()
        self.published_files_dir = ensure_dir(
            self.workspace.parent / ".nanobot-published-files" / store_key
        )
        with suppress(OSError):
            os.chmod(self.published_files_dir, 0o700)
        self.legacy_sessions_dir = (
            get_legacy_sessions_dir() if is_default_workspace(self.workspace) else None
        )
        self._cache: dict[str, Session] = {}

    @staticmethod
    def published_file_url(session_key: str, file_id: str) -> str:
        """Return the one canonical relative route emitted by ``publish_file``."""
        if _PUBLISHED_FILE_ID_RE.fullmatch(file_id) is None:
            raise ValueError("invalid published file id")
        return f"/api/sessions/{quote(session_key, safe='')}/files/{file_id}"

    @staticmethod
    def _has_published_markdown_link(content: str, url: str) -> bool:
        """Recognize the exact Markdown-link URL shape emitted by the tool."""
        return f"]({url})" in content

    def store_published_snapshot(self, filename: str, payload: bytes) -> str:
        """Atomically persist immutable publication bytes in the server store."""
        if not isinstance(payload, bytes) or len(payload) > 2 * 1024 * 1024:
            raise ValueError("invalid published file payload")
        if not isinstance(filename, str) or not filename.endswith(".md"):
            raise ValueError("invalid published filename")
        directory_fd = os.open(
            self.published_files_dir,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
        try:
            for _ in range(8):
                file_id = secrets.token_hex(16)
                temp_name = f".{file_id}.tmp"
                try:
                    fd = os.open(
                        temp_name,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                        0o600,
                        dir_fd=directory_fd,
                    )
                except FileExistsError:
                    continue
                try:
                    with os.fdopen(fd, "wb") as handle:
                        handle.write(payload)
                        handle.flush()
                        os.fsync(handle.fileno())
                    # link(2), unlike replace, never overwrites an existing
                    # id. Both names are resolved from the trusted directory
                    # descriptor, so a path race cannot redirect storage.
                    try:
                        os.link(
                            temp_name,
                            file_id,
                            src_dir_fd=directory_fd,
                            dst_dir_fd=directory_fd,
                            follow_symlinks=False,
                        )
                    except FileExistsError:
                        continue
                    os.unlink(temp_name, dir_fd=directory_fd)
                    os.fsync(directory_fd)
                    return file_id
                finally:
                    with suppress(FileNotFoundError):
                        os.unlink(temp_name, dir_fd=directory_fd)
        finally:
            os.close(directory_fd)
        raise OSError("could not allocate a publication id")

    def _snapshot_bytes(self, file_id: str) -> bytes | None:
        if _PUBLISHED_FILE_ID_RE.fullmatch(file_id) is None:
            return None
        directory_fd = -1
        try:
            directory_fd = os.open(
                self.published_files_dir,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
            )
            fd = os.open(
                file_id,
                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=directory_fd,
            )
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_size > 2 * 1024 * 1024:
                    return None
                chunks: list[bytes] = []
                remaining = 2 * 1024 * 1024 + 1
                while remaining:
                    chunk = os.read(fd, min(64 * 1024, remaining))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                data = b"".join(chunks)
                return data if len(data) <= 2 * 1024 * 1024 else None
            finally:
                os.close(fd)
        except OSError:
            return None
        finally:
            if directory_fd >= 0:
                os.close(directory_fd)

    def read_published_file(self, session_key: str, file_id: str) -> tuple[str, bytes] | None:
        """Return a granted snapshot only; ids are never global capabilities."""
        if _PUBLISHED_FILE_ID_RE.fullmatch(file_id) is None:
            return None
        payload = self.read_session_file(session_key)
        if not isinstance(payload, dict) or payload.get("key") != session_key:
            return None
        metadata = payload.get("metadata") if isinstance(payload, dict) else None
        grants = metadata.get(_PUBLISHED_GRANTS_KEY) if isinstance(metadata, dict) else None
        grant = grants.get(file_id) if isinstance(grants, dict) else None
        filename = grant.get("filename") if isinstance(grant, dict) else None
        if not isinstance(filename, str) or not filename.endswith(".md"):
            return None
        data = self._snapshot_bytes(file_id)
        return (filename, data) if data is not None else None

    def grant_published_files(
        self,
        session: Session,
        publications: dict[str, str],
        *,
        message_start: int,
    ) -> None:
        """Grant only tool publications rendered in a final visible answer.

        The full assistant-message digest is durable provenance.  Room cloning
        later requires that digest as well as the exact canonical URL, so a
        pasted or invented id cannot become a room grant.
        """
        if not publications:
            return
        grants = session.metadata.setdefault(_PUBLISHED_GRANTS_KEY, {})
        provenance = session.metadata.setdefault(_PUBLISHED_PROVENANCE_KEY, {})
        if not isinstance(grants, dict) or not isinstance(provenance, dict):
            return
        for message in session.messages[message_start:]:
            if message.get("role") != "assistant" or message.get("tool_calls"):
                continue
            content = message.get("content")
            if not isinstance(content, str):
                continue
            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
            timestamp = message.get("timestamp")
            for file_id, filename in publications.items():
                if (
                    _PUBLISHED_FILE_ID_RE.fullmatch(file_id) is None
                    or not isinstance(filename, str)
                    or not filename.endswith(".md")
                    or not self._has_published_markdown_link(
                        content, self.published_file_url(session.key, file_id)
                    )
                    or self._snapshot_bytes(file_id) is None
                ):
                    continue
                message_id = message.get(_PUBLISHED_MESSAGE_ID_KEY)
                if not isinstance(message_id, str) or _PUBLISHED_FILE_ID_RE.fullmatch(message_id) is None:
                    message_id = secrets.token_hex(16)
                    message[_PUBLISHED_MESSAGE_ID_KEY] = message_id
                grants[file_id] = {"filename": filename}
                records = provenance.setdefault(file_id, [])
                if not isinstance(records, list):
                    continue
                record = {
                    "message_sha256": digest,
                    "timestamp": timestamp,
                    "message_id": message_id,
                }
                if record not in records:
                    records.append(record)

    @staticmethod
    def safe_key(key: str) -> str:
        """Public helper used by HTTP handlers to map an arbitrary key to a stable filename stem."""
        return safe_filename(key.replace(":", "_"))

    def _get_session_path(self, key: str) -> Path:
        """Get the file path for a session."""
        return self.sessions_dir / f"{self.safe_key(key)}.jsonl"

    def _get_legacy_session_path(self, key: str) -> Path:
        """Legacy global session path (~/.nanobot/sessions/)."""
        if self.legacy_sessions_dir is None:
            raise RuntimeError("legacy session migration is disabled for custom workspaces")
        return self.legacy_sessions_dir / f"{self.safe_key(key)}.jsonl"

    def _migrate_legacy_sessions(self) -> None:
        """Move discoverable owner sessions into the workspace before listing."""
        legacy_dir = self.legacy_sessions_dir
        if legacy_dir is None or not legacy_dir.is_dir():
            return
        for legacy_path in legacy_dir.glob("*.jsonl"):
            if legacy_path.is_symlink() or not legacy_path.is_file():
                continue
            destination = self.sessions_dir / legacy_path.name
            if destination.exists():
                continue
            try:
                shutil.move(str(legacy_path), str(destination))
                logger.info("Migrated legacy session {} before listing", legacy_path.stem)
            except Exception:
                logger.exception("Failed to migrate legacy session {}", legacy_path.stem)

    def get_or_create(self, key: str) -> Session:
        """
        Get an existing session or create a new one.

        Args:
            key: Session key (usually channel:chat_id).

        Returns:
            The session.
        """
        if key in self._cache:
            return self._cache[key]

        session = self._load(key)
        if session is None:
            session = Session(key=key)

        self._cache[key] = session
        return session

    def _load(self, key: str) -> Session | None:
        """Load a session from disk."""
        path = self._get_session_path(key)
        if not path.exists() and self.legacy_sessions_dir is not None:
            legacy_path = self._get_legacy_session_path(key)
            if legacy_path.exists():
                try:
                    shutil.move(str(legacy_path), str(path))
                    logger.info("Migrated session {} from legacy path", key)
                except Exception:
                    logger.exception("Failed to migrate session {}", key)

        if not path.exists():
            return None

        try:
            messages = []
            metadata = {}
            created_at = None
            updated_at = None
            last_consolidated = 0

            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue

                    data = json.loads(line)

                    if data.get("_type") == "metadata":
                        metadata = data.get("metadata", {})
                        created_at = datetime.fromisoformat(data["created_at"]) if data.get("created_at") else None
                        updated_at = datetime.fromisoformat(data["updated_at"]) if data.get("updated_at") else None
                        last_consolidated = data.get("last_consolidated", 0)
                    else:
                        messages.append(data)

            return Session(
                key=key,
                messages=messages,
                created_at=created_at or datetime.now(),
                updated_at=updated_at or datetime.now(),
                metadata=metadata,
                last_consolidated=last_consolidated
            )
        except Exception as e:
            logger.warning("Failed to load session {}: {}", key, e)
            repaired = self._repair(key)
            if repaired is not None:
                logger.info("Recovered session {} from corrupt file ({} messages)", key, len(repaired.messages))
            return repaired

    def _repair(self, key: str) -> Session | None:
        """Attempt to recover a session from a corrupt JSONL file."""
        path = self._get_session_path(key)
        if not path.exists():
            return None

        try:
            messages: list[dict[str, Any]] = []
            metadata: dict[str, Any] = {}
            created_at: datetime | None = None
            updated_at: datetime | None = None
            last_consolidated = 0
            skipped = 0

            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        skipped += 1
                        continue

                    if data.get("_type") == "metadata":
                        metadata = data.get("metadata", {})
                        if data.get("created_at"):
                            with suppress(ValueError, TypeError):
                                created_at = datetime.fromisoformat(data["created_at"])
                        if data.get("updated_at"):
                            with suppress(ValueError, TypeError):
                                updated_at = datetime.fromisoformat(data["updated_at"])
                        last_consolidated = data.get("last_consolidated", 0)
                    else:
                        messages.append(data)

            if skipped:
                logger.warning("Skipped {} corrupt lines in session {}", skipped, key)

            if not messages and not metadata:
                return None

            return Session(
                key=key,
                messages=messages,
                created_at=created_at or datetime.now(),
                updated_at=updated_at or datetime.now(),
                metadata=metadata,
                last_consolidated=last_consolidated
            )
        except Exception as e:
            logger.warning("Repair failed for session {}: {}", key, e)
            return None

    @staticmethod
    def _session_payload(session: Session) -> dict[str, Any]:
        return {
            "key": session.key,
            "created_at": session.created_at.isoformat(),
            "updated_at": session.updated_at.isoformat(),
            "metadata": session.metadata,
            "messages": session.messages,
        }

    def save(self, session: Session, *, fsync: bool = False) -> None:
        """Save a session to disk atomically.

        When *fsync* is ``True`` the final file and its parent directory are
        explicitly flushed to durable storage.  This is intentionally off by
        default (the OS page-cache is sufficient for normal operation) but
        should be enabled during graceful shutdown so that filesystems with
        write-back caching (e.g. rclone VFS, NFS, FUSE mounts) do not lose
        the most recent writes.
        """
        path = self._get_session_path(session.key)
        tmp_path = path.with_suffix(".jsonl.tmp")

        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                metadata_line = {
                    "_type": "metadata",
                    "key": session.key,
                    "created_at": session.created_at.isoformat(),
                    "updated_at": session.updated_at.isoformat(),
                    "metadata": session.metadata,
                    "last_consolidated": session.last_consolidated
                }
                f.write(json.dumps(metadata_line, ensure_ascii=False) + "\n")
                for msg in session.messages:
                    f.write(json.dumps(msg, ensure_ascii=False) + "\n")
                if fsync:
                    f.flush()
                    os.fsync(f.fileno())

            os.replace(tmp_path, path)

            if fsync:
                # fsync the directory so the rename is durable.
                # On Windows, opening a directory with O_RDONLY raises
                # PermissionError — skip the dir sync there (NTFS
                # journals metadata synchronously).
                with suppress(PermissionError):
                    fd = os.open(str(path.parent), os.O_RDONLY)
                    try:
                        os.fsync(fd)
                    finally:
                        os.close(fd)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise

        self._cache[session.key] = session

    def flush_all(self) -> int:
        """Re-save every cached session with fsync for durable shutdown.

        Returns the number of sessions flushed.  Errors on individual
        sessions are logged but do not prevent other sessions from being
        flushed.
        """
        flushed = 0
        for key, session in list(self._cache.items()):
            try:
                self.save(session, fsync=True)
                flushed += 1
            except Exception:
                logger.warning("Failed to flush session {}", key, exc_info=True)
        return flushed

    def invalidate(self, key: str) -> None:
        """Remove a session from the in-memory cache."""
        self._cache.pop(key, None)

    def delete_session(self, key: str) -> bool:
        """Remove a session from disk and the in-memory cache.

        Returns True if a JSONL file was found and unlinked.
        """
        path = self._get_session_path(key)
        self.invalidate(key)
        if not path.exists():
            return False
        try:
            path.unlink()
            return True
        except OSError as e:
            logger.warning("Failed to delete session file {}: {}", path, e)
            return False

    def clone_session(
        self,
        source_key: str,
        destination_key: str,
        *,
        metadata: dict[str, Any] | None = None,
        shared_room_owner: str | None = None,
        snapshot_message_count: int | None = None,
        snapshot_sha256: str | None = None,
    ) -> Session:
        """Create a durable conversation branch without sharing future source turns."""
        if self._get_session_path(destination_key).exists():
            raise FileExistsError(destination_key)
        source = self.get_or_create(source_key)
        if not self._get_session_path(source_key).exists():
            raise FileNotFoundError(source_key)
        now = datetime.now()
        messages = deepcopy(source.messages)
        if snapshot_message_count is not None:
            if type(snapshot_message_count) is not int or not 0 <= snapshot_message_count <= len(messages):
                raise ValueError("Invalid snapshot boundary")
            messages = messages[:snapshot_message_count]
            digest = hashlib.sha256(json.dumps(messages, sort_keys=True, default=str).encode()).hexdigest()
            if not isinstance(snapshot_sha256, str) or digest != snapshot_sha256:
                raise ValueError("The preview changed; review it again")
        clone_metadata = {**deepcopy(source.metadata), **(metadata or {})}
        if shared_room_owner is not None:
            # Private summaries, cached tool context, and instruction metadata
            # are never part of a guest-visible snapshot.
            clone_metadata = deepcopy(metadata or {})
            messages, file_grants, file_provenance = self._shareable_messages(
                messages,
                shared_room_owner,
                source_key=source.key,
                source_metadata=source.metadata,
                destination_key=destination_key,
            )
            # Never copy private grants wholesale.  Only the exact links that
            # survived transcript sanitization and match server provenance are
            # made available to the room session.
            clone_metadata.pop(_PUBLISHED_GRANTS_KEY, None)
            clone_metadata.pop(_PUBLISHED_PROVENANCE_KEY, None)
            if file_grants:
                clone_metadata[_PUBLISHED_GRANTS_KEY] = file_grants
                clone_metadata[_PUBLISHED_PROVENANCE_KEY] = file_provenance
        clone = Session(
            key=destination_key,
            messages=messages,
            created_at=now,
            updated_at=now,
            metadata=clone_metadata,
            last_consolidated=0 if shared_room_owner is not None else source.last_consolidated,
        )
        self.save(clone, fsync=True)
        return clone

    def _shareable_messages(
        self,
        messages: list[dict[str, Any]],
        owner_display_name: str,
        *,
        source_key: str,
        source_metadata: dict[str, Any],
        destination_key: str,
    ) -> tuple[list[dict[str, Any]], dict[str, dict[str, str]], dict[str, list[dict[str, Any]]]]:
        """Copy only the transcript users could see before a room was shared."""
        allowed = {
            "role",
            "content",
            "timestamp",
            "client_message_id",
            "client_message_ids",
            "participant_id",
            "participant_display_name",
        }
        shareable: list[dict[str, Any]] = []
        copied_grants: dict[str, dict[str, str]] = {}
        copied_provenance: dict[str, list[dict[str, Any]]] = {}
        owner = owner_display_name.strip()[:64] or "Owner"
        for message in messages:
            if message.get("role") not in {"user", "assistant"}:
                continue
            visible = {
                key: deepcopy(value)
                for key, value in message.items()
                if key in allowed
            }
            content = visible.get("content")
            if not isinstance(content, str) or not content.strip():
                continue
            if visible.get("role") == "assistant":
                copied: list[tuple[str, str]] = []
                grants = source_metadata.get(_PUBLISHED_GRANTS_KEY)
                provenance = source_metadata.get(_PUBLISHED_PROVENANCE_KEY)
                digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
                if isinstance(grants, dict) and isinstance(provenance, dict):
                    for file_id, grant in grants.items():
                        records = provenance.get(file_id)
                        filename = grant.get("filename") if isinstance(grant, dict) else None
                        if (
                            not isinstance(file_id, str)
                            or _PUBLISHED_FILE_ID_RE.fullmatch(file_id) is None
                            or not isinstance(filename, str)
                            or not isinstance(records, list)
                            or not any(
                                isinstance(record, dict)
                                and record.get("message_sha256") == digest
                                and record.get("timestamp") == message.get("timestamp")
                                and record.get("message_id")
                                == message.get(_PUBLISHED_MESSAGE_ID_KEY)
                                for record in records
                            )
                        ):
                            continue
                        source_url = self.published_file_url(source_key, file_id)
                        if self._has_published_markdown_link(content, source_url):
                            copied.append((file_id, filename))
                            content = content.replace(
                                source_url,
                                self.published_file_url(destination_key, file_id),
                            )
                if copied:
                    visible["content"] = content
                    destination_message_id = secrets.token_hex(16)
                    # Persisted internally so a room copied again has stable
                    # server-recorded identity even after history compaction.
                    # The WebSocket JSON handler removes this field on output.
                    visible[_PUBLISHED_MESSAGE_ID_KEY] = destination_message_id
                    rewritten_digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
                    for file_id, filename in copied:
                        copied_grants[file_id] = {"filename": filename}
                        copied_provenance.setdefault(file_id, []).append(
                            {
                                "message_sha256": rewritten_digest,
                                "timestamp": visible.get("timestamp"),
                                "message_id": destination_message_id,
                            }
                        )
            if visible.get("role") == "user":
                visible.setdefault("participant_id", "owner")
                visible.setdefault("participant_display_name", owner)
            shareable.append(visible)
        return shareable, copied_grants, copied_provenance

    def read_session_file(self, key: str) -> dict[str, Any] | None:
        """Load a session from disk without caching; intended for read-only HTTP endpoints.

        Returns ``{"key", "created_at", "updated_at", "metadata", "messages"}`` or
        ``None`` when the session file does not exist or fails to parse.
        """
        path = self._get_session_path(key)
        if not path.exists():
            return None
        try:
            messages: list[dict[str, Any]] = []
            metadata: dict[str, Any] = {}
            created_at: str | None = None
            updated_at: str | None = None
            stored_key: str | None = None
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    data = json.loads(line)
                    if data.get("_type") == "metadata":
                        metadata = data.get("metadata", {})
                        created_at = data.get("created_at")
                        updated_at = data.get("updated_at")
                        stored_key = data.get("key")
                    else:
                        messages.append(data)
            return {
                "key": stored_key or key,
                "created_at": created_at,
                "updated_at": updated_at,
                "metadata": metadata,
                "messages": messages,
            }
        except Exception as e:
            logger.warning("Failed to read session {}: {}", key, e)
            repaired = self._repair(key)
            if repaired is not None:
                logger.info("Recovered read-only session view {} from corrupt file", key)
                return self._session_payload(repaired)
            return None

    def set_session_title(
        self,
        key: str,
        title: str,
        *,
        room_id: str | None = None,
        title_revision: int | None = None,
    ) -> str:
        """Durably mutate the cached session metadata without replacing its turns.

        The metadata line is part of the session JSONL snapshot, so this uses the
        normal cache/save path rather than appending a competing metadata record.
        ``missing`` also covers filename aliases whose persisted canonical key
        does not exactly match the API key.
        """
        persisted = self.read_session_file(key)
        if persisted is None or persisted.get("key") != key:
            return "missing"
        session = self.get_or_create(key)
        metadata = session.metadata if isinstance(session.metadata, dict) else {}
        if room_id is None and metadata.get("shared_room") is True:
            return "shared"
        if room_id is not None and (
            metadata.get("shared_room") is not True or metadata.get("room_id") != room_id
        ):
            return "missing"
        if title_revision is not None:
            current = metadata.get("shared_room_title_revision", 0)
            current = current if isinstance(current, int) and not isinstance(current, bool) else 0
            if title_revision < current:
                return "older"
            metadata["shared_room_title_revision"] = title_revision
        metadata["title"] = title
        # Explicit names such as "Chat" are meaningful; clients must not
        # substitute a message preview for them.
        metadata["title_user_defined"] = True
        session.metadata = metadata
        self.save(session, fsync=True)
        return "updated"

    def read_session_preview(self, key: str) -> str:
        """Read a bounded first-user-message preview without loading session history."""
        path = self._get_session_path(key)
        if not path.exists():
            return ""
        scanned = 0
        try:
            with open(path, "rb") as f:
                for _ in range(SESSION_PREVIEW_MAX_LINES):
                    remaining = SESSION_PREVIEW_MAX_BYTES - scanned
                    if remaining <= 0:
                        break
                    line = f.readline(remaining + 1)
                    if not line or len(line) > remaining:
                        break
                    scanned += len(line)
                    data = json.loads(line)
                    if data.get("_type") == "metadata" or data.get("role") != "user":
                        continue
                    content = data.get("content", "")
                    if isinstance(content, str):
                        text = content
                    elif isinstance(content, list):
                        text = " ".join(
                            item["text"]
                            for item in content
                            if isinstance(item, dict) and isinstance(item.get("text"), str)
                        )
                    else:
                        text = ""
                    preview = " ".join(text.split())
                    if preview:
                        return preview[:SESSION_PREVIEW_MAX_CHARS]
                    media = data.get("media")
                    if isinstance(media, list) and media:
                        return "Media attachment"
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as e:
            logger.warning("Failed to read session preview {}: {}", key, e)
        return ""

    def list_sessions(self) -> list[dict[str, Any]]:
        """
        List all sessions.

        Returns:
            List of session info dicts.
        """
        self._migrate_legacy_sessions()
        sessions = []

        for path in self.sessions_dir.glob("*.jsonl"):
            fallback_key = path.stem.replace("_", ":", 1)
            try:
                # Read just the metadata line
                with open(path, encoding="utf-8") as f:
                    first_line = f.readline().strip()
                    if first_line:
                        data = json.loads(first_line)
                        if data.get("_type") == "metadata":
                            key = data.get("key") or path.stem.replace("_", ":", 1)
                            sessions.append({
                                "key": key,
                                "created_at": data.get("created_at"),
                                "updated_at": data.get("updated_at"),
                                "metadata": data.get("metadata", {}),
                                "path": str(path)
                            })
            except Exception:
                repaired = self._repair(fallback_key)
                if repaired is not None:
                    sessions.append({
                        "key": repaired.key,
                        "created_at": repaired.created_at.isoformat(),
                        "updated_at": repaired.updated_at.isoformat(),
                        "metadata": repaired.metadata,
                        "path": str(path)
                    })
                continue

        return sorted(sessions, key=lambda x: x.get("updated_at", ""), reverse=True)

    def search_sessions(self, query: str, *, limit: int = 20) -> list[dict[str, Any]]:
        """Search user-visible websocket conversation text.

        Results are message-level, newest-session first, and intentionally
        exclude system, tool, reasoning, and filesystem metadata.
        """
        normalized_query = " ".join(query.split())
        if len(normalized_query) < 2:
            return []
        limit = min(max(1, limit), SESSION_SEARCH_MAX_RESULTS)
        folded_query = normalized_query.casefold()
        results: list[dict[str, Any]] = []

        for summary in self.list_sessions():
            key = summary.get("key")
            if not isinstance(key, str) or not key.startswith("websocket:"):
                continue
            payload = self.read_session_file(key)
            if not isinstance(payload, dict):
                continue
            metadata = payload.get("metadata")
            title = metadata.get("title") if isinstance(metadata, dict) else None
            title = title.strip() if isinstance(title, str) else ""
            matches: list[dict[str, Any]] = []

            if title and folded_query in title.casefold():
                matches.append(self._search_result(
                    key=key,
                    title=title,
                    text=title,
                    folded_query=folded_query,
                    role="title",
                    message_index=None,
                    timestamp=summary.get("updated_at"),
                    updated_at=summary.get("updated_at"),
                ))

            messages = payload.get("messages")
            if isinstance(messages, list):
                for index in range(len(messages) - 1, -1, -1):
                    if len(matches) >= SESSION_SEARCH_MATCHES_PER_SESSION:
                        break
                    message = messages[index]
                    if not isinstance(message, dict):
                        continue
                    role = message.get("role")
                    if role not in {"user", "assistant"}:
                        continue
                    text = self._search_message_text(message.get("content"))
                    if not text or folded_query not in text.casefold():
                        continue
                    matches.append(self._search_result(
                        key=key,
                        title=title,
                        text=text,
                        folded_query=folded_query,
                        role=role,
                        message_index=index,
                        timestamp=message.get("timestamp"),
                        updated_at=summary.get("updated_at"),
                    ))

            results.extend(matches[: max(0, limit - len(results))])
            if len(results) >= limit:
                break

        return results

    @staticmethod
    def _search_message_text(content: Any) -> str:
        if isinstance(content, str):
            raw = content
        elif isinstance(content, list):
            raw = " ".join(
                item["text"]
                for item in content
                if isinstance(item, dict) and isinstance(item.get("text"), str)
            )
        else:
            return ""
        return " ".join(raw.split())

    @staticmethod
    def _search_result(
        *,
        key: str,
        title: str,
        text: str,
        folded_query: str,
        role: str,
        message_index: int | None,
        timestamp: Any,
        updated_at: Any,
    ) -> dict[str, Any]:
        folded_text = text.casefold()
        match = folded_text.find(folded_query)
        context = SESSION_SEARCH_SNIPPET_CHARS // 2
        start = max(0, match - context) if match >= 0 else 0
        end = min(len(text), start + SESSION_SEARCH_SNIPPET_CHARS)
        start = max(0, end - SESSION_SEARCH_SNIPPET_CHARS)
        snippet = text[start:end]
        if start:
            snippet = "..." + snippet.lstrip()
        if end < len(text):
            snippet = snippet.rstrip() + "..."
        result: dict[str, Any] = {
            "session_key": key,
            "title": title,
            "snippet": snippet,
            "role": role,
            "updated_at": updated_at,
        }
        if message_index is not None:
            result["message_index"] = message_index
        if isinstance(timestamp, str) and timestamp:
            result["timestamp"] = timestamp
        return result
