"""Authenticated REST and WebSocket contracts for durable Work tasks."""

from __future__ import annotations

import asyncio
import base64
import functools
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import websockets

from nanobot.channels.websocket import WebSocketChannel
from nanobot.session.manager import SessionManager


def _channel(bus: Any, tmp_path: Path, port: int) -> WebSocketChannel:
    return WebSocketChannel(
        {
            "enabled": True,
            "allowFrom": ["*"],
            "host": "127.0.0.1",
            "port": port,
            "path": "/",
            "websocketRequiresToken": True,
        },
        bus,
        session_manager=SessionManager(tmp_path),
    )


async def _request(method: str, url: str, **kwargs: Any) -> httpx.Response:
    return await asyncio.to_thread(
        functools.partial(httpx.request, method, url, timeout=5.0, **kwargs)
    )


async def _token(port: int) -> str:
    response = await _request("GET", f"http://127.0.0.1:{port}/webui/bootstrap")
    assert response.status_code == 200
    return str(response.json()["token"])


@pytest.fixture()
def bus() -> MagicMock:
    value = MagicMock()
    value.publish_inbound = AsyncMock()
    return value


def test_work_media_accepts_typed_document(tmp_path: Path) -> None:
    payload = base64.b64encode(b"%PDF-1.4\n%%EOF").decode()
    media = [{
        "data_url": f"data:application/pdf;base64,{payload}",
        "name": "work-report.pdf",
    }]

    with patch("nanobot.channels.websocket.get_media_dir", return_value=tmp_path):
        paths, reason = WebSocketChannel._work_media(media)

    assert reason is None
    assert len(paths) == 1
    assert Path(paths[0]).name.endswith("-work-report.pdf")


@pytest.mark.asyncio
async def test_work_rest_routes_and_isolated_inbounds(bus: MagicMock, tmp_path: Path) -> None:
    port = 29931
    channel = _channel(bus, tmp_path, port)
    server = asyncio.create_task(channel.start())
    await asyncio.sleep(0.2)
    try:
        denied = await _request("GET", f"http://127.0.0.1:{port}/api/work")
        assert denied.status_code == 401
        token = await _token(port)
        headers = {"Authorization": f"Bearer {token}"}

        created = await _request(
            "POST",
            f"http://127.0.0.1:{port}/api/work",
            headers=headers,
            json={"chat_id": "visible-chat", "content": "Prepare a report"},
        )
        assert created.status_code == 201
        assert set(created.json()) == {"task"}
        task = created.json()["task"]
        task_id = task["task_id"]
        assert task["session_key"] == f"work:{task_id}"
        inbound = bus.publish_inbound.await_args.args[0]
        assert inbound.chat_id == "visible-chat"
        assert inbound.session_key_override == f"work:{task_id}"

        listing = await _request("GET", f"http://127.0.0.1:{port}/api/work", headers=headers)
        assert listing.status_code == 200
        assert set(listing.json()) == {
            "tasks",
            "has_more",
            "next_offset",
            "next_task_id",
        }
        assert listing.json()["has_more"] is False
        assert listing.json()["next_offset"] == 1
        assert listing.json()["tasks"][0]["task_id"] == task_id
        detail = await _request(
            "GET", f"http://127.0.0.1:{port}/api/work/{task_id}", headers=headers
        )
        assert detail.status_code == 200
        assert set(detail.json()) == {"task"}
        events = await _request(
            "GET",
            f"http://127.0.0.1:{port}/api/work/{task_id}/events?after=0",
            headers=headers,
        )
        assert events.status_code == 200
        assert events.json()["events"][0]["type"] == "task.created"

        assert channel._work_store is not None
        channel._work_store.update_status(task_id, "waiting")
        bus.publish_inbound.reset_mock()
        follow_up = await _request(
            "POST",
            f"http://127.0.0.1:{port}/api/work/{task_id}/message",
            headers=headers,
            json={"content": "Use the latest numbers"},
        )
        assert follow_up.status_code == 202
        assert follow_up.json() == {"accepted": True, "task_id": task_id}
        follow_up_inbound = bus.publish_inbound.await_args.args[0]
        assert follow_up_inbound.session_key_override == f"work:{task_id}"
        assert follow_up_inbound.content == "Use the latest numbers"

        channel._work_store.update_status(task_id, "succeeded")
        bus.publish_inbound.reset_mock()
        rejected = await _request(
            "POST",
            f"http://127.0.0.1:{port}/api/work/{task_id}/message",
            headers=headers,
            json={"content": "Run again"},
        )
        assert rejected.status_code == 409
        bus.publish_inbound.assert_not_awaited()
    finally:
        await channel.stop()
        await server


