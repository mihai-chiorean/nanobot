"""End-to-end room intents with two guest sockets and an owner review surface."""

import asyncio
import json
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import websockets

from nanobot.channels.websocket import WebSocketChannel
from nanobot.session.manager import Session, SessionManager
from tests.channels.test_websocket_shared_rooms import _request


async def event(socket, name):
    async with asyncio.timeout(5):
        while True:
            value = json.loads(await socket.recv())
            if value.get("event") == name:
                return value


@pytest.mark.asyncio
async def test_two_guests_discussion_approval_private_review_publication_and_revoke(tmp_path: Path):
    sessions = SessionManager(tmp_path)
    source = Session(key="websocket:source")
    source.metadata["private_summary"] = "PRIVATE OWNER MEMORY"
    source.add_message("user", "Share this context")
    source.add_message("assistant", "Visible answer")
    sessions.save(source)
    bus = MagicMock()
    bus.publish_inbound = AsyncMock()
    channel = WebSocketChannel(
        {
            "enabled": True,
            "allowFrom": ["*"],
            "host": "127.0.0.1",
            "port": 29931,
            "path": "/",
            "tokenIssueSecret": "room-test-bootstrap-secret",
            "websocketRequiresToken": True,
            "sharedRoomCollaborationEnabled": True,
        },
        bus,
        session_manager=sessions,
    )
    channel.connected_room_executor = AsyncMock(
        return_value="Private email result: telescope meeting"
    )
    task = asyncio.create_task(channel.start())
    base = "http://127.0.0.1:29931"
    headers = {"Authorization": "Bearer room-test-bootstrap-secret"}
    try:
        for _ in range(100):
            if channel._server_task is not None:
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        preview = await _request(
            "POST",
            base + "/auth/shared-rooms/preview",
            headers=headers,
            json={"source_session_key": source.key},
        )
        assert preview.status_code == 200, preview.text
        boundary = preview.json()
        source.add_message("user", "LATER PRIVATE MESSAGE")
        sessions.save(source)
        room_id = "room_" + "a" * 32
        chat_id = "room_" + "b" * 32
        room = {"room_id": room_id, "chat_id": chat_id}
        created = await _request(
            "POST",
            base + "/auth/shared-rooms",
            headers=headers,
            json={
                **room,
                "source_session_key": source.key,
                "mode": "collaborative-v1",
                "snapshot_message_count": boundary["message_count"],
                "snapshot_sha256": boundary["snapshot_sha256"],
            },
        )
        assert created.status_code == 201, created.text
        credentials = []
        for digit in ("1", "2"):
            response = await _request(
                "POST",
                base + "/auth/shared-room-token",
                headers=headers,
                json={
                    **room,
                    "participant_id": "participant_" + digit * 32,
                    "display_name": "Guest " + digit,
                },
            )
            assert response.status_code == 200, response.text
            credentials.append(response.json())
        history = await _request(
            "GET",
            base + f"/api/sessions/websocket:{chat_id}/messages",
            headers={"Authorization": "Bearer " + credentials[0]["token"]},
        )
        assert "LATER PRIVATE MESSAGE" not in history.text
        assert "PRIVATE OWNER MEMORY" not in history.text
        assert credentials[0]["mode"] == "collaborative-v1"
        async with (
            websockets.connect(f"ws://127.0.0.1:29931/?token={credentials[0]['token']}") as first,
            websockets.connect(f"ws://127.0.0.1:29931/?token={credentials[1]['token']}") as second,
        ):
            for socket in (first, second):
                await socket.send(json.dumps({"type": "attach", "chat_id": chat_id}))
                await event(socket, "attached")
            presence = await event(second, "room.presence")
            assert {p["display_name"] for p in presence["participants"]} == {"Guest 1", "Guest 2"}
            discussion = {
                "type": "message",
                "chat_id": chat_id,
                "client_message_id": str(uuid.uuid4()),
                "content": "Hello everyone",
                "room_intent": "discussion",
            }
            await first.send(json.dumps(discussion))
            await event(first, "message.ack")
            assert (await event(second, "participant.message"))["content"] == "Hello everyone"
            bus.publish_inbound.assert_not_awaited()
            await first.send(json.dumps(discussion))
            assert (await event(first, "message.ack"))["status"] == "duplicate"
            ask = {
                **discussion,
                "client_message_id": str(uuid.uuid4()),
                "room_intent": "ask_ziggy",
                "content": "Summarize the shared context",
            }
            await second.send(json.dumps(ask))
            await event(second, "message.ack")
            assert bus.publish_inbound.await_count == 1
            proposal = {
                **discussion,
                "client_message_id": str(uuid.uuid4()),
                "room_intent": "proposal",
                "content": "subject:telescope",
                "proposal": {
                    "operation": "gmail_search",
                    "account_id": "owner-selected",
                    "arguments": {"query": "subject:telescope", "max_results": 10},
                },
            }
            await second.send(json.dumps(proposal))
            await event(second, "message.ack")
            state = (
                await _request(
                    "POST", base + "/auth/shared-rooms/state", headers=headers, json=room
                )
            ).json()
            p = state["proposals"][0]
            denied = await _request(
                "POST",
                base + "/auth/shared-rooms/state",
                headers={"Authorization": "Bearer " + credentials[0]["token"]},
                json=room,
            )
            assert denied.status_code == 401
            prepared = await _request(
                "POST",
                base + "/auth/shared-rooms/prepare",
                headers=headers,
                json={
                    **room,
                    "proposal_id": p["id"],
                    "argument_hash": p["argument_hash"],
                    "account_id": "account-owner",
                },
            )
            assert prepared.status_code == 200, prepared.text
            state = (
                await _request(
                    "POST", base + "/auth/shared-rooms/state", headers=headers, json=room
                )
            ).json()
            p = state["proposals"][0]
            request = {**room, "proposal_id": p["id"], "argument_hash": p["argument_hash"]}
            responses = await asyncio.gather(
                *[
                    _request(
                        "POST", base + "/auth/shared-rooms/approve", headers=headers, json=request
                    )
                    for _ in range(3)
                ]
            )
            assert all(r.status_code == 200 for r in responses)
            channel.connected_room_executor.assert_awaited_once()
            public = channel._room_work_store().list(room_id)
            assert "private_result" not in json.dumps(public) and "account-owner" not in json.dumps(
                public
            )
            history = await _request(
                "GET",
                base + f"/api/sessions/websocket:{chat_id}/messages",
                headers={"Authorization": "Bearer " + credentials[0]["token"]},
            )
            assert "Private email result" not in history.text
            published = await _request(
                "POST",
                base + "/auth/shared-rooms/publish",
                headers=headers,
                json={**request, "content": "Meeting about telescopes"},
            )
            assert published.status_code == 200, published.text
            while (await event(first, "room.proposal"))["proposal"]["state"] != "published":
                pass
            assert (await event(first, "room.publication"))["content"] == "Meeting about telescopes"
            replay = await _request(
                "POST",
                base + "/auth/shared-rooms/publish",
                headers=headers,
                json={**request, "content": "Meeting about telescopes"},
            )
            assert replay.status_code == 200
            stored = sessions.read_session_file(f"websocket:{chat_id}")
            assert sum(m.get("room_publication") is True for m in stored["messages"]) == 1
            revoked = await _request(
                "POST", base + "/auth/shared-room-revoke", headers=headers, json=room
            )
            assert revoked.status_code == 200
            after = await _request(
                "POST",
                base + "/auth/shared-rooms/publish",
                headers=headers,
                json={**request, "content": "Meeting about telescopes"},
            )
            assert after.status_code == 410
            access = await _request(
                "GET",
                base + f"/api/sessions/websocket:{chat_id}/messages",
                headers={"Authorization": "Bearer " + credentials[0]["token"]},
            )
            assert access.status_code in (401, 404)
            token = await _request(
                "POST",
                base + "/auth/shared-room-token",
                headers=headers,
                json={
                    **room,
                    "participant_id": "participant_" + "1" * 32,
                    "display_name": "Guest 1",
                },
            )
            assert token.status_code == 410
    finally:
        await channel.stop()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
