"""SQLite-backed local Work store.

The Work store is intentionally local to the appliance workspace. It records
background task state, visible progress events, and artifact metadata without
requiring a hosted control plane.
"""

from __future__ import annotations

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
    """Small synchronous SQLite store for Work tasks."""

    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.root = ensure_dir(workspace / "work")
        self.artifacts_root = ensure_dir(self.root / "artifacts")
        self.db_path = self.root / "work.sqlite3"
        self._init_db()
        self.reconcile_interrupted()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS work_tasks (
                    task_id TEXT PRIMARY KEY,
                    scope TEXT NOT NULL,
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

                CREATE INDEX IF NOT EXISTS idx_work_tasks_scope_updated
                    ON work_tasks(scope, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_work_events_task_seq
                    ON work_events(task_id, seq);
                CREATE INDEX IF NOT EXISTS idx_work_artifacts_task
                    ON work_artifacts(task_id);
                """
            )

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    @staticmethod
    def _preview(text: str, limit: int = 240) -> str:
        return " ".join((text or "").split())[:limit]

    def create_task(
        self,
        *,
        scope: str,
        session_key: str,
        chat_id: str,
        content: str,
        mode: str = "background",
        title: str | None = None,
        model: str = "",
        status: str = "queued",
    ) -> dict[str, Any]:
        now = utc_now()
        task_id = f"work_{uuid.uuid4().hex}"
        prompt_preview = self._preview(content)
        clean_title = self._preview(title or prompt_preview, limit=96) or "Untitled task"
        clean_status = status if status in {"scheduled", *ACTIVE_STATUSES} else "queued"
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO work_tasks (
                    task_id, scope, session_key, chat_id, title, prompt_preview,
                    status, mode, model, created_at, updated_at, last_seq
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    task_id,
                    scope,
                    session_key,
                    chat_id,
                    clean_title,
                    prompt_preview,
                    clean_status,
                    mode,
                    model,
                    now,
                    now,
                ),
            )
        self.append_event(
            task_id,
            "task.created",
            {"task_id": task_id, "title": clean_title, "status": clean_status},
        )
        task = self.get_task(task_id)
        assert task is not None
        return task

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            return self._row(
                conn.execute("SELECT * FROM work_tasks WHERE task_id = ?", (task_id,)).fetchone()
            )

    def list_tasks(
        self,
        *,
        scope: str,
        limit: int = 50,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 200))
        params: list[Any] = [scope]
        where = "scope = ?"
        if status:
            where += " AND status = ?"
            params.append(status)
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM work_tasks WHERE {where} ORDER BY updated_at DESC LIMIT ?",
                params,
            ).fetchall()
        return [dict(r) for r in rows]

    def scope_allowed(self, task_id: str, scope: str) -> bool:
        task = self.get_task(task_id)
        return bool(task and task.get("scope") == scope)

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
        with self._connect() as conn:
            row = conn.execute(
                "SELECT last_seq FROM work_tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if row is None:
                logger.warning("WorkStore append_event for missing task {}", task_id)
                return None
            seq = int(row["last_seq"]) + 1
            conn.execute(
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
                    now,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                ),
            )
            conn.execute(
                "UPDATE work_tasks SET last_seq = ?, updated_at = ? WHERE task_id = ?",
                (seq, now, task_id),
            )
        return WorkEvent(
            task_id=task_id,
            seq=seq,
            type=event_type,
            actor=actor,
            step_id=step_id,
            created_at=now,
            payload=payload,
        )

    def update_status(
        self,
        task_id: str,
        status: str,
        *,
        error: str | None = None,
        result_summary: str | None = None,
    ) -> WorkEvent | None:
        now = utc_now()
        current = self.get_task(task_id)
        if current is None:
            return None
        if current.get("status") in TERMINAL_STATUSES and current.get("status") != status:
            return None
        fields = ["status = ?", "updated_at = ?"]
        params: list[Any] = [status, now]
        if status == "running":
            fields.append("started_at = COALESCE(started_at, ?)")
            params.append(now)
        if status in TERMINAL_STATUSES or status == "waiting":
            fields.append("completed_at = ?")
            params.append(now if status in TERMINAL_STATUSES else None)
        if error is not None:
            fields.append("error = ?")
            params.append(error)
        if result_summary is not None:
            fields.append("result_summary = ?")
            params.append(result_summary)
        params.append(task_id)
        with self._connect() as conn:
            conn.execute(
                f"UPDATE work_tasks SET {', '.join(fields)} WHERE task_id = ?",
                params,
            )
        return self.append_event(
            task_id,
            "status.changed",
            {"status": status, "error": error, "result_summary": result_summary},
        )

    def start_step(self, task_id: str, title: str, *, actor: str = "main_agent") -> str | None:
        event = self.append_event(task_id, "step.started", {"title": title}, actor=actor)
        if event is None:
            return None
        step_id = f"step_{uuid.uuid4().hex}"
        now = utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO work_steps (
                    step_id, task_id, seq_start, title, status, started_at
                ) VALUES (?, ?, ?, ?, 'running', ?)
                """,
                (step_id, task_id, event.seq, self._preview(title, 160), now),
            )
        self.append_event(task_id, "step.bound", {"step_id": step_id}, actor=actor, step_id=step_id)
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
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE work_steps
                SET status = ?, completed_at = ?, summary = COALESCE(?, summary)
                WHERE task_id = ? AND step_id = ?
                """,
                (status, now, summary, task_id, step_id),
            )
        self.append_event(
            task_id,
            "step.finished",
            {"status": status, "summary": summary},
            actor=actor,
            step_id=step_id,
        )

    def list_events(self, task_id: str, *, after_seq: int = 0) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM work_events
                WHERE task_id = ? AND seq > ?
                ORDER BY seq ASC
                """,
                (task_id, after_seq),
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
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM work_steps WHERE task_id = ? ORDER BY seq_start ASC",
                (task_id,),
            ).fetchall()
        return [dict(r) for r in rows]

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
        import hashlib

        artifact_id = f"artifact_{uuid.uuid4().hex}"
        safe_name = safe_filename(name) or f"{artifact_id}.txt"
        task_dir = ensure_dir(self.artifacts_root / task_id)
        dest = task_dir / safe_name
        if source_path is not None:
            shutil.copyfile(source_path, dest)
        else:
            data = content if content is not None else b""
            if isinstance(data, str):
                data = data.encode("utf-8")
            dest.write_bytes(data)
        body = dest.read_bytes()
        mime = mimetypes.guess_type(dest.name)[0] or "application/octet-stream"
        rel = dest.relative_to(self.root).as_posix()
        now = utc_now()
        artifact = {
            "artifact_id": artifact_id,
            "task_id": task_id,
            "step_id": step_id,
            "kind": kind,
            "name": safe_name,
            "mime": mime,
            "size_bytes": len(body),
            "sha256": hashlib.sha256(body).hexdigest(),
            "path_rel": rel,
            "created_at": now,
            "summary": summary,
        }
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO work_artifacts (
                    artifact_id, task_id, step_id, kind, name, mime, size_bytes,
                    sha256, path_rel, created_at, summary
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact_id,
                    task_id,
                    step_id,
                    kind,
                    safe_name,
                    mime,
                    len(body),
                    artifact["sha256"],
                    rel,
                    now,
                    summary,
                ),
            )
            conn.execute(
                """
                UPDATE work_tasks
                SET artifact_count = artifact_count + 1, updated_at = ?
                WHERE task_id = ?
                """,
                (now, task_id),
            )
        self.append_event(
            task_id,
            "artifact.created",
            {k: v for k, v in artifact.items() if k != "path_rel"},
            actor="main_agent",
            step_id=step_id,
        )
        return artifact

    def list_artifacts(self, task_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT artifact_id, task_id, step_id, kind, name, mime, size_bytes,
                       sha256, created_at, summary
                FROM work_artifacts
                WHERE task_id = ?
                ORDER BY created_at ASC
                """,
                (task_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def artifact_path(self, artifact_id: str, scope: str) -> tuple[Path, dict[str, Any]] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT a.*, t.scope
                FROM work_artifacts a
                JOIN work_tasks t ON t.task_id = a.task_id
                WHERE a.artifact_id = ?
                """,
                (artifact_id,),
            ).fetchone()
        if row is None or row["scope"] != scope:
            return None
        meta = dict(row)
        try:
            root = self.root.resolve()
            path = (root / meta["path_rel"]).resolve()
            path.relative_to(root)
        except (OSError, ValueError):
            return None
        if not path.is_file():
            return None
        return path, meta

    def reconcile_interrupted(self) -> int:
        now = utc_now()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT task_id FROM work_tasks
                WHERE status IN ('queued', 'running', 'waiting')
                """
            ).fetchall()
            task_ids = [str(r["task_id"]) for r in rows]
            conn.execute(
                """
                UPDATE work_tasks
                SET status = 'interrupted', updated_at = ?, completed_at = ?,
                    error = COALESCE(error, 'Gateway restarted before this task completed.')
                WHERE status IN ('queued', 'running', 'waiting')
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
