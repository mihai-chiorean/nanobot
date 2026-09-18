import asyncio
from contextlib import suppress
from pathlib import Path

import pytest
from watchfiles import Change

import nanobot.config.watcher as config_watcher


@pytest.mark.asyncio
async def test_watch_config_file_filters_directory_events(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "config.json"
    other_path = tmp_path / "other.json"
    seen: dict[str, object] = {}

    async def fake_awatch(*paths, **kwargs):
        seen["paths"] = paths
        seen["recursive"] = kwargs["recursive"]
        watch_filter = kwargs["watch_filter"]
        assert watch_filter(Change.modified, str(config_path)) is True
        assert watch_filter(Change.modified, str(other_path)) is False
        yield {(Change.modified, str(config_path))}

    monkeypatch.setattr(config_watcher, "awatch", fake_awatch)
    changes: list[None] = []

    await config_watcher.watch_config_file(config_path, lambda: changes.append(None))

    assert seen == {"paths": (tmp_path,), "recursive": False}
    assert changes == [None]


@pytest.mark.asyncio
async def test_watch_config_file_awaits_async_callbacks(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "config.json"
    events: list[str] = []

    async def fake_awatch(*_paths, **_kwargs):
        yield {(Change.modified, str(config_path))}
        events.append("second-batch")
        yield {(Change.modified, str(config_path))}

    async def on_change() -> None:
        events.append("start")
        await asyncio.sleep(0)
        events.append("done")

    monkeypatch.setattr(config_watcher, "awatch", fake_awatch)

    await config_watcher.watch_config_file(config_path, on_change)

    # The awaitable is awaited, and fully, before the next batch is consumed.
    assert events == ["start", "done", "second-batch", "start", "done"]


@pytest.mark.asyncio
async def test_watch_config_file_logs_handler_errors_and_keeps_watching(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "config.json"
    calls = 0
    records: list = []

    async def fake_awatch(*_paths, **_kwargs):
        yield {(Change.modified, str(config_path))}
        yield {(Change.modified, str(config_path))}

    async def on_change() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("handler exploded")

    monkeypatch.setattr(config_watcher, "awatch", fake_awatch)
    sink = config_watcher.logger.add(
        lambda message: records.append(message.record),
        level="DEBUG",
        filter=lambda record: record["name"] == config_watcher.__name__,
    )
    try:
        await config_watcher.watch_config_file(config_path, on_change)
    finally:
        config_watcher.logger.remove(sink)

    assert calls == 2
    assert [r["level"].name for r in records] == ["ERROR"]
    assert "handler exploded" in str(records[0]["exception"])


@pytest.mark.asyncio
async def test_watch_config_file_propagates_cancellation_from_handler(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "config.json"

    async def fake_awatch(*_paths, **_kwargs):
        yield {(Change.modified, str(config_path))}
        raise AssertionError("watch must stop once cancelled")

    async def on_change() -> None:
        raise asyncio.CancelledError()

    monkeypatch.setattr(config_watcher, "awatch", fake_awatch)

    with pytest.raises(asyncio.CancelledError):
        await config_watcher.watch_config_file(config_path, on_change)


@pytest.mark.asyncio
async def test_watch_config_file_observes_atomic_replace(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text("{}", encoding="utf-8")
    changed = asyncio.Event()
    task = asyncio.create_task(
        config_watcher.watch_config_file(config_path, changed.set)
    )

    try:
        for attempt in range(10):
            replacement = tmp_path / "config.tmp"
            replacement.write_text(f'{{"attempt": {attempt}}}', encoding="utf-8")
            replacement.replace(config_path)
            try:
                await asyncio.wait_for(changed.wait(), timeout=0.2)
                break
            except TimeoutError:
                continue
        assert changed.is_set()
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
