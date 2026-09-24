"""MIT-1429: 0.2.x session files keep their conversations after the 0.3 move."""

from __future__ import annotations

import json
from pathlib import Path

from nanobot.session.manager import JsonlSessionStore, SessionManager

SESSION_ID = "BA6E0A4B-DA2D-4C73-A45C-A33373AD1FE2"
KEY = f"websocket:{SESSION_ID}"
LEGACY_NAME = f"websocket_{SESSION_ID}.jsonl"


def _legacy_lines(key: str = KEY, *, updated_at: str = "2026-07-28T13:58:44") -> str:
    records = [
        {
            "_type": "metadata",
            "key": key,
            "created_at": "2026-07-28T13:57:25",
            "updated_at": updated_at,
            "metadata": {},
            "last_consolidated": 0,
        },
        {"role": "user", "content": "can entropy be reversed?"},
        {"role": "assistant", "content": "Locally, yes; globally, no."},
    ]
    return "".join(json.dumps(record) + "\n" for record in records)


def _contents(manager: SessionManager) -> list[str]:
    return [m["content"] for m in manager.get_or_create(KEY).messages]


def _store_files(manager: SessionManager) -> list[str]:
    return sorted(p.name for p in manager.sessions_dir.glob("*.jsonl"))


def test_legacy_named_workspace_file_is_listed_and_loaded(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "sessions").mkdir(parents=True)
    legacy = workspace / "sessions" / LEGACY_NAME
    legacy.write_text(_legacy_lines(), encoding="utf-8")

    manager = SessionManager(workspace=workspace)

    assert KEY in [row["key"] for row in manager.list_sessions()]
    assert _contents(manager) == ["can entropy be reversed?", "Locally, yes; globally, no."]
    assert _store_files(manager) == [f"{JsonlSessionStore.storage_key(KEY)}.jsonl"]
    assert not legacy.exists()


def test_legacy_named_file_already_moved_into_store_is_repaired(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    first = SessionManager(workspace=workspace)
    # A previous build moved the file into the store but kept its legacy name.
    stranded = first.sessions_dir / LEGACY_NAME
    stranded.write_text(_legacy_lines(), encoding="utf-8")

    manager = SessionManager(workspace=workspace)

    assert KEY in [row["key"] for row in manager.list_sessions()]
    assert _contents(manager) == ["can entropy be reversed?", "Locally, yes; globally, no."]
    assert not stranded.exists()
    assert _store_files(manager) == [f"{JsonlSessionStore.storage_key(KEY)}.jsonl"]


def test_migration_twice_is_a_no_op(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "sessions").mkdir(parents=True)
    (workspace / "sessions" / LEGACY_NAME).write_text(_legacy_lines(), encoding="utf-8")

    manager = SessionManager(workspace=workspace)
    canonical = manager._get_session_path(KEY)
    before = canonical.read_bytes()
    before_files = _store_files(manager)

    again = SessionManager(workspace=workspace)
    store = again._jsonl_store
    with store.locked_session_files():
        store._migrate_from_workspace(store.workspace)

    assert _store_files(again) == before_files
    assert canonical.read_bytes() == before
    assert not (again.sessions_dir / ".migration-conflicts").exists()
    assert _contents(again) == ["can entropy be reversed?", "Locally, yes; globally, no."]


def test_differing_legacy_and_canonical_copies_are_both_kept(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    manager = SessionManager(workspace=workspace)
    session = manager.get_or_create(KEY)
    session.add_message("user", "newer canonical message")
    manager.save(session)
    stranded = manager.sessions_dir / LEGACY_NAME
    stranded.write_text(_legacy_lines(updated_at="2020-01-01T00:00:00"), encoding="utf-8")

    retried = SessionManager(workspace=workspace)

    assert _contents(retried)[-1] == "newer canonical message"
    conflicts = list((retried.sessions_dir / ".migration-conflicts").glob("*.jsonl"))
    assert len(conflicts) == 1
    assert "can entropy be reversed?" in conflicts[0].read_text(encoding="utf-8")
    assert not stranded.exists()


def test_rollback_restores_names_that_0_2_can_read(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "sessions").mkdir(parents=True)
    (workspace / "sessions" / LEGACY_NAME).write_text(_legacy_lines(), encoding="utf-8")
    manager = SessionManager(workspace=workspace)

    result = manager.restore_sessions_to_workspace()

    assert result.restored == 1
    restored = workspace / "sessions" / LEGACY_NAME
    assert "can entropy be reversed?" in restored.read_text(encoding="utf-8")
    # Starting 0.3 again maps the restored file back without conflicts.
    again = SessionManager(workspace=workspace)
    assert _contents(again) == ["can entropy be reversed?", "Locally, yes; globally, no."]
    assert not (again.sessions_dir / ".migration-conflicts").exists()