@pytest.mark.asyncio
async def test_work_websocket_idempotency_keys_reuse_task_and_message(
    bus: MagicMock, tmp_path: Path
) -> None:
    port = 29936
    channel = _channel(bus, tmp_path, port)
    server = asyncio.create_task(channel.start())
    await asyncio.sleep(0.2)
    try:
        token = await _token(port)
        async with websockets.connect(f"ws://127.0.0.1:{port}/?token={token}") as client:
            assert json.loads(await client.recv())["event"] == "ready"
            create = {
                "type": "work.create",
                "chat_id": "idempotent-chat",
                "content": "One execution",
                "idempotency_key": "work_" + "a" * 32,
            }
            await client.send(json.dumps(create))
            first = json.loads(await client.recv())
            await client.send(json.dumps(create))
            duplicate = json.loads(await client.recv())
            replay = json.loads(await client.recv())

            assert duplicate["task_id"] == first["task_id"]
            assert replay["type"] == "task.created"
            assert bus.publish_inbound.await_count == 1

            message = {
                "type": "work.message",
                "task_id": first["task_id"],
                "content": "One follow-up",
                "idempotency_key": "cmd_" + "b" * 32,
            }
            await client.send(json.dumps(message))
            assert json.loads(await client.recv())["type"] == "message.received"
            await client.send(json.dumps(message))
            await client.send(
                json.dumps(
                    {
                        "type": "work.subscribe",
                        "task_id": first["task_id"],
                        "after_seq": 1,
                    }
                )
            )
            assert json.loads(await client.recv())["event"] == "work.subscribed"
            assert json.loads(await client.recv())["type"] == "message.received"
            assert bus.publish_inbound.await_count == 2
    finally:
        await channel.stop()
        await server


@pytest.mark.asyncio
async def test_work_message_idempotency_retries_after_publish_failure(
    bus: MagicMock, tmp_path: Path
) -> None:
    port = 29937
    channel = _channel(bus, tmp_path, port)
    server = asyncio.create_task(channel.start())
    await asyncio.sleep(0.2)
    try:
        token = await _token(port)
        async with websockets.connect(f"ws://127.0.0.1:{port}/?token={token}") as client:
            assert json.loads(await client.recv())["event"] == "ready"
            await client.send(
                json.dumps(
                    {
                        "type": "work.create",
                        "chat_id": "retry-chat",
                        "content": "Initial task",
                    }
                )
            )
            created = json.loads(await client.recv())
            task_id = created["task_id"]
            bus.publish_inbound.reset_mock()
            bus.publish_inbound.side_effect = RuntimeError("queue unavailable")
            message = {
                "type": "work.message",
                "task_id": task_id,
                "content": "Retry this follow-up",
                "idempotency_key": "cmd_" + "d" * 32,
            }

            await client.send(json.dumps(message))
            failed = json.loads(await client.recv())
            assert failed == {"event": "error", "detail": "failed to enqueue work message"}
            assert bus.publish_inbound.await_count == 1

            bus.publish_inbound.side_effect = None
            await client.send(json.dumps(message))
            retried = json.loads(await client.recv())
            assert retried["type"] == "message.received"
            assert bus.publish_inbound.await_count == 2

            await client.send(json.dumps(message))
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(client.recv(), timeout=0.2)
            assert bus.publish_inbound.await_count == 2
    finally:
        await channel.stop()
        await server


@pytest.mark.asyncio
async def test_work_cancel_signals_agent_and_enqueue_failure_is_terminal(
    bus: MagicMock, tmp_path: Path
) -> None:
    port = 29932
    channel = _channel(bus, tmp_path, port)
    server = asyncio.create_task(channel.start())
    await asyncio.sleep(0.2)
    try:
        token = await _token(port)
        headers = {"Authorization": f"Bearer {token}"}
        created = await _request(
            "POST",
            f"http://127.0.0.1:{port}/api/work",
            headers=headers,
            json={"chat_id": "cancel-chat", "content": "Long task"},
        )
        task = created.json()["task"]
        bus.publish_inbound.reset_mock()

        cancelled = await _request(
            "POST",
            f"http://127.0.0.1:{port}/api/work/{task['task_id']}/cancel",
            headers=headers,
        )

        assert cancelled.status_code == 200
        assert set(cancelled.json()) == {"task"}
        assert cancelled.json()["task"]["status"] == "cancelled"
        stop = bus.publish_inbound.await_args.args[0]
        assert stop.content == "/stop"
        assert stop.chat_id == "cancel-chat"
        assert stop.session_key_override == task["session_key"]

        bus.publish_inbound.side_effect = RuntimeError("queue unavailable")
        failed_create = await _request(
            "POST",
            f"http://127.0.0.1:{port}/api/work",
            headers=headers,
            json={"chat_id": "failed-chat", "content": "Cannot enqueue"},
        )
        assert failed_create.status_code == 503
        assert channel._work_store is not None
        failed = next(
            item for item in channel._work_store.list_tasks() if item["chat_id"] == "failed-chat"
        )
        assert failed["status"] == "failed"
        assert failed["error"] == "Failed to enqueue Work task."
    finally:
        await channel.stop()
        await server


