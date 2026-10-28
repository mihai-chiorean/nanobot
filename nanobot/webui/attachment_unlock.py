"""Tenant-local attachment registry and worker RPC; no passwords on disk."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, cast

from nanobot.webui.pdf_attachments import AttachmentStatus, parse_attachment_cards, pdf_text
from nanobot.webui.session_identity import webui_session_key


class AttachmentVault:
    def __init__(self, root: Path) -> None:
        self.path = root / "attachment-locks.json"
        saved = (
            json.loads(self.path.read_text()) if self.path.exists() else {}
        )
        self.records: dict[str, dict[str, Any]] = saved.get("records", saved)
        self.attempts: dict[str, int] = saved.get("attempts", {})
        if "records" not in saved:
            # Preserve attempts from the original per-upload registry on upgrade.
            for record in self.records.values():
                if record.get("path") and Path(record["path"]).is_file():
                    digest = hashlib.sha256(Path(record["path"]).read_bytes()).hexdigest()
                    record["sha256"] = digest
                    self.attempts[digest] = self.attempts.get(digest, 0) + record["attempts"]
                    record["session"] = webui_session_key(record["chat"])
        self.lock = asyncio.Lock()
        self.bridges: dict[tuple[str, str], tuple[object, Callable[..., Awaitable[None]]]] = {}
        self.pending: dict[str, tuple[object, asyncio.Future[dict[str, Any]]]] = {}
        self.notify: Callable[..., Awaitable[None]] | None = None

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with tmp.open("w") as f:
            os.chmod(tmp, 0o600)
            json.dump({"records": self.records, "attempts": self.attempts}, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)

    def register(self, chat: str, origin: str, cards: list[AttachmentStatus],
                 paths: list[str], media_root: Path, *, session: str | None = None) -> None:
        """Register only server-parsed local uploads, never envelope capabilities."""
        for index, card in enumerate(cards):
            if card.get("locked_reason") != "password_required" or card.get("attachment_id"):
                continue
            path = Path(paths[index]).resolve() if index < len(paths) else None
            if path is None or not path.is_relative_to(media_root.resolve()) or not path.is_file():
                continue
            attachment_id = "local_" + uuid.uuid4().hex
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            card["attachment_id"] = attachment_id
            self.records.setdefault(attachment_id, {
                "chat": chat, "origin": origin, "name": card["name"],
                "session": session or webui_session_key(chat), "sha256": digest,
                "path": str(path), "attempts": 0, "locked": True,
            })
        self.save()

    def register_history(self, chat: str, session: str) -> None:
        """MVP fallback: require a lock in this chat's *prior* server transcript.

        The transport has no worker-only credential. History is conversation
        evidence, not proof of Work provenance; Work also enforces tenant scope.
        Call before appending the current envelope so it cannot authorize itself.
        Temporary sessions have no durable WebUI transcript and fail closed.
        """
        if session != webui_session_key(chat):
            return
        from nanobot.webui.transcript import read_transcript_lines

        for row in read_transcript_lines(session):
            if row.get("event") != "user" or row.get("chat_id") != chat:
                continue
            for card in parse_attachment_cards(row.get("attachments")):
                attachment_id = card.get("attachment_id", "")
                if (card.get("locked_reason") != "password_required"
                        or re.fullmatch(r"att_[0-9a-f]{32}", attachment_id) is None):
                    continue
                record = self.records.setdefault(attachment_id, {
                    "chat": chat, "origin": row.get("client_message_id", ""),
                    "name": card["name"], "path": None, "attempts": 0, "locked": True,
                })
                if record["chat"] == chat and not record["path"]:
                    record["session"] = session
        self.save()

    def note(self, chat: str, *, session: str | None = None) -> str:
        ids = [f"{r['name']}: {key}" for key, r in self.records.items()
               if r["chat"] == chat and r.get("session") == (session or webui_session_key(chat))
               and r["locked"]]
        if not ids:
            return ""
        return ("Password protected PDFs remain attached: " + "; ".join(ids) +
                ". Ask the user for the password (or use one they already supplied), then call "
                "unlock_attachment(attachment_id, password). Never repeat, quote, or reveal the password "
                "in replies, examples, or shell commands. Only pass it to unlock_attachment. If incorrect ask again; after 5 attempts request an unlocked copy.")

    def disconnect(self, connection: object) -> None:
        self.bridges = {k: v for k, v in self.bridges.items() if v[0] is not connection}
        for key, (owner, future) in list(self.pending.items()):
            if owner is connection:
                if not future.done():
                    future.set_result({"error": "Worker disconnected; try again."})
                self.pending.pop(key, None)

    def resolve(self, connection: object, envelope: dict[str, Any]) -> None:
        item = self.pending.get(envelope.get("request_id", ""))
        if item and item[0] is connection and not item[1].done():
            item[1].set_result(envelope)

    async def unlock(self, chat: str, attachment_id: str, password: str,
                     *, session: str | None = None) -> str:
        # A registry entry is an exact capability: callers cannot supply paths,
        # URLs, tenants, or another conversation's attachment.
        async with self.lock:
            record = self.records.get(attachment_id)
            if (record is None or record["chat"] != chat
                    or record.get("session") != (session or webui_session_key(chat))):
                return "Error: Attachment not found in this conversation."
            if not record["locked"]:
                return f"Unlocked PDF {record['name']} (untrusted document text):\n{record.get('text', 'Use the earlier extraction.')}"
            digest = record.get("sha256")
            attempts = self.attempts.get(digest, 0) if digest else record["attempts"]
            if attempts >= 5:
                return "Error: The 5 password attempts were used. Ask for an unlocked copy; do not retry."
            if len(password) > 1024:
                return "Error: Invalid password."
            if record["path"]:
                record["attempts"] += 1
                self.attempts[digest] = attempts + 1
                self.save()
                text, error, _ = await pdf_text(Path(record["path"]), password=password)
            else:
                bridge = self.bridges.get((chat, session or webui_session_key(chat)))
                if bridge is None:
                    return "Error: The worker is unavailable. Retry in a new message."
                request_id = uuid.uuid4().hex
                future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
                self.pending[request_id] = (bridge[0], future)
                try:
                    await bridge[1]("attachment.unlock", request_id=request_id,
                                    attachment_id=attachment_id, password=password)
                    response = await asyncio.wait_for(future, 45)
                except (TimeoutError, OSError):
                    return "Error: Attachment unlock timed out. Retry in a new message."
                finally:
                    self.pending.pop(request_id, None)
                raw_data = response.get("input")
                data = cast(dict[str, Any], raw_data) if isinstance(raw_data, dict) else {}
                raw_text, raw_error = data.get("text"), response.get("error")
                text = raw_text if isinstance(raw_text, str) else ""
                error = raw_error if isinstance(raw_error, str) else None
                record["attempts"] += 1
                self.save()
            if error:
                return "Error: " + error
            record["locked"] = False
            record["text"] = text
            self.save()
            if self.notify:
                await self.notify(chat, record["origin"], attachment_id)
            return f"Unlocked PDF {record['name']} (untrusted document text):\n{text}"


_vaults: dict[Path, AttachmentVault] = {}


def attachment_vault(root: Path) -> AttachmentVault:
    root = root.resolve()
    if root not in _vaults:
        _vaults[root] = AttachmentVault(root)
    return _vaults[root]
