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

from nanobot.webui.pdf_attachments import (
    AttachmentStatus,
    parse_attachment_cards,
    pdf_text,
    unlock_partial_note,
)
from nanobot.webui.session_identity import webui_session_key

# Page results must fit inside the configured ``max_tool_result_chars`` so
# the model receives the range whole instead of an offload stub, and the
# continuation hint survives (MIT-1967 re-review item 4). The extraction
# ceiling is that limit minus a reserve for the framing this module appends
# around the text (head, oversized-page disclosure, continuation ≈ 800 chars,
# 1,200 with long attachment names).
UNLOCK_TOOL_RESULT_FALLBACK_CHARS = 16_000  # AgentDefaults.max_tool_result_chars
UNLOCK_PAGE_RESULT_OVERHEAD_CHARS = 1_200
UNLOCK_PAGE_MIN_CHARS = 512
UNLOCK_PAGE_MAX_PAGES = 100
UNLOCK_PAGE_WINDOW = 20


def unlock_page_max_chars() -> int:
    """Per-call page extraction budget derived from the configured tool-result limit."""
    limit = UNLOCK_TOOL_RESULT_FALLBACK_CHARS
    try:
        from nanobot.config.loader import load_config
        configured = int(load_config().agents.defaults.max_tool_result_chars)
        if configured > 0:
            limit = configured
    except Exception:
        pass
    return max(UNLOCK_PAGE_MIN_CHARS, limit - UNLOCK_PAGE_RESULT_OVERHEAD_CHARS)


def _format_page_result(name: str, attachment_id: str, pages: str, extraction: Any) -> str:
    """Render one unlock page range with its continuation, mirroring read_file."""
    total = extraction.total_pages
    partial = bool(extraction.partial_last_page)
    last_full = extraction.end_page + 1 - (1 if partial else 0)
    raw_start = pages.strip().split("-")[0].strip()
    start = raw_start if raw_start.isdigit() and int(raw_start) >= 1 else "1"
    shown = max(last_full, int(start)) if total > 0 else 0
    head = f"Unlocked PDF {name} pages {start}-{shown} of {total} (untrusted document text):\n"
    if extraction.text:
        body = extraction.text
    elif total <= 0:
        body = "(PDF has no extractable text)"
    else:
        body = f"(No extractable text on pages {start}-{extraction.end_page + 1})"
    if partial:
        body += (
            f"\n\n(Page {extraction.end_page + 1} exceeded the per-call character limit; "
            "only its first part is shown, so do not claim to have read its full contents.)"
        )
    if extraction.end_page < total - 1:
        # Continue at the following page, as read_file does: an oversized
        # partial page was disclosed above, and pointing the continuation
        # back at it re-served the same truncated prefix forever (re-review
        # blocking item 4).
        next_start = min(extraction.end_page + 2, total)
        next_end = min(total, next_start + UNLOCK_PAGE_WINDOW - 1)
        body += (
            f"\n\nContinue with unlock_attachment attachment_id='{attachment_id}' "
            f"pages='{next_start}-{next_end}' and keep paging with the range each result "
            f"suggests until page {total} is covered. Never claim to have read pages you have not."
        )
    return head + body


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
                     *, session: str | None = None, pages: str | None = None) -> str:
        # A registry entry is an exact capability: callers cannot supply paths,
        # URLs, tenants, or another conversation's attachment.
        async with self.lock:
            record = self.records.get(attachment_id)
            if (record is None or record["chat"] != chat
                    or record.get("session") != (session or webui_session_key(chat))):
                return "Error: Attachment not found in this conversation."
            if not record["locked"]:
                if pages is not None:
                    # Page reads decrypt the stored file: the attempt cap
                    # checked on the locked path must gate this one too,
                    # or re-upload attempts could be exhausted around an
                    # already-unlocked attachment while paging keeps
                    # decrypting (re-review blocking item 6).
                    page_digest = record.get("sha256")
                    page_attempts = (self.attempts.get(page_digest, 0) if page_digest
                                     else record["attempts"])
                    if page_attempts >= 5:
                        return ("Error: The 5 password attempts were used. Ask for an "
                                "unlocked copy; do not retry.")
                    return await self._read_pages(attachment_id, record, pages, password)
                return (f"Unlocked PDF {record['name']} (untrusted document text):\n"
                        f"{record.get('text', 'Use the earlier extraction.')}")
            digest = record.get("sha256")
            attempts = self.attempts.get(digest, 0) if digest else record["attempts"]
            if attempts >= 5:
                return "Error: The 5 password attempts were used. Ask for an unlocked copy; do not retry."
            if len(password) > 1024:
                return "Error: Invalid password."
            preview = None
            if record["path"]:
                record["attempts"] += 1
                self.attempts[digest] = attempts + 1
                self.save()
                text, error, preview = await pdf_text(Path(record["path"]), password=password)
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
            if record["path"]:
                note = unlock_partial_note(record["name"], attachment_id, preview)
                if note is not None:
                    record["text"] = f"{text}\n\n{note[0]}"
                    record["partial"] = note[1]
                else:
                    record["text"] = text
            else:
                record["text"] = text
            self.save()
            if self.notify:
                await self.notify(chat, record["origin"], attachment_id)
            return (f"Unlocked PDF {record['name']} (untrusted document text):\n"
                    f"{record['text']}")

    async def _read_pages(self, attachment_id: str, record: dict[str, Any],
                          pages: str, password: str) -> str:
        """Serve a page range from a verified attachment without exposing the
        password to read_file (MIT-1967 review). Each result stays inside the
        configured ``max_tool_result_chars`` budget so paging returns whole
        pages, and the continuation hint carries the walk to the final page."""
        from nanobot.utils.document import (
            PdfPageRangeError,
            PdfPasswordRequiredError,
            PdfSafetyError,
            PdfWrongPasswordError,
            extract_pdf_pages,
        )

        raw_path = record.get("path")
        if not raw_path:
            return ("Error: This copy is held by the worker; page reads need the file "
                    "stored here. Ask the user to re-upload the PDF to read it page by page.")
        path = Path(str(raw_path))
        if not path.is_file():
            return "Error: The stored PDF is no longer available; ask the user to re-upload it."
        last_error = "Text extraction failed."
        for candidate in [password] if password else [""]:
            try:
                extraction = await asyncio.to_thread(
                    extract_pdf_pages, path, pages=pages,
                    max_pages=UNLOCK_PAGE_MAX_PAGES, max_chars=unlock_page_max_chars(),
                    password=candidate or None,
                )
            except PdfPageRangeError as e:
                return f"Error: Invalid page range '{pages}': {e!s}."
            except PdfSafetyError as e:
                return f"Error: Reading PDF pages failed: {e!s}"
            except (PdfPasswordRequiredError, PdfWrongPasswordError):
                last_error = ("The stored PDF no longer matches the verified password. "
                              "Ask the user for the password again.")
                continue
            except Exception:
                last_error = "Text extraction failed; try a smaller page range."
                continue
            return _format_page_result(record["name"], attachment_id, pages, extraction)
        return "Error: " + last_error


_vaults: dict[Path, AttachmentVault] = {}


def attachment_vault(root: Path) -> AttachmentVault:
    root = root.resolve()
    if root not in _vaults:
        _vaults[root] = AttachmentVault(root)
    return _vaults[root]