@pytest.mark.asyncio
async def test_cancel_race_accepts_agent_cancel_and_publish_failure_keeps_state(
    bus: MagicMock, tmp_path: Path
) -> None:
    channel = _channel(bus, tmp_path, 29933)
    assert channel._work_store is not None
    task = channel._work_store.create_task(chat_id="race-chat", content="Race")
    original_update = channel._work_store.update_status

    def agent_wins(task_id: str, status: str, **kwargs: Any):
        original_update(task_id, status, **kwargs)
        return None

    channel._work_store.update_status = agent_wins
    assert await channel._cancel_work_task(task, sender_id="rest") is None
    assert channel._work_store.get_task(task["task_id"])["status"] == "cancelled"

    second = channel._work_store.create_task(chat_id="race-chat", content="No queue")
    bus.publish_inbound.side_effect = RuntimeError("queue unavailable")
    assert await channel._cancel_work_task(second, sender_id="rest") == "publish_failed"
    assert channel._work_store.get_task(second["task_id"])["status"] == "queued"


@pytest.mark.asyncio
async def test_work_artifact_auth_streaming_and_unknown_ids(bus: MagicMock, tmp_path: Path) -> None:
    port = 29934
    channel = _channel(bus, tmp_path, port)
    assert channel._work_store is not None
    task = channel._work_store.create_task(chat_id="artifact-chat", content="Artifact")
    artifact = channel._work_store.add_artifact(
        task["task_id"], name="report.txt", kind="file", content=b"streamed report"
    )
    server = asyncio.create_task(channel.start())
    await asyncio.sleep(0.2)
    try:
        url = f"http://127.0.0.1:{port}/api/work/artifacts/{artifact['artifact_id']}"
        assert (await _request("GET", url)).status_code == 401
        headers = {"Authorization": f"Bearer {await _token(port)}"}
        downloaded = await _request("GET", url, headers=headers)
        assert downloaded.status_code == 200
        assert downloaded.content == b"streamed report"
        assert downloaded.headers["content-disposition"] == ('attachment; filename="report.txt"')

        unknown_task = "work_" + "0" * 32
        unknown_artifact = "artifact_" + "0" * 32
        assert (
            await _request(
                "GET", f"http://127.0.0.1:{port}/api/work/{unknown_task}", headers=headers
            )
        ).status_code == 404
        assert (
            await _request(
                "GET",
                f"http://127.0.0.1:{port}/api/work/artifacts/{unknown_artifact}",
                headers=headers,
            )
        ).status_code == 404
        traversal = await _request(
            "GET",
            f"http://127.0.0.1:{port}/api/work/artifacts/%252E%252E",
            headers=headers,
        )
        assert traversal.status_code == 400
    finally:
        await channel.stop()
        await server


@pytest.mark.asyncio
async def test_work_websocket_envelopes_and_stop_signal(bus: MagicMock, tmp_path: Path) -> None:
    port = 29935
    channel = _channel(bus, tmp_path, port)
    server = asyncio.create_task(channel.start())
    await asyncio.sleep(0.2)
    try:
        token = await _token(port)
        async with websockets.connect(f"ws://127.0.0.1:{port}/?token={token}") as client:
            assert json.loads(await client.recv())["event"] == "ready"
            await client.send(
                json.dumps(
                    {
                        "type": "work.create",
                        "chat_id": "ws-work-chat",
                        "content": "Background analysis",
                    }
                )
            )
            created = json.loads(await client.recv())
            assert created["event"] == "work.created"
            task_id = created["task_id"]
            session_key = f"work:{task_id}"
            assert created["task"]["session_key"] == session_key
            assert bus.publish_inbound.await_args.args[0].session_key_override == session_key

            await client.send(json.dumps({"type": "work.subscribe", "task_id": task_id}))
            assert json.loads(await client.recv()) == {
                "event": "work.subscribed",
                "task_id": task_id,
            }
            replay = json.loads(await client.recv())
            assert replay["event"] == "work.event"
            assert replay["type"] == "task.created"

            bus.publish_inbound.reset_mock()
            await client.send(
                json.dumps({"type": "work.message", "task_id": task_id, "content": "More detail"})
            )
            message_event = json.loads(await client.recv())
            assert message_event["type"] == "message.received"
            assert bus.publish_inbound.await_args.args[0].session_key_override == session_key

            bus.publish_inbound.reset_mock()
            await client.send(json.dumps({"type": "work.cancel", "task_id": task_id}))
            cancel_event = json.loads(await client.recv())
            assert cancel_event["type"] == "status.changed"
            assert cancel_event["payload"]["status"] == "cancelled"
            stop = bus.publish_inbound.await_args.args[0]
            assert stop.content == "/stop"
            assert stop.session_key_override == session_key

            await client.send(
                json.dumps({"type": "work.message", "task_id": task_id, "content": "Too late"})
            )
            terminal = json.loads(await client.recv())
            assert terminal["event"] == "error"
            assert terminal["detail"] == "task does not accept messages"
    finally:
        await channel.stop()
        await server
