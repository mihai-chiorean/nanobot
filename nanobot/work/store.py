"""SQLite-backed local Work task, event, step, and artifact store."""

from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
import shutil
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.utils.helpers import ensure_dir, safe_filename

TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled", "interrupted"})
ACTIVE_STATUSES = frozenset({"queued", "running", "waiting"})
VALID_STATUSES = frozenset({"scheduled", *ACTIVE_STATUSES, *TERMINAL_STATUSES})
MAX_ARTIFACT_BYTES = 100 * 1024 * 1024
MAX_ARTIFACTS_PER_TASK = 64
MAX_TASK_ARTIFACT_BYTES = 256 * 1024 * 1024
MAX_TENANT_ARTIFACT_BYTES = 1024 * 1024 * 1024
MAX_EVENTS_PER_TASK = 10_000
MAX_EVENT_PAGE = 500


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class WorkEvent:
    task_id: str
    seq: int
    type: str
    payload: dict[str, Any]
    actor: str = "system"
    step_id: str | None = None
    created_at: str = ""

    def to_api(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "seq": self.seq,
            "type": self.type,
            "actor": self.actor,
            "step_id": self.step_id,
            "created_at": self.created_at,
            "payload": self.payload,
        }


class WorkStore:
    """Synchronous workspace-local store for a single Nanobot tenant."""

    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.root = ensure_dir(workspace / "work")
        self.artifacts_root = ensure_dir(self.root / "artifacts")
        self.db_path = self.root / "work.sqlite3"
        self._io_lock = asyncio.Lock()
        self._init_db()
        self._has_compat_scope = self._column_exists("work_tasks", "scope")
        self.reconcile_interrupted()

    async def run_io(self, operation: Any, /, *args: Any, **kwargs: Any) -> Any:
        """Run one store operation off the event loop, preserving tenant-local order."""
        async with self._io_lock:
            return await asyncio.to_thread(operation, *args, **kwargs)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def _init_db(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS work_tasks (
                    task_id TEXT PRIMARY KEY,
                    session_key TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    prompt_preview TEXT NOT NULL,
                    status TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    model TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    last_seq INTEGER NOT NULL DEFAULT 0,
                    result_summary TEXT,
                    error TEXT,
                    artifact_count INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS work_steps (
                    step_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    seq_start INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    summary TEXT,
                    FOREIGN KEY(task_id) REFERENCES work_tasks(task_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS work_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    type TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    step_id TEXT,
                    created_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    UNIQUE(task_id, seq),
                    FOREIGN KEY(task_id) REFERENCES work_tasks(task_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS work_artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    step_id TEXT,
                    kind TEXT NOT NULL,
                    name TEXT NOT NULL,
                    mime TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    path_rel TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    summary TEXT,
                    FOREIGN KEY(task_id) REFERENCES work_tasks(task_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS work_commands (
                    command_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    dispatched_at TEXT,
                    FOREIGN KEY(task_id) REFERENCES work_tasks(task_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_work_tasks_updated
                    ON work_tasks(updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_work_events_task_seq
                    ON work_events(task_id, seq);
                CREATE INDEX IF NOT EXISTS idx_work_artifacts_task
                    ON work_artifacts(task_id);
                """
            )
            task_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(work_tasks)").fetchall()
            }
            if "request_id" not in task_columns:
                connection.execute("ALTER TABLE work_tasks ADD COLUMN request_id TEXT")
            if "dispatched_at" not in task_columns:
                connection.execute("ALTER TABLE work_tasks ADD COLUMN dispatched_at TEXT")
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_work_tasks_request_id
                    ON work_tasks(request_id) WHERE request_id IS NOT NULL
                """
            )

    def _column_exists(self, table: str, column: str) -> bool:
        with self._connect() as connection:
            rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
        return any(row["name"] == column for row in rows)

    @staticmethod
    def _task_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        item = dict(row)
        item.pop("scope", None)
        item.pop("request_id", None)
        item.pop("dispatched_at", None)
        return item

    @staticmethod
    def _preview(text: str, limit: int = 240) -> str:
        return " ".join((text or "").split())[:limit]

    def create_task(
        self,
        *,
        session_key: str | None = None,
        chat_id: str,
        content: str,
        mode: str = "background",
        title: str | None = None,
        model: str = "",
        status: str = "queued",
        request_id: str | None = None,
    ) -> dict[str, Any]:
        now = utc_now()
        task_id = f"work_{uuid.uuid4().hex}"
        session_key = session_key or f"work:{task_id}"
        prompt_preview = self._preview(content)
        clean_title = self._preview(title or prompt_preview, 96) or "Untitled task"
        clean_status = status if status in {"scheduled", *ACTIVE_STATUSES} else "queued"
        columns = [
            "task_id",
            "session_key",
            "chat_id",
            "title",
            "prompt_preview",
            "status",
            "mode",
            "model",
            "created_at",
            "updated_at",
            "last_seq",
        ]
        values: list[Any] = [
            task_id,
            session_key,
            chat_id,
            clean_title,
            prompt_preview,
            clean_status,
            mode,
            model,
            now,
            now,
            0,
        ]
        if request_id:
            columns.append("request_id")
            values.append(request_id)
        # Production builds predating single-tenant Clerk auth stored a scope
        # column. Populate it only to keep those existing databases writable;
        # it is never returned or used for authorization.
        if self._has_compat_scope:
            columns.insert(1, "scope")
            values.insert(1, "tenant")
        placeholders = ", ".join("?" for _ in columns)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if request_id:
                existing = connection.execute(
                    "SELECT * FROM work_tasks WHERE request_id = ?", (request_id,)
                ).fetchone()
                if existing is not None:
                    task = self._task_row(existing)
                    assert task is not None
                    task["_was_created"] = False
                    task["_was_dispatched"] = existing["dispatched_at"] is not None
                    return task
            connection.execute(
                f"INSERT INTO work_tasks ({', '.join(columns)}) VALUES ({placeholders})",
                values,
            )
        self.append_event(
            task_id,
            "task.created",
            {"task_id": task_id, "title": clean_title, "status": clean_status},
        )
        task = self.get_task(task_id)
        assert task is not None
        if request_id:
            task["_was_created"] = True
            task["_was_dispatched"] = False
        return task

    def mark_dispatched(self, task_id: str, request_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE work_tasks SET dispatched_at = COALESCE(dispatched_at, ?)
                WHERE task_id = ? AND request_id = ?
                """,
                (utc_now(), task_id, request_id),
            )

    def reserve_command(self, command_id: str, task_id: str, kind: str) -> tuple[bool, bool]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT task_id, kind, dispatched_at FROM work_commands WHERE command_id = ?",
                (command_id,),
            ).fetchone()
            if existing is not None:
                if existing["task_id"] != task_id or existing["kind"] != kind:
                    raise ValueError("Work command id is already bound")
                return False, existing["dispatched_at"] is not None
            connection.execute(
                "INSERT INTO work_commands (command_id, task_id, kind) VALUES (?, ?, ?)",
                (command_id, task_id, kind),
            )
            return True, False

    def mark_command_dispatched(self, command_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE work_commands SET dispatched_at = COALESCE(dispatched_at, ?) WHERE command_id = ?",
                (utc_now(), command_id),
            )

    def release_command(self, command_id: str, task_id: str, kind: str) -> None:
        """Release a reservation that was not published so the caller can retry it."""
        with self._connect() as connection:
            connection.execute(
                """
                DELETE FROM work_commands
                WHERE command_id = ? AND task_id = ? AND kind = ? AND dispatched_at IS NULL
                """,
                (command_id, task_id, kind),
            )

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM work_tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
        return self._task_row(row)

    def list_tasks(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        status: str | None = None,
        after_task_id: str | None = None,
        order_by_task_id: bool = False,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 200))
        offset = max(0, offset)
        params: list[Any] = []
        conditions: list[str] = []
        if status:
            conditions.append("status = ?")
            params.append(status)
        if order_by_task_id and after_task_id:
            conditions.append("task_id > ?")
            params.append(after_task_id)
        where = "WHERE " + " AND ".join(conditions) if conditions else ""
        order = "task_id ASC" if order_by_task_id else "updated_at DESC, task_id DESC"
        params.extend((limit, offset))
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM work_tasks {where} ORDER BY {order} LIMIT ? OFFSET ?",
                params,
            ).fetchall()
        return [item for row in rows if (item := self._task_row(row)) is not None]

    def append_event(
        self,
        task_id: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
        *,
        actor: str = "system",
        step_id: str | None = None,
    ) -> WorkEvent | None:
        payload = payload or {}
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            return self._append_event_in_transaction(
                connection,
                task_id,
                event_type,
                payload,
                actor=actor,
                step_id=step_id,
                created_at=now,
            )

    @staticmethod
    def _append_event_in_transaction(
        connection: sqlite3.Connection,
        task_id: str,
        event_type: str,
        payload: dict[str, Any],
        *,
        actor: str,
        step_id: str | None,
        created_at: str,
    ) -> WorkEvent | None:
        row = connection.execute(
            "SELECT last_seq FROM work_tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        if row is None:
            logger.warning("WorkStore append_event for missing task {}", task_id)
            return None
        seq = int(row["last_seq"]) + 1
        connection.execute(
            """
            INSERT INTO work_events (
                task_id, seq, type, actor, step_id, created_at, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                seq,
                event_type,
                actor,
                step_id,
                created_at,
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
            ),
        )
        connection.execute(
            "UPDATE work_tasks SET last_seq = ?, updated_at = ? WHERE task_id = ?",
            (seq, created_at, task_id),
        )
        connection.execute(
            "DELETE FROM work_events WHERE task_id = ? AND seq <= ?",
            (task_id, seq - MAX_EVENTS_PER_TASK),
        )
        return WorkEvent(task_id, seq, event_type, payload, actor, step_id, created_at)

    def update_status(
        self,
        task_id: str,
        status: str,
        *,
        error: str | None = None,
        result_summary: str | None = None,
    ) -> WorkEvent | None:
        if status not in VALID_STATUSES:
            raise ValueError(f"Unknown Work status: {status}")
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT status FROM work_tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if current is None:
                return None
            if current["status"] in TERMINAL_STATUSES:
                return None
            fields = ["status = ?", "updated_at = ?"]
            params: list[Any] = [status, now]
            if status == "running":
                fields.append("started_at = COALESCE(started_at, ?)")
                params.append(now)
            if status in TERMINAL_STATUSES:
                fields.append("completed_at = ?")
                params.append(now)
            if error is not None:
                fields.append("error = ?")
                params.append(error)
            if result_summary is not None:
                fields.append("result_summary = ?")
                params.append(result_summary)
            params.append(task_id)
            connection.execute(
                f"UPDATE work_tasks SET {', '.join(fields)} WHERE task_id = ?", params
            )
            return self._append_event_in_transaction(
                connection,
                task_id,
                "status.changed",
                {"status": status, "error": error, "result_summary": result_summary},
                actor="system",
                step_id=None,
                created_at=now,
            )

    def start_step(self, task_id: str, title: str, *, actor: str = "main_agent") -> str | None:
        step_id = f"step_{uuid.uuid4().hex}"
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            event = self._append_event_in_transaction(
                connection,
                task_id,
                "step.started",
                {"title": title},
                actor=actor,
                step_id=step_id,
                created_at=now,
            )
            if event is None:
                return None
            connection.execute(
                """
                INSERT INTO work_steps (
                    step_id, task_id, seq_start, title, status, started_at
                ) VALUES (?, ?, ?, ?, 'running', ?)
                """,
                (step_id, task_id, event.seq, self._preview(title, 160), now),
            )
            self._append_event_in_transaction(
                connection,
                task_id,
                "step.bound",
                {"step_id": step_id},
                actor=actor,
                step_id=step_id,
                created_at=now,
            )
        return step_id

    def finish_step(
        self,
        task_id: str,
        step_id: str,
        *,
        status: str = "succeeded",
        summary: str | None = None,
        actor: str = "main_agent",
    ) -> None:
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE work_steps
                SET status = ?, completed_at = ?, summary = COALESCE(?, summary)
                WHERE task_id = ? AND step_id = ?
                """,
                (status, now, summary, task_id, step_id),
            )
            if cursor.rowcount:
                self._append_event_in_transaction(
                    connection,
                    task_id,
                    "step.finished",
                    {"status": status, "summary": summary},
                    actor=actor,
                    step_id=step_id,
                    created_at=now,
                )

    def list_events(
        self,
        task_id: str,
        *,
        after_seq: int = 0,
        limit: int = MAX_EVENT_PAGE,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(limit, MAX_EVENT_PAGE))
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM work_events
                WHERE task_id = ? AND seq > ?
                ORDER BY seq ASC
                LIMIT ?
                """,
                (task_id, after_seq, limit),
            ).fetchall()
        events: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            try:
                payload = json.loads(item.pop("payload_json") or "{}")
            except json.JSONDecodeError:
                payload = {}
            item["payload"] = payload
            events.append(item)
        return events

    def list_steps(self, task_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM work_steps WHERE task_id = ? ORDER BY seq_start ASC",
                (task_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def task_snapshot(self, task_id: str) -> dict[str, Any] | None:
        task = self.get_task(task_id)
        if task is None:
            return None
        task["steps"] = self.list_steps(task_id)
        task["artifacts"] = self.list_artifacts(task_id)
        return task

    def add_artifact(
        self,
        task_id: str,
        *,
        name: str,
        kind: str,
        content: str | bytes | None = None,
        source_path: Path | None = None,
        summary: str | None = None,
        step_id: str | None = None,
    ) -> dict[str, Any]:
        if self.get_task(task_id) is None:
            raise ValueError("Work task does not exist")
        artifact_id = f"artifact_{uuid.uuid4().hex}"
        safe_name = safe_filename(name) or f"{artifact_id}.txt"
        artifact_dir = ensure_dir(self.artifacts_root / task_id / artifact_id)
        destination = artifact_dir / safe_name
        try:
            if source_path is not None:
                if not source_path.is_file():
                    raise ValueError("Work artifact source is not a file")
                size_bytes = source_path.stat().st_size
                if size_bytes > MAX_ARTIFACT_BYTES:
                    raise ValueError("Work artifact exceeds the 100 MiB limit")
                shutil.copyfile(source_path, destination)
            else:
                data = content if content is not None else b""
                if isinstance(data, str):
                    data = data.encode("utf-8")
                if len(data) > MAX_ARTIFACT_BYTES:
                    raise ValueError("Work artifact exceeds the 100 MiB limit")
                destination.write_bytes(data)
            digest = hashlib.sha256()
            size_bytes = 0
            with destination.open("rb") as artifact_file:
                while chunk := artifact_file.read(1024 * 1024):
                    size_bytes += len(chunk)
                    if size_bytes > MAX_ARTIFACT_BYTES:
                        raise ValueError("Work artifact exceeds the 100 MiB limit")
                    digest.update(chunk)
        except Exception:
            shutil.rmtree(artifact_dir, ignore_errors=True)
            raise
        mime = mimetypes.guess_type(destination.name)[0] or "application/octet-stream"
        now = utc_now()
        artifact = {
            "artifact_id": artifact_id,
            "task_id": task_id,
            "step_id": step_id,
            "kind": kind,
            "name": safe_name,
            "mime": mime,
            "size_bytes": size_bytes,
            "sha256": digest.hexdigest(),
            "path_rel": destination.relative_to(self.root).as_posix(),
            "created_at": now,
            "summary": summary,
        }
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                task_usage = connection.execute(
                    """
                    SELECT COUNT(*) AS artifact_count,
                           COALESCE(SUM(size_bytes), 0) AS size_bytes
                    FROM work_artifacts WHERE task_id = ?
                    """,
                    (task_id,),
                ).fetchone()
                tenant_usage = connection.execute(
                    "SELECT COALESCE(SUM(size_bytes), 0) AS size_bytes FROM work_artifacts"
                ).fetchone()
                if int(task_usage["artifact_count"]) >= MAX_ARTIFACTS_PER_TASK:
                    raise ValueError("Work task artifact count limit reached")
                if int(task_usage["size_bytes"]) + size_bytes > MAX_TASK_ARTIFACT_BYTES:
                    raise ValueError("Work task artifact storage limit reached")
                if int(tenant_usage["size_bytes"]) + size_bytes > MAX_TENANT_ARTIFACT_BYTES:
                    raise ValueError("Tenant Work artifact storage limit reached")
                connection.execute(
                    """
                    INSERT INTO work_artifacts (
                        artifact_id, task_id, step_id, kind, name, mime, size_bytes,
                        sha256, path_rel, created_at, summary
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    tuple(
                        artifact[key]
                        for key in (
                            "artifact_id",
                            "task_id",
                            "step_id",
                            "kind",
                            "name",
                            "mime",
                            "size_bytes",
                            "sha256",
                            "path_rel",
                            "created_at",
                            "summary",
                        )
                    ),
                )
                connection.execute(
                    """
                    UPDATE work_tasks
                    SET artifact_count = artifact_count + 1, updated_at = ?
                    WHERE task_id = ?
                    """,
                    (now, task_id),
                )
                self._append_event_in_transaction(
                    connection,
                    task_id,
                    "artifact.created",
                    {key: value for key, value in artifact.items() if key != "path_rel"},
                    actor="main_agent",
                    step_id=step_id,
                    created_at=now,
                )
        except Exception:
            shutil.rmtree(artifact_dir, ignore_errors=True)
            raise
        return artifact

    def list_artifacts(self, task_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT artifact_id, task_id, step_id, kind, name, mime, size_bytes,
                       sha256, created_at, summary
                FROM work_artifacts
                WHERE task_id = ?
                ORDER BY created_at ASC
                """,
                (task_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def artifact_path(self, artifact_id: str) -> tuple[Path, dict[str, Any]] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM work_artifacts WHERE artifact_id = ?", (artifact_id,)
            ).fetchone()
        if row is None:
            return None
        metadata = dict(row)
        try:
            root = self.root.resolve()
            path = (root / metadata["path_rel"]).resolve()
            path.relative_to(root)
        except (OSError, ValueError):
            return None
        if not path.is_file():
            return None
        return path, metadata

    def reconcile_interrupted(self) -> int:
        now = utc_now()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT task_id FROM work_tasks
                WHERE status IN ('queued', 'running')
                """
            ).fetchall()
            task_ids = [str(row["task_id"]) for row in rows]
            connection.execute(
                """
                UPDATE work_tasks
                SET status = 'interrupted', updated_at = ?, completed_at = ?,
                    error = COALESCE(error, 'Gateway restarted before this task completed.')
                WHERE status IN ('queued', 'running')
                """,
                (now, now),
            )
        for task_id in task_ids:
            self.append_event(
                task_id,
                "status.changed",
                {
                    "status": "interrupted",
                    "error": "Gateway restarted before this task completed.",
                },
            )
        return len(task_ids)
