from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from nanobot.work import store as store_module
from nanobot.work.store import WorkStore


def _task(store: WorkStore, *, status: str = "queued") -> dict:
    return store.create_task(
        session_key="websocket:chat-1",
        chat_id="chat-1",
        content="Prepare a report",
        status=status,
    )


def test_reconcile_preserves_waiting_tasks(tmp_path: Path) -> None:
    store = WorkStore(tmp_path)
    waiting = _task(store)
    store.update_status(waiting["task_id"], "waiting")
    queued = _task(store)
    running = _task(store)
    store.update_status(running["task_id"], "running")

    restarted = WorkStore(tmp_path)

    assert restarted.get_task(waiting["task_id"])["status"] == "waiting"
    assert restarted.get_task(queued["task_id"])["status"] == "interrupted"
    assert restarted.get_task(running["task_id"])["status"] == "interrupted"


def test_terminal_status_transition_is_atomic(tmp_path: Path) -> None:
    store = WorkStore(tmp_path)
    task = _task(store)
    barrier = Barrier(2)

    def finish(status: str):
        barrier.wait()
        return store.update_status(task["task_id"], status)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(finish, ["succeeded", "cancelled"]))

    assert sum(result is not None for result in results) == 1
    snapshot = store.get_task(task["task_id"])
    assert snapshot is not None
    assert snapshot["status"] in {"succeeded", "cancelled"}
    events = store.list_events(task["task_id"])
    assert [event["seq"] for event in events] == list(range(1, len(events) + 1))
    assert [event["type"] for event in events] == ["task.created", "status.changed"]


def test_duplicate_artifact_names_do_not_overwrite(tmp_path: Path) -> None:
    store = WorkStore(tmp_path)
    task = _task(store)

    first = store.add_artifact(task["task_id"], name="report.md", kind="markdown", content="first")
    second = store.add_artifact(
        task["task_id"], name="report.md", kind="markdown", content="second"
    )

    first_path, _ = store.artifact_path(first["artifact_id"])
    second_path, _ = store.artifact_path(second["artifact_id"])
    assert first_path != second_path
    assert first_path.read_text() == "first"
    assert second_path.read_text() == "second"


def test_artifact_size_limit_cleans_partial_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(store_module, "MAX_ARTIFACT_BYTES", 4)
    store = WorkStore(tmp_path)
    task = _task(store)

    with pytest.raises(ValueError, match="100 MiB"):
        store.add_artifact(task["task_id"], name="large.txt", kind="file", content="12345")

    assert store.list_artifacts(task["task_id"]) == []
    assert list((store.artifacts_root / task["task_id"]).glob("**/*")) == []


def test_invalid_status_is_rejected(tmp_path: Path) -> None:
    store = WorkStore(tmp_path)
    task = _task(store)

    with pytest.raises(ValueError, match="Unknown Work status"):
        store.update_status(task["task_id"], "complete")


def test_terminal_status_update_is_idempotent(tmp_path: Path) -> None:
    store = WorkStore(tmp_path)
    task = _task(store)

    assert store.update_status(task["task_id"], "cancelled") is not None
    assert store.update_status(task["task_id"], "cancelled") is None
    assert [event["type"] for event in store.list_events(task["task_id"])] == [
        "task.created",
        "status.changed",
    ]
