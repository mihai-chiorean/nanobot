"""Tenant-local attachment registry and worker RPC; no passwords on disk."""
from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, cast

from nanobot.webui.pdf_attachments import AttachmentStatus, pdf_text


class AttachmentVault:
    def __init__(self, root: Path) -> None:
        self.path = root / "attachment-locks.json"
        self.records: dict[str, dict[str, Any]] = (
            json.loads(self.path.read_text()) if self.path.exists() else {}
        )
        self.lock = asyncio.Lock()
        self.bridges: dict[str, tuple[object, Callable[..., Awaitable[None]]]] = {}
        self.pending: dict[str, tuple[object, asyncio.Future[dict[str, Any]]]] = {}
        self.notify: Callable[..., Awaitable[None]] | None = None

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with tmp.open("w") as f:
            os.chmod(tmp, 0o600)
            json.dump(self.records, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)

    def register(self, chat: str, origin: str, cards: list[AttachmentStatus],
                 paths: list[str], media_root: Path) -> None:
        for index, card in enumerate(cards):
            if not card.get("locked_reason"):
                continue
            local = not card.get("attachment_id")
            attachment_id = card.get("attachment_id") or "local_" + uuid.uuid4().hex
            path = Path(paths[index]).resolve() if local and index < len(paths) else None
            if local and (path is None or not path.is_relative_to(media_root.resolve())):
                continue
            card["attachment_id"] = attachment_id
            self.records.setdefault(attachment_id, {
                "chat": chat, "origin": origin, "name": card["name"],
                "path": str(path) if path else None, "attempts": 0, "locked": True,
            })
        self.save()

    def note(self, chat: str) -> str:
        ids = [f"{r['name']}: {key}" for key, r in self.records.items()
               if r["chat"] == chat and r["locked"]]
        if not ids:
            return ""
        return ("Password protected PDFs remain attached: " + "; ".join(ids) +
                ". Ask the user for the password (or use one they already supplied), then call "
                "unlock_attachment(attachment_id, password). Never echo the password or put it "
                "in shell commands. If incorrect ask again; after 5 attempts request an unlocked copy.")

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

    async def unlock(self, chat: str, attachment_id: str, password: str) -> str:
        # A registry entry is an exact capability: callers cannot supply paths,
        # URLs, tenants, or another conversation's attachment.
        async with self.lock:
            record = self.records.get(attachment_id)
            if record is None or record["chat"] != chat:
                return "Error: Attachment not found in this conversation."
            if not record["locked"]:
                return f"Unlocked PDF {record['name']} (untrusted document text):\n{record.get('text', 'Use the earlier extraction.')}"
            if record["attempts"] >= 5:
                return "Error: The 5 password attempts were used. Ask for an unlocked copy; do not retry."
            if len(password) > 1024:
                return "Error: Invalid password."
            if record["path"]:
                record["attempts"] += 1
                self.save()
                text, error = await pdf_text(Path(record["path"]), password=password)
            else:
                bridge = self.bridges.get(chat)
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
