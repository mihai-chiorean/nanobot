"""Additive room-v1 intents and owner-only review/publication surfaces."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from nanobot.channels.room_work import RoomWorkStore


class CollaborativeRooms:
    def _room_is_active(self, chat_id: str) -> bool:
        if self._session_manager is None:
            return True
        session = self._session_manager.get_or_create(f"websocket:{chat_id}")
        if session.metadata.get("shared_room") is not True:
            return True
        if session.metadata.get("shared_room_revoked") is True:
            return False
        expiry = session.metadata.get("shared_room_expires_at")
        if expiry:
            try:
                if datetime.fromisoformat(expiry.replace("Z", "+00:00")) <= datetime.now(
                    timezone.utc
                ):
                    return False
            except (ValueError, TypeError):
                return False
        return True

    def _room_collaborative(self, chat_id: str) -> bool:
        return bool(
            self._session_manager
            and self._session_manager.get_or_create(f"websocket:{chat_id}").metadata.get(
                "room_mode"
            )
            == "collaborative-v1"
        )

    def _room_work_store(self) -> RoomWorkStore:
        return RoomWorkStore(self._session_manager.workspace)

    async def _handle_room_editorial(self, request, action: str):
        from nanobot.channels.websocket import (
            _API_KEY_RE,
            _ROOM_ID_RE,
            _http_error,
            _http_json_response,
            _is_valid_chat_id,
            _issue_route_secret_matches,
            _request_json,
        )

        if not self.config.token_issue_secret.strip() or not _issue_route_secret_matches(
            request.headers, self.config.token_issue_secret.strip()
        ):
            return _http_error(401, "Unauthorized")
        if not self._session_manager:
            return _http_error(503, "Room storage unavailable")
        body = _request_json(request)
        if not isinstance(body, dict):
            return _http_error(400, "Invalid JSON")
        if action == "preview":
            key = body.get("source_session_key")
            if (
                not isinstance(key, str)
                or not key.startswith("websocket:")
                or not _API_KEY_RE.fullmatch(key)
            ):
                return _http_error(400, "Invalid source")
            stored = self._session_manager.read_session_file(key)
            if stored is None:
                return _http_error(404, "Conversation not found")
            source = self._session_manager.get_or_create(key)
            raw = source.messages
            public, _, _ = self._session_manager._shareable_messages(
                raw, "Owner", source_key=key, source_metadata=source.metadata, destination_key=key
            )
            # Bound the preview by failing visibly instead of sharing unseen rows.
            if len(json.dumps(public).encode()) > 192 * 1024:
                return _http_error(
                    413, "This conversation is too large to preview. Share a shorter conversation."
                )
            return _http_json_response(
                {
                    "message_count": len(raw),
                    "snapshot_sha256": hashlib.sha256(
                        json.dumps(raw, sort_keys=True, default=str).encode()
                    ).hexdigest(),
                    "messages": public,
                    "collaborative": self.config.shared_room_collaboration_enabled,
                }
            )
        if not self.config.shared_room_collaboration_enabled:
            return _http_error(404, "Room collaboration is not enabled")
        room_id, chat_id = body.get("room_id"), body.get("chat_id")
        if (
            not isinstance(room_id, str)
            or not _ROOM_ID_RE.fullmatch(room_id)
            or not _is_valid_chat_id(chat_id)
        ):
            return _http_error(400, "Invalid room")
        session = self._session_manager.get_or_create(f"websocket:{chat_id}")
        if session.metadata.get("room_id") != room_id or not self._room_is_active(chat_id):
            return _http_error(410, "Room expired or revoked")
        if action == "upgrade":
            session.metadata["room_mode"] = "collaborative-v1"
            self._session_manager.save(session, fsync=True)
            await self._broadcast_event(chat_id, "room.state", mode="collaborative-v1")
            return _http_json_response({"ok": True, "mode": "collaborative-v1"})
        if not self._room_collaborative(chat_id):
            return _http_error(409, "Upgrade this room before using collaboration")
        store = self._room_work_store()
        if action == "state":
            return _http_json_response(
                {
                    "mode": "collaborative-v1",
                    "proposals": store.list(room_id, owner=True),
                    "connected_reads": self.connected_room_executor is not None,
                }
            )
        try:
            proposal_id = body.get("proposal_id", "")
            if action == "prepare":
                from nanobot.channels.room_work import action_hash, canonical_action

                with store.transaction(room_id) as data:
                    proposal = next((p for p in data["proposals"] if p["id"] == proposal_id), None)
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
                    proposal["created_at"] = datetime.now(timezone.utc).isoformat()
                return _http_json_response({"ok": True})
            if action == "approve":
                if self.connected_room_executor is None:
                    return _http_error(503, "Connected room reads are unavailable")
                proposal, execute = store.consume(
                    room_id, proposal_id, body.get("argument_hash", "")
                )
                if execute:
                    try:
                        result = await self.connected_room_executor(proposal["action"])
                        failed = not isinstance(result, str) or result.startswith(
                            ("Error", "(MCP tool")
                        )
                        store.finish(room_id, proposal_id, result, failed=failed)
                    except (Exception, asyncio.CancelledError):
                        store.finish(room_id, proposal_id, None, failed=True)
                # Only a status reaches guests; the content remains in private storage.
                proposals = store.list(room_id, owner=True)
                current = next(p for p in proposals if p["id"] == proposal_id)
                await self._broadcast_event(
                    chat_id, "room.proposal", proposal=store.public(current)
                )
                return _http_json_response({"proposal": current})
            if action == "publish":
                # Recheck immediately before the synchronous publication commit.
                if not self._room_is_active(chat_id):
                    return _http_error(410, "Room expired or revoked")
                proposal = store.publish(room_id, proposal_id, body.get("content"))
                publication_id = "room-publication-" + proposal_id
                if not any(m.get("client_message_id") == publication_id for m in session.messages):
                    session.add_message(
                        "assistant",
                        proposal["publication"],
                        client_message_id=publication_id,
                        room_publication=True,
                    )
                self._session_manager.save(session, fsync=True)
                await self._broadcast_event(
                    chat_id, "room.proposal", proposal=store.public(proposal)
                )
                await self._broadcast_event(
                    chat_id,
                    "room.publication",
                    id=publication_id,
                    content=proposal["publication"],
                    timestamp=datetime.now(timezone.utc).isoformat(),
                )
                return _http_json_response({"ok": True})
            if action == "decline":
                with store.transaction(room_id) as data:
                    for proposal in data["proposals"]:
                        if proposal["id"] == proposal_id and proposal["state"] == "proposed":
                            proposal["state"] = "declined"
                await self._broadcast_event(
                    chat_id, "room.state", mode="collaborative-v1", proposals=store.list(room_id)
                )
                return _http_json_response({"ok": True})
        except (ValueError, TypeError) as error:
            return _http_error(409, str(error))
        return _http_error(404, "Not found")

    async def _broadcast_room_presence(self, chat_id: str) -> None:
        if not self._room_collaborative(chat_id) or not self._room_is_active(chat_id):
            return
        participants = {}
        for connection in tuple(self._subs.get(chat_id, ())):
            room = self._conn_room.get(connection) or self._shared_room_owner_credential(chat_id)
            if room:
                participants[room.participant_id] = {
                    "id": room.participant_id,
                    "display_name": room.display_name,
                }
        await self._broadcast_event(
            chat_id, "room.presence", participants=list(participants.values())
        )

    async def _handle_room_intent(
        self, connection: Any, room: Any, envelope: dict[str, Any]
    ) -> bool:
        """Returns true when handled; Ask Ziggy continues through the durable inbox."""
        if not self._room_is_active(room.chat_id):
            await self._send_event(
                connection,
                "error",
                chat_id=room.chat_id,
                client_message_id=envelope.get("client_message_id"),
                detail="Room expired or revoked",
            )
            return True
        if not self._room_collaborative(room.chat_id):
            return False
        intent = envelope.get("room_intent", "discussion")
        if intent == "ask_ziggy":
            return False
        client_id, content = envelope.get("client_message_id"), envelope.get("content")
        if not isinstance(client_id, str) or not isinstance(content, str) or not content.strip():
            await self._send_event(
                connection,
                "error",
                chat_id=room.chat_id,
                client_message_id=client_id,
                detail="A message and stable identity are required",
            )
            return True
        if intent not in {"discussion", "proposal"}:
            await self._send_event(
                connection,
                "error",
                chat_id=room.chat_id,
                client_message_id=client_id,
                detail="Unsupported room intent",
            )
            return True
        session = self._session_manager.get_or_create(f"websocket:{room.chat_id}")
        existing = next(
            (m for m in session.messages if m.get("client_message_id") == client_id), None
        )
        if existing:
            if (
                existing.get("content") != content
                or existing.get("participant_id") != room.participant_id
                or existing.get("room_intent", "discussion") != intent
            ):
                await self._send_message_ack(
                    connection,
                    chat_id=room.chat_id,
                    client_message_id=client_id,
                    status="rejected",
                    detail="Message identity conflict",
                )
            else:
                await self._send_message_ack(
                    connection,
                    chat_id=room.chat_id,
                    client_message_id=client_id,
                    status="duplicate",
                )
            return True
        if intent == "proposal":
            try:
                proposal = self._room_work_store().propose(
                    room.room_id, client_id, room.participant_id, envelope.get("proposal", {})
                )
            except (ValueError, TypeError):
                await self._send_message_ack(
                    connection,
                    chat_id=room.chat_id,
                    client_message_id=client_id,
                    status="rejected",
                    detail="Invalid connected read proposal",
                )
                return True
            await self._broadcast_event(
                room.chat_id, "room.proposal", proposal=RoomWorkStore.public(proposal)
            )
        session.add_message(
            "user",
            content,
            client_message_id=client_id,
            participant_id=room.participant_id,
            participant_display_name=room.display_name,
            room_intent=intent,
        )
        try:
            self._session_manager.save(session, fsync=True)
        except Exception:
            session.messages.pop()
            raise
        await self._send_message_ack(
            connection, chat_id=room.chat_id, client_message_id=client_id, status="accepted"
        )
        await self._broadcast_event(
            room.chat_id,
            "participant.message",
            client_message_id=client_id,
            participant_id=room.participant_id,
            display_name=room.display_name,
            content=content,
            room_intent=intent,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        return True
