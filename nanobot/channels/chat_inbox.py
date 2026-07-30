"""Durable ingress and idempotency ledger for WebSocket chat messages."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from nanobot.bus.events import InboundMessage
from nanobot.utils.helpers import ensure_dir

_MAX_PROCESSED_RECEIPTS = 4_096


@dataclass(frozen=True)
class ChatInboxRecord:
    sequence: int
    message: InboundMessage
    client_message_id: str
    state: str
    payload_sha256: str
    retry_count: int


class ChatInboxStore:
    """Workspace-local SQLite inbox shared by the channel and agent loop."""

    def __init__(self, workspace: Path):
        root = ensure_dir(workspace / "chat")
        self.db_path = root / "inbox.sqlite3"
        self._init_db()

    async def accept(
        self,
        message: InboundMessage,
        client_message_id: str,
    ) -> tuple[str, ChatInboxRecord]:
        return await asyncio.to_thread(self._accept, message, client_message_id)

    async def mark_enqueued(self, chat_id: str, client_message_id: str) -> None:
        await self.claim_for_enqueue(chat_id, client_message_id)

    async def claim_for_enqueue(self, chat_id: str, client_message_id: str) -> bool:
        return await asyncio.to_thread(
            self._claim_for_enqueue,
            chat_id,
            client_message_id,
        )

    async def release_enqueue_claim(self, chat_id: str, client_message_id: str) -> None:
        await asyncio.to_thread(
            self._release_enqueue_claim,
            chat_id,
            client_message_id,
        )

    async def claim_retry_for_enqueue(
        self,
        chat_id: str,
        client_message_id: str,
    ) -> bool:
        return await asyncio.to_thread(
            self._claim_retry_for_enqueue,
            chat_id,
            client_message_id,
        )

    async def prepare_retry(
        self,
        chat_id: str,
        client_message_id: str,
    ) -> ChatInboxRecord:
        return await asyncio.to_thread(
            self._prepare_retry,
            chat_id,
            client_message_id,
        )

    async def mark_processed(self, chat_id: str, client_message_id: str) -> None:
        await asyncio.to_thread(
            self._set_state,
            chat_id,
            client_message_id,
            "processed",
        )

    async def mark_command_started(
        self,
        chat_id: str,
        client_message_id: str,
    ) -> bool:
        return await asyncio.to_thread(
            self._mark_command_started,
            chat_id,
            client_message_id,
        )

    async def recoverable(self) -> list[ChatInboxRecord]:
        return await asyncio.to_thread(self._recoverable)

    async def interrupted_commands(self) -> list[ChatInboxRecord]:
        return await asyncio.to_thread(self._interrupted_commands)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def _init_db(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS chat_inbox (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id TEXT NOT NULL,
                    client_message_id TEXT NOT NULL,
                    sender_id TEXT NOT NULL,
                    content TEXT NOT NULL,
                    media_json TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    session_key_override TEXT,
                    state TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    created_at_ns INTEGER NOT NULL,
                    updated_at_ns INTEGER NOT NULL,
                    UNIQUE(chat_id, client_message_id)
                );

                CREATE INDEX IF NOT EXISTS idx_chat_inbox_recovery
                    ON chat_inbox(state, sequence);
                """
            )
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(chat_inbox)").fetchall()
            }
            if "retry_count" not in columns:
                connection.execute(
                    "ALTER TABLE chat_inbox "
                    "ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0"
                )

    def _accept(
        self,
        message: InboundMessage,
        client_message_id: str,
    ) -> tuple[str, ChatInboxRecord]:
        media_json = json.dumps(message.media, ensure_ascii=False, separators=(",", ":"))
        metadata_json = json.dumps(
            message.metadata,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            default=str,
        )
        fingerprint_metadata_json = json.dumps(
            {
                key: value
                for key, value in message.metadata.items()
                if key != "remote"
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            default=str,
        )
        payload_sha256 = self._payload_sha256(
            message,
            client_message_id,
            self._media_fingerprints(message.media),
            fingerprint_metadata_json,
        )
        now = time.time_ns()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO chat_inbox (
                    chat_id, client_message_id, sender_id, content,
                    media_json, metadata_json, session_key_override,
                    state, payload_sha256, retry_count, created_at_ns, updated_at_ns
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'stored', ?, 0, ?, ?)
                """,
                (
                    message.chat_id,
                    client_message_id,
                    message.sender_id,
                    message.content,
                    media_json,
                    metadata_json,
                    message.session_key_override,
                    payload_sha256,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                """
                SELECT * FROM chat_inbox
                WHERE chat_id = ? AND client_message_id = ?
                """,
                (message.chat_id, client_message_id),
            ).fetchone()
        assert row is not None
        record = self._record(row)
        if record.payload_sha256 != payload_sha256:
            return "conflict", record
        return ("inserted" if cursor.rowcount == 1 else "existing"), record

    def _claim_for_enqueue(self, chat_id: str, client_message_id: str) -> bool:
        now = time.time_ns()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE chat_inbox
                SET state = 'enqueued', updated_at_ns = ?
                WHERE chat_id = ? AND client_message_id = ? AND state = 'stored'
                """,
                (now, chat_id, client_message_id),
            )
            if cursor.rowcount == 1:
                return True
            exists = connection.execute(
                """
                SELECT 1 FROM chat_inbox
                WHERE chat_id = ? AND client_message_id = ?
                """,
                (chat_id, client_message_id),
            ).fetchone()
            if exists is None:
                raise KeyError((chat_id, client_message_id))
            return False

    def _release_enqueue_claim(self, chat_id: str, client_message_id: str) -> None:
        now = time.time_ns()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE chat_inbox
                SET state = 'stored', updated_at_ns = ?
                WHERE chat_id = ? AND client_message_id = ? AND state = 'enqueued'
                """,
                (now, chat_id, client_message_id),
            )
            if cursor.rowcount == 1:
                return
            exists = connection.execute(
                """
                SELECT 1 FROM chat_inbox
                WHERE chat_id = ? AND client_message_id = ?
                """,
                (chat_id, client_message_id),
            ).fetchone()
            if exists is None:
                raise KeyError((chat_id, client_message_id))

    def _claim_retry_for_enqueue(
        self,
        chat_id: str,
        client_message_id: str,
    ) -> bool:
        now = time.time_ns()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE chat_inbox
                SET state = 'enqueued', updated_at_ns = ?
                WHERE chat_id = ? AND client_message_id = ? AND state = 'retry_wait'
                """,
                (now, chat_id, client_message_id),
            )
            if cursor.rowcount == 1:
                return True
            exists = connection.execute(
                """
                SELECT 1 FROM chat_inbox
                WHERE chat_id = ? AND client_message_id = ?
                """,
                (chat_id, client_message_id),
            ).fetchone()
            if exists is None:
                raise KeyError((chat_id, client_message_id))
            return False

    def _prepare_retry(
        self,
        chat_id: str,
        client_message_id: str,
    ) -> ChatInboxRecord:
        now = time.time_ns()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE chat_inbox
                SET state = 'retry_wait',
                    retry_count = retry_count + 1,
                    updated_at_ns = ?
                WHERE chat_id = ? AND client_message_id = ? AND state != 'processed'
                """,
                (now, chat_id, client_message_id),
            )
            row = connection.execute(
                """
                SELECT * FROM chat_inbox
                WHERE chat_id = ? AND client_message_id = ?
                """,
                (chat_id, client_message_id),
            ).fetchone()
        if row is None:
            raise KeyError((chat_id, client_message_id))
        if cursor.rowcount != 1 and row["state"] != "processed":
            raise RuntimeError("durable chat receipt could not be prepared for retry")
        return self._record(row)

    def _mark_command_started(self, chat_id: str, client_message_id: str) -> bool:
        now = time.time_ns()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE chat_inbox
                SET state = 'command_started', updated_at_ns = ?
                WHERE chat_id = ? AND client_message_id = ?
                  AND state IN ('stored', 'enqueued', 'retry_wait')
                """,
                (now, chat_id, client_message_id),
            )
            if cursor.rowcount == 1:
                return True
            row = connection.execute(
                """
                SELECT state FROM chat_inbox
                WHERE chat_id = ? AND client_message_id = ?
                """,
                (chat_id, client_message_id),
            ).fetchone()
        if row is None:
            raise KeyError((chat_id, client_message_id))
        return False

    def _set_state(self, chat_id: str, client_message_id: str, state: str) -> None:
        if state != "processed":
            raise ValueError(f"invalid chat inbox state: {state}")
        now = time.time_ns()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE chat_inbox
                SET state = 'processed', updated_at_ns = ?
                WHERE chat_id = ? AND client_message_id = ?
                """,
                (now, chat_id, client_message_id),
            )
            if cursor.rowcount != 1:
                raise KeyError((chat_id, client_message_id))
            connection.execute(
                """
                DELETE FROM chat_inbox
                WHERE sequence IN (
                    SELECT sequence FROM chat_inbox
                    WHERE state = 'processed'
                    ORDER BY sequence DESC
                    LIMIT -1 OFFSET ?
                )
                """,
                (_MAX_PROCESSED_RECEIPTS,),
            )

    def _recoverable(self) -> list[ChatInboxRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM chat_inbox
                WHERE state IN ('stored', 'enqueued', 'retry_wait')
                ORDER BY sequence ASC
                """
            ).fetchall()
        return [self._record(row) for row in rows]

    def _interrupted_commands(self) -> list[ChatInboxRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM chat_inbox
                WHERE state = 'command_started'
                ORDER BY sequence ASC
                """
            ).fetchall()
        return [self._record(row) for row in rows]

    @staticmethod
    def _payload_sha256(
        message: InboundMessage,
        client_message_id: str,
        media_fingerprints: list[str],
        metadata_json: str,
    ) -> str:
        payload = json.dumps(
            {
                "channel": message.channel,
                "chat_id": message.chat_id,
                "client_message_id": client_message_id,
                "content": message.content,
                "media": media_fingerprints,
                "metadata_json": metadata_json,
                "session_key_override": message.session_key_override,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode()).hexdigest()

    @staticmethod
    def _media_fingerprints(media: list[str]) -> list[str]:
        fingerprints: list[str] = []
        for item in media:
            path = Path(item)
            try:
                with path.open("rb") as source:
                    digest = hashlib.file_digest(source, "sha256").hexdigest()
                fingerprints.append(f"sha256:{digest}")
            except OSError:
                fingerprints.append(f"path:{item}")
        return fingerprints

    @staticmethod
    def _record(row: sqlite3.Row) -> ChatInboxRecord:
        metadata = json.loads(row["metadata_json"])
        metadata["client_message_id"] = row["client_message_id"]
        message = InboundMessage(
            channel="websocket",
            sender_id=row["sender_id"],
            chat_id=row["chat_id"],
            content=row["content"],
            media=json.loads(row["media_json"]),
            metadata=metadata,
            session_key_override=row["session_key_override"],
        )
        return ChatInboxRecord(
            sequence=row["sequence"],
            message=message,
            client_message_id=row["client_message_id"],
            state=row["state"],
            payload_sha256=row["payload_sha256"],
            retry_count=row["retry_count"],
        )
