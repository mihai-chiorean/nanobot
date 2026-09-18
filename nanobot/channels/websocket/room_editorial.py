"""Room-v1 intents and the owner review/publication surface (Ziggy-local, MIT-1010).

Split out of the snapshot's ``channels/collaborative_rooms.py`` mixin so the
channel class stays composed rather than inherited, matching how 0.3.0 factors
``WebUICommandRouter`` / ``WebUIOutboundProjector`` out of ``WebSocketChannel``.

Two halves:

* :func:`handle_room_intent` -- a guest frame carrying ``room_intent``.
  ``discussion`` and ``proposal`` are recorded and broadcast here; ``ask_ziggy``
  returns ``False`` so the normal durable-inbox path drives an agent turn.
* :func:`handle_room_editorial` -- the owner's private review loop.  A guest
  proposes a bounded connected read; the owner prepares, approves, publishes or
  declines it.  **Only a status ever reaches guests** -- the read's content
  stays in private storage until the owner explicitly publishes an excerpt.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from nanobot.channels.websocket.room_work import RoomWorkStore
from nanobot.channels.websocket.rooms import ROOM_MODE_COLLABORATIVE
from nanobot.webui.http_utils import http_error, http_json_response
from nanobot.webui.session_identity import webui_session_key


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def handle_room_intent(
    channel: Any,
    connection: Any,
    credential: Any,
    envelope: dict[str, Any],
) -> bool:
    """Handle one room frame. ``True`` when handled; ``False`` to fall through.

    Returning ``False`` for ``ask_ziggy`` is deliberate: that intent must go
    through the durable chat inbox so the agent turn is ordered and
    exactly-once, exactly like an owner message.
    """
    chat_id = credential.chat_id
    rooms = channel.rooms
    if rooms is None:
        return False
    if not rooms.is_active(chat_id):
        await channel._send_event(
            connection,
            "error",
            chat_id=chat_id,
            client_message_id=envelope.get("client_message_id"),
            detail="Room expired or revoked",
        )
        return True
    if not rooms.is_collaborative(chat_id):
        return False

    intent = envelope.get("room_intent", "discussion")
    if intent == "ask_ziggy":
        return False

    client_id = envelope.get("client_message_id")
    content = envelope.get("content")
    if not isinstance(client_id, str) or not isinstance(content, str) or not content.strip():
        await channel._send_event(
            connection,
            "error",
            chat_id=chat_id,
            client_message_id=client_id,
            detail="A message and stable identity are required",
        )
        return True
    if intent not in {"discussion", "proposal"}:
        await channel._send_event(
            connection,
            "error",
            chat_id=chat_id,
            client_message_id=client_id,
            detail="Unsupported room intent",
        )
        return True

    sessions = channel.gateway.session_manager
    session = sessions.get_or_create(webui_session_key(chat_id))
    existing = next(
        (m for m in session.messages if m.get("client_message_id") == client_id),
        None,
    )
    if existing is not None:
        # A replayed id must be byte-identical, otherwise a guest could rewrite
        # a message another participant already saw.
        same = (
            existing.get("content") == content
            and existing.get("participant_id") == credential.participant_id
            and existing.get("room_intent", "discussion") == intent
        )
        await channel._send_event(
            connection,
            "message.ack",
            chat_id=chat_id,
            client_message_id=client_id,
            status="duplicate" if same else "rejected",
            **({} if same else {"detail": "Message identity conflict"}),
        )
        return True

    if intent == "proposal":
        try:
            proposal = channel.room_work_store().propose(
                credential.room_id,
                client_id,
                credential.participant_id,
                envelope.get("proposal", {}),
            )
        except (ValueError, TypeError):
            await channel._send_event(
                connection,
                "message.ack",
                chat_id=chat_id,
                client_message_id=client_id,
                status="rejected",
                detail="Invalid connected read proposal",
            )
            return True
        await channel.broadcast_room_event(
            chat_id,
            "room.proposal",
            proposal=RoomWorkStore.public(proposal),
        )

    session.add_message(
        "user",
        content,
        client_message_id=client_id,
        participant_id=credential.participant_id,
        participant_display_name=credential.display_name,
        room_intent=intent,
    )
    try:
        sessions.save(session, fsync=True)
    except Exception:
        # Never acknowledge a message that is not durable.
        session.messages.pop()
        raise

    await channel._send_event(
        connection,
        "message.ack",
        chat_id=chat_id,
        client_message_id=client_id,
        status="accepted",
    )
    await channel.broadcast_room_event(
        chat_id,
        "participant.message",
        client_message_id=client_id,
        participant_id=credential.participant_id,
        display_name=credential.display_name,
        content=content,
        room_intent=intent,
        timestamp=_now(),
    )
    return True


async def broadcast_room_presence(channel: Any, chat_id: str) -> None:
    rooms = channel.rooms
    if rooms is None or not rooms.is_collaborative(chat_id) or not rooms.is_active(chat_id):
        return
    participants: dict[str, dict[str, str]] = {}
    for connection in channel.webui_subscribers(chat_id):
        credential = channel.effective_room_credential(connection, chat_id)
        if credential is not None:
            participants[credential.participant_id] = {
                "id": credential.participant_id,
                "display_name": credential.display_name,
            }
    await channel.broadcast_room_event(
        chat_id,
        "room.presence",
        participants=list(participants.values()),
    )


async def handle_room_editorial(
    channel: Any,
    action: str,
    *,
    room_id: str,
    chat_id: str,
    body: dict[str, Any],
) -> Any:
    """Owner-only review actions. The caller has already proved control-plane authority."""
    sessions = channel.gateway.session_manager
    session = sessions.get_or_create(webui_session_key(chat_id))

    if action == "upgrade":
        session.metadata["room_mode"] = ROOM_MODE_COLLABORATIVE
        sessions.save(session, fsync=True)
        await channel.broadcast_room_event(chat_id, "room.state", mode=ROOM_MODE_COLLABORATIVE)
        return http_json_response({"ok": True, "mode": ROOM_MODE_COLLABORATIVE})

    if not channel.rooms.is_collaborative(chat_id):
        return http_error(409, "Upgrade this room before using collaboration")

    store = channel.room_work_store()
    if action == "state":
        return http_json_response(
            {
                "mode": ROOM_MODE_COLLABORATIVE,
                "proposals": store.list(room_id, owner=True),
                "connected_reads": channel.connected_room_executor is not None,
            }
        )

    proposal_id = body.get("proposal_id", "")
    try:
        if action == "prepare":
            from nanobot.channels.websocket.room_work import action_hash, canonical_action

            with store.transaction(room_id) as data:
                proposal = next(
                    (p for p in data["proposals"] if p["id"] == proposal_id),
                    None,
                )
                if (
                    proposal is None
                    or proposal["state"] != "proposed"
                    or proposal["argument_hash"] != body.get("argument_hash")
                ):
                    raise ValueError("Proposal changed. Review again.")
                proposal["action"] = canonical_action(
                    proposal["action"]["operation"],
                    body.get("account_id"),
                    proposal["action"]["arguments"],
                )
                proposal["argument_hash"] = action_hash(proposal["action"])
                proposal["created_at"] = _now()
            return http_json_response({"ok": True})

        if action == "approve":
            if channel.connected_room_executor is None:
                return http_error(503, "Connected room reads are unavailable")
            proposal, execute = store.consume(
                room_id,
                proposal_id,
                body.get("argument_hash", ""),
            )
            if execute:
                try:
                    result = await channel.connected_room_executor(proposal["action"])
                    failed = not isinstance(result, str) or result.startswith(
                        ("Error", "(MCP tool")
                    )
                    store.finish(room_id, proposal_id, result, failed=failed)
                except (Exception, asyncio.CancelledError):
                    store.finish(room_id, proposal_id, None, failed=True)
            proposals = store.list(room_id, owner=True)
            current = next(p for p in proposals if p["id"] == proposal_id)
            # Only a status reaches guests; the content stays in private storage.
            await channel.broadcast_room_event(
                chat_id,
                "room.proposal",
                proposal=store.public(current),
            )
            return http_json_response({"proposal": current})

        if action == "publish":
            # Recheck immediately before the synchronous publication commit: a
            # revocation that landed during the owner's review must win.
            if not channel.rooms.is_active(chat_id):
                return http_error(410, "Room expired or revoked")
            proposal = store.publish(room_id, proposal_id, body.get("content"))
            publication_id = "room-publication-" + str(proposal_id)
            if not any(
                m.get("client_message_id") == publication_id for m in session.messages
            ):
                session.add_message(
                    "assistant",
                    proposal["publication"],
                    client_message_id=publication_id,
                    room_publication=True,
                )
            sessions.save(session, fsync=True)
            await channel.broadcast_room_event(
                chat_id,
                "room.proposal",
                proposal=store.public(proposal),
            )
            await channel.broadcast_room_event(
                chat_id,
                "room.publication",
                id=publication_id,
                content=proposal["publication"],
                timestamp=_now(),
            )
            return http_json_response({"ok": True})

        if action == "decline":
            with store.transaction(room_id) as data:
                for proposal in data["proposals"]:
                    if proposal["id"] == proposal_id and proposal["state"] == "proposed":
                        proposal["state"] = "declined"
            await channel.broadcast_room_event(
                chat_id,
                "room.state",
                mode=ROOM_MODE_COLLABORATIVE,
                proposals=store.list(room_id),
            )
            return http_json_response({"ok": True})
    except (ValueError, TypeError) as error:
        return http_error(409, str(error))
    return http_error(404, "Not found")


__all__ = ["broadcast_room_presence", "handle_room_editorial", "handle_room_intent"]
