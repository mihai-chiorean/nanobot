import asyncio
import threading
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


def test_list_tasks_supports_stable_offset_pages(tmp_path: Path) -> None:
    store = WorkStore(tmp_path)
    first = _task(store)
    second = _task(store)

    assert [item["task_id"] for item in store.list_tasks(limit=1, offset=0)] == [second["task_id"]]
    assert [item["task_id"] for item in store.list_tasks(limit=1, offset=1)] == [first["task_id"]]


def test_task_persists_reasoning_profile(tmp_path: Path) -> None:
    store = WorkStore(tmp_path)

    task = store.create_task(
        chat_id="chat-1",
        content="Implement the feature",
        reasoning_profile="think-code",
    )

    assert task["reasoning_profile"] == "think-code"
    assert store.get_task(task["task_id"])["reasoning_profile"] == "think-code"


def test_create_and_message_command_ids_are_idempotent(tmp_path: Path) -> None:
    store = WorkStore(tmp_path)
    request_id = "work_" + "a" * 32
    first = store.create_task(chat_id="chat-1", content="Prepare a report", request_id=request_id)
    assert first.pop("_was_created") is True
    assert first.pop("_was_dispatched") is False
    store.mark_dispatched(first["task_id"], request_id)

    duplicate = store.create_task(
        chat_id="chat-1", content="Prepare a report", request_id=request_id
    )
    assert duplicate.pop("_was_created") is False
    assert duplicate.pop("_was_dispatched") is True
    assert duplicate["task_id"] == first["task_id"]
    assert len(store.list_tasks()) == 1

    command_id = "cmd_" + "b" * 32
    assert store.reserve_command(command_id, first["task_id"], "message") == (
        True,
        False,
    )
    store.mark_command_dispatched(command_id)
    assert store.reserve_command(command_id, first["task_id"], "message") == (
        False,
        True,
    )


def test_concurrent_duplicate_request_ids_insert_one_task(tmp_path: Path) -> None:
    store = WorkStore(tmp_path)
    request_id = "work_" + "c" * 32
    barrier = Barrier(8)

    def create(_: int) -> dict:
        barrier.wait()
        return store.create_task(
            chat_id="chat-1",
            content="Prepare one report",
            request_id=request_id,
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(create, range(8)))

    assert sum(result.pop("_was_created") for result in results) == 1
    assert all(not result.pop("_was_dispatched") for result in results)
    assert len({result["task_id"] for result in results}) == 1
    assert len(store.list_tasks()) == 1
    assert [event["type"] for event in store.list_events(results[0]["task_id"])] == ["task.created"]


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


def test_event_retention_and_page_size_are_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(store_module, "MAX_EVENTS_PER_TASK", 3)
    store = WorkStore(tmp_path)
    task = _task(store)
    for index in range(5):
        store.append_event(task["task_id"], "progress", {"index": index})

    events = store.list_events(task["task_id"], limit=2)
    assert [event["seq"] for event in events] == [4, 5]
    assert [event["seq"] for event in store.list_events(task["task_id"])] == [4, 5, 6]


def test_artifact_count_and_tenant_storage_are_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(store_module, "MAX_ARTIFACTS_PER_TASK", 1)
    monkeypatch.setattr(store_module, "MAX_TENANT_ARTIFACT_BYTES", 5)
    store = WorkStore(tmp_path)
    first_task = _task(store)
    second_task = _task(store)
    store.add_artifact(first_task["task_id"], name="one.txt", kind="file", content="123")

    with pytest.raises(ValueError, match="count limit"):
        store.add_artifact(first_task["task_id"], name="two.txt", kind="file", content="1")
    with pytest.raises(ValueError, match="Tenant Work artifact storage"):
        store.add_artifact(second_task["task_id"], name="three.txt", kind="file", content="456")


def test_run_io_executes_store_work_off_event_loop(tmp_path: Path) -> None:
    store = WorkStore(tmp_path)
    event_loop_thread = threading.get_ident()

    async def run() -> int:
        return await store.run_io(threading.get_ident)

    assert asyncio.run(run()) != event_loop_thread
