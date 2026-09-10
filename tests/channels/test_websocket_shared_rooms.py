"""Shared-room authorization and participant fan-out tests."""

import asyncio
import functools
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import websockets

from nanobot.agent.context import ContextBuilder
from nanobot.channels.websocket import WebSocketChannel
from nanobot.session.manager import Session, SessionManager


async def _request(method: str, url: str, **kwargs: object) -> httpx.Response:
    return await asyncio.to_thread(
        functools.partial(
            httpx.request,
            method,
            url,
            timeout=5.0,
            **kwargs,
        )
    )


@pytest.mark.asyncio
async def test_room_token_is_scoped_and_messages_fan_out(tmp_path: Path) -> None:
    bus = MagicMock()
    bus.publish_inbound = AsyncMock()
    sessions = SessionManager(tmp_path)
    source = Session(key="websocket:source")
    source.add_message("user", "Existing shared context")
    source.add_message(
        "assistant",
        "",
        tool_calls=[{
            "id": "private-tool-call",
            "function": {"name": "exec", "arguments": "{\"command\":\"cat /private/key\"}"},
        }],
    )
    source.add_message(
        "tool",
        "PRIVATE TOOL RESULT",
        tool_call_id="private-tool-call",
        media=["/home/mihai/private.pdf"],
    )
    source.add_message("assistant", "Existing answer", reasoning_content="PRIVATE REASONING")
    published_id = sessions.store_published_snapshot("shared-report.md", b"# Shared report\n")
    published_url = sessions.published_file_url(source.key, published_id)
    source.add_message("assistant", f"Visible report: [shared-report.md]({published_url})")
    sessions.grant_published_files(
        source,
        {published_id: "shared-report.md"},
        message_start=len(source.messages) - 1,
    )
    sessions.save(source)
    channel = WebSocketChannel(
        {
            "enabled": True,
            "allowFrom": ["*"],
            "host": "127.0.0.1",
            "port": 29924,
            "path": "/",
            "tokenIssuePath": "/auth/token",
            "tokenIssueSecret": "runtime-bootstrap-secret-long-enough",
            "websocketRequiresToken": True,
        },
        bus,
        session_manager=sessions,
    )
    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.2)
    authorization = {
        "Authorization": "Bearer runtime-bootstrap-secret-long-enough"
    }
    room_id = "room_" + ("a" * 32)
    chat_id = "room_" + ("b" * 32)
    participant_id = "participant_" + ("c" * 32)
    try:
        created = await _request(
            "POST",
            "http://127.0.0.1:29924/auth/shared-rooms",
            headers=authorization,
            json={
                "room_id": room_id,
                "chat_id": chat_id,
                "source_session_key": "websocket:source",
                "title": "Shared test",
                "owner_display_name": "Owner",
            },
        )
        assert created.status_code == 201

        # A delayed mirror response must never roll a later room rename back.
        newest = await _request(
            "POST",
            "http://127.0.0.1:29924/auth/shared-rooms/title",
            headers=authorization,
            json={
                "room_id": room_id,
                "chat_id": chat_id,
                "title": "Newest room title",
                "title_revision": 2,
            },
        )
        assert newest.status_code == 200
        delayed = await _request(
            "POST",
            "http://127.0.0.1:29924/auth/shared-rooms/title",
            headers=authorization,
            json={
                "room_id": room_id,
                "chat_id": chat_id,
                "title": "Old room title",
                "title_revision": 1,
            },
        )
        assert delayed.status_code == 409
        room_payload = sessions.read_session_file(f"websocket:{chat_id}")
        assert room_payload is not None
        assert room_payload["metadata"]["title"] == "Newest room title"
        assert room_payload["metadata"]["title_user_defined"] is True
        assert room_payload["metadata"]["shared_room_title_revision"] == 2

        issued = await _request(
            "POST",
            "http://127.0.0.1:29924/auth/shared-room-token",
            headers=authorization,
            json={
                "room_id": room_id,
                "chat_id": chat_id,
                "participant_id": participant_id,
                "display_name": "Roxana",
                "role": "contributor",
            },
        )
        assert issued.status_code == 200
        token = issued.json()["token"]
        room_auth = {"Authorization": f"Bearer {token}"}

        listing = await _request(
            "GET",
            "http://127.0.0.1:29924/api/sessions",
            headers=room_auth,
        )
        assert listing.status_code == 401
        foreign = await _request(
            "GET",
            "http://127.0.0.1:29924/api/sessions/websocket:source/messages",
            headers=room_auth,
        )
        assert foreign.status_code == 401
        exact = await _request(
            "GET",
            f"http://127.0.0.1:29924/api/sessions/websocket:{chat_id}/messages",
            headers=room_auth,
        )
        assert exact.status_code == 200
        assert exact.json()["metadata"]["shared_room"] is True
        serialized_history = json.dumps(exact.json())
        assert "PRIVATE TOOL RESULT" not in serialized_history
        assert "PRIVATE REASONING" not in serialized_history
        assert "/private/key" not in serialized_history
        assert "/home/mihai/private.pdf" not in serialized_history
        existing_user = next(
            message
            for message in exact.json()["messages"]
            if message["content"] == "Existing shared context"
        )
        assert existing_user["participant_id"] == "owner"
        assert existing_user["participant_display_name"] == "Owner"
        room_file_url = sessions.published_file_url(
            f"websocket:{chat_id}", published_id
        )
        room_file = await _request(
            "GET",
            f"http://127.0.0.1:29924{room_file_url}",
            headers=room_auth,
        )
        assert room_file.status_code == 200
        assert room_file.content == b"# Shared report\n"
        source_file = await _request(
            "GET",
            f"http://127.0.0.1:29924{published_url}",
            headers=room_auth,
        )
        assert source_file.status_code == 404

        async with websockets.connect(
            f"ws://127.0.0.1:29924/?token={token}"
        ) as socket:
            ready = json.loads(await socket.recv())
            assert ready["chat_id"] == chat_id

            await socket.send(json.dumps({"type": "attach", "chat_id": "source"}))
            denied = json.loads(await socket.recv())
            assert denied == {"event": "error", "detail": "room scope violation"}

            message_id = "9a50d3cf-42c4-4ba7-93da-84f9b00ad20c"
            await socket.send(
                json.dumps(
                    {
                        "type": "message",
                        "chat_id": chat_id,
                        "client_message_id": message_id,
                        "content": "What should we do next?",
                    }
                )
            )
            acknowledged = json.loads(await socket.recv())
            participant = json.loads(await socket.recv())
            assert acknowledged["event"] == "message.ack"
            assert acknowledged["status"] == "accepted"
            assert participant == {
                "event": "participant.message",
                "chat_id": chat_id,
                "client_message_id": message_id,
                "participant_id": participant_id,
                "display_name": "Roxana",
                "content": "What should we do next?",
                "timestamp": participant["timestamp"],
            }
            inbound = bus.publish_inbound.await_args.args[0]
            assert inbound.sender_id == participant_id
            assert inbound.metadata["shared_room"] is True
            assert inbound.metadata["participant_display_name"] == "Roxana"

            revoked = await _request(
                "POST",
                "http://127.0.0.1:29924/auth/shared-room-revoke",
                headers=authorization,
                json={"room_id": room_id, "chat_id": chat_id},
            )
            assert revoked.status_code == 200
            assert revoked.json()["closed_connections"] == 1
            denied_file = await _request(
                "GET",
                f"http://127.0.0.1:29924{room_file_url}",
                headers=room_auth,
            )
            assert denied_file.status_code == 404
            with pytest.raises(websockets.exceptions.ConnectionClosed):
                await socket.recv()

        expired = await _request(
            "GET",
            f"http://127.0.0.1:29924/api/sessions/websocket:{chat_id}/messages",
            headers=room_auth,
        )
        assert expired.status_code == 401
    finally:
        await channel.stop()
        await server_task


def test_shared_room_prompt_excludes_private_workspace_context(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("PRIVATE BOOTSTRAP", encoding="utf-8")
    memory = tmp_path / "memory"
    memory.mkdir()
    (memory / "MEMORY.md").write_text("PRIVATE MEMORY", encoding="utf-8")
    context = ContextBuilder(tmp_path)

    messages = context.build_messages(
        history=[],
        current_message="Review this plan",
        channel="websocket",
        chat_id="room",
        sender_id="participant",
        shared_room=True,
        participant_display_name="Roxana",
    )

    system = messages[0]["content"]
    assert "shared conversation" in system
    assert "PRIVATE BOOTSTRAP" not in system
    assert "PRIVATE MEMORY" not in system
    assert "Roxana: Review this plan" in messages[1]["content"]
