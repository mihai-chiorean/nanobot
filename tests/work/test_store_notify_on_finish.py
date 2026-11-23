"""WorkStore ``notify_on_finish`` column (MIT-1855 / OA-12)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from nanobot.work.store import WorkStore


def _store(tmp_path: Path) -> WorkStore:
    return WorkStore(tmp_path, reconcile_on_open=False)


def _column(tmp_path: Path, name: str) -> bool:
    connection = sqlite3.connect(tmp_path / "work" / "work.sqlite3")
    try:
        rows = connection.execute("PRAGMA table_info(work_tasks)").fetchall()
    finally:
        connection.close()
    return any(row[1] == name for row in rows)


def test_create_task_stores_notify_on_finish(tmp_path: Path) -> None:
    store = _store(tmp_path)
    quiet = store.create_task(chat_id="chat-1", content="quiet task")
    loud = store.create_task(chat_id="chat-1", content="loud task", notify_on_finish=True)

    assert store.get_task(quiet["task_id"])["notify_on_finish"] == 0
    assert store.get_task(loud["task_id"])["notify_on_finish"] == 1
    # ``create_task`` returns the same row the store keeps.
    assert loud["notify_on_finish"] == 1


def test_column_migrates_into_an_old_database(tmp_path: Path) -> None:
    """A database written before MIT-1855 gains the column and stays writable.

    The DROP COLUMN below stands in for a pre-MIT-1855 database: the store's
    own ``_init_db`` must add the column back with the same lazy-migration
    style as ``scope``/``read_only`` and default existing rows to 0.
    """
    store = _store(tmp_path)
    task = store.create_task(chat_id="chat-1", content="pre-feature task")
    connection = sqlite3.connect(tmp_path / "work" / "work.sqlite3")
    try:
        connection.execute("ALTER TABLE work_tasks DROP COLUMN notify_on_finish")
        connection.commit()
    finally:
        connection.close()
    assert not _column(tmp_path, "notify_on_finish")

    reopened = _store(tmp_path)
    # Any operation brings the schema up, in the store's lazy-migration style.
    assert reopened.get_task(task["task_id"])["notify_on_finish"] == 0
    assert _column(tmp_path, "notify_on_finish")
    fresh = reopened.create_task(chat_id="chat-1", content="post-migration", notify_on_finish=True)
    assert reopened.get_task(fresh["task_id"])["notify_on_finish"] == 1
