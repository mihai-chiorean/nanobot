"""llm_usage ``turn_id`` column and its v1 -> v2 migration (MIT-1862 / TP-05).

The store ships ``SCHEMA_VERSION = 2`` with a nullable ``turn_id TEXT`` column;
files created under v1 (no such column) must be migrated in place with
``ALTER TABLE ... ADD COLUMN`` when first opened, and a record written with
``turn_id="t1"`` must read back ``"t1"`` through ``recent_calls``.

The v1 fixture below is the verbatim v1 schema (pre-TP-05 ``store.py``,
``git show origin/ziggy-main:nanobot/llm_usage/store.py``), so the migration
is exercised against the file real deployments carry — not a schema the fix
itself wrote.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from nanobot.llm_usage.models import LLMCallRecord
from nanobot.llm_usage.store import SCHEMA_VERSION, LLMUsageStore

_NOW_MS = int(time.time() * 1000)  # rows older than 400 days are pruned

_V1_CREATE = """
CREATE TABLE llm_calls (
    id INTEGER PRIMARY KEY,
    started_at_ms INTEGER NOT NULL,
    duration_ms INTEGER NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    source TEXT NOT NULL,
    stream INTEGER NOT NULL,
    finish_reason TEXT NOT NULL,
    input_tokens INTEGER,
    output_tokens INTEGER,
    total_tokens INTEGER,
    cache_read_tokens INTEGER,
    cache_write_tokens INTEGER,
    reported_tokens INTEGER,
    estimated_tokens INTEGER,
    generation_ms INTEGER,
    measured_output_tokens INTEGER,
    ttft_ms INTEGER,
    timed_requests INTEGER,
    error_status_code INTEGER,
    error_kind TEXT
);
CREATE INDEX llm_calls_started_at_idx ON llm_calls(started_at_ms);
CREATE INDEX llm_calls_provider_model_time_idx ON llm_calls(provider, model, started_at_ms);
"""


def _make_v1_file(path: Path) -> None:
    """A database exactly as the v1 store would have written it."""
    connection = sqlite3.connect(path)
    connection.executescript(_V1_CREATE)
    connection.execute(
        "INSERT INTO llm_calls (started_at_ms, duration_ms, provider, model,"
        " source, stream, finish_reason) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (_NOW_MS - 60_000, 42, "fake", "test-model", "user", 0, "stop"),
    )
    connection.execute("PRAGMA user_version = 1")
    connection.commit()
    connection.close()


def _columns_and_version(path: Path) -> tuple[set[str], int]:
    connection = sqlite3.connect(path)
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(llm_calls)")}
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        return columns, version
    finally:
        connection.close()


def _call(**overrides: object) -> LLMCallRecord:
    fields: dict[str, object] = {
        "started_at_ms": _NOW_MS,
        "duration_ms": 10,
        "provider": "fake",
        "model": "test-model",
        "source": "user",
        "stream": False,
        "finish_reason": "stop",
    }
    fields.update(overrides)
    return LLMCallRecord(**fields)  # type: ignore[arg-type]


def test_v1_file_is_migrated_in_place(tmp_path: Path) -> None:
    path = tmp_path / "llm-usage.sqlite3"
    _make_v1_file(path)
    assert "turn_id" not in _columns_and_version(path)[0]  # precondition

    store = LLMUsageStore(path)
    try:
        assert store.count() == 1  # legacy row survived the migration
    finally:
        store.close()

    columns, version = _columns_and_version(path)
    assert "turn_id" in columns
    assert version == SCHEMA_VERSION == 2


def test_record_with_turn_id_reads_back(tmp_path: Path) -> None:
    path = tmp_path / "llm-usage.sqlite3"
    _make_v1_file(path)

    store = LLMUsageStore(path)
    try:
        store.record(_call(turn_id="t1"))
        rows = {row["started_at_ms"]: row for row in store.recent_calls(limit=10)}
        assert rows[_NOW_MS]["turn_id"] == "t1"
        # Pre-existing v1 rows read back NULL, not empty string.
        assert rows[_NOW_MS - 60_000]["turn_id"] is None
    finally:
        store.close()


def test_record_without_turn_id_defaults_to_none(tmp_path: Path) -> None:
    path = tmp_path / "llm-usage.sqlite3"  # fresh v2 file, no migration path
    store = LLMUsageStore(path)
    try:
        store.record(_call())
        rows = store.recent_calls(limit=10)
        assert len(rows) == 1
        assert rows[0]["turn_id"] is None
    finally:
        store.close()


def test_migration_is_idempotent_on_reopen(tmp_path: Path) -> None:
    path = tmp_path / "llm-usage.sqlite3"
    _make_v1_file(path)
    for _ in range(2):  # second open must not trip over the existing column
        store = LLMUsageStore(path)
        store.record(_call(turn_id="t1"))
        store.close()
    columns, version = _columns_and_version(path)
    assert "turn_id" in columns and version == 2
