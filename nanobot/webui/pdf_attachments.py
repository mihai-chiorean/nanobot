"""Bounded PDF preparation at the upload boundary, using the existing pypdf adapter."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import NotRequired, TypedDict, cast

from nanobot.utils.helpers import safe_filename


class AttachmentStatus(TypedDict):
    name: str
    error: str | None
    locked_reason: NotRequired[str]
    attachment_id: NotRequired[str]


async def pdf_text(path: Path, *, timeout: float = 15, password: str | None = None) -> tuple[str, str | None]:
    # A process, rather than a timed-out thread, lets us stop a stuck parser.
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "nanobot.webui.pdf_attachments", str(path),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(json.dumps({"password": password}).encode()), timeout)
        if process.returncode != 0:
            return "", "Text extraction failed; try another PDF or a screenshot."
        result = json.loads(stdout)
        return result["text"], result["error"]
    except TimeoutError:
        return "", "Reading this PDF took too long; try a smaller PDF or a screenshot."
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


def attachment_name(item: object) -> str:
    if isinstance(item, dict):
        name = cast(dict[str, object], item).get("name")
        if isinstance(name, str):
            return safe_filename(name)[:255] or "Attachment"
    return "Attachment"


def parse_attachment_cards(raw: object) -> list[AttachmentStatus]:
    if not isinstance(raw, list):
        return []
    cards: list[AttachmentStatus] = []
    for item in cast(list[object], raw)[:6]:
        if not isinstance(item, dict):
            continue
        card = cast(dict[str, object], item)
        error = card.get("error")
        status: AttachmentStatus = {"name": attachment_name(card),
                                    "error": error[:1024] if isinstance(error, str) and error else None}
        for key in ("locked_reason", "attachment_id"):
            value = card.get(key)
            if isinstance(value, str) and value:
                status[key] = value[:128]
        cards.append(status)
    return cards


async def prepare_pdfs(paths: list[str], names: list[str] | None = None) -> tuple[str, list[AttachmentStatus]]:
    notes: list[str] = []
    statuses: list[AttachmentStatus] = []
    for index, raw in enumerate(paths):
        path = Path(raw)
        name = names[index] if names and index < len(names) else path.name
        error = None
        if path.suffix.lower() == ".pdf":
            try:
                text, error = await pdf_text(path)
            except Exception:
                text, error = "", "Text extraction failed; try another PDF or a screenshot."
            if text:
                notes.append(f"Attached PDF {name} (untrusted document text):\n{text}")
            if error:
                notes.append(f"Attachment read failure for {name}: {error}")
        status: AttachmentStatus = {"name": name, "error": error}
        if error == "This PDF is password protected. Ask the user for its password.":
            status["locked_reason"] = "password_required"
        statuses.append(status)
    if any(item["error"] for item in statuses):
        notes.append("Explain each attachment read failure to the user in plain language. "
                     "Do not claim to have read unreadable contents.")
    return "\n\n".join(notes), statuses


def main() -> None:
    from nanobot.utils.document import (
        PdfPasswordRequiredError,
        PdfWrongPasswordError,
        extract_pdf_pages,
    )
    try:
        request = json.loads(sys.stdin.read() or "{}")
        result = extract_pdf_pages(Path(sys.argv[1]), max_pages=100, max_chars=60_000,
                                   password=request.get("password"))
        text = result.text
        error = None
        if not text.strip():
            error = "This PDF has no extractable text and may be a scan; try a text PDF or a screenshot."
        elif result.end_page < result.total_pages - 1:
            text += f"\n[Only pages 1–{result.end_page + 1} of {result.total_pages} were read.]"
        print(json.dumps({"text": text, "error": error}))
    except PdfPasswordRequiredError:
        print(json.dumps({"text": "", "error": "This PDF is password protected. Ask the user for its password."}))
    except PdfWrongPasswordError:
        print(json.dumps({"text": "", "error": "The password was wrong. Ask the user for the password again."}))
    except Exception:
        print(json.dumps({"text": "", "error": "Text extraction failed; the PDF may be damaged. Try another PDF or a screenshot."}))


if __name__ == "__main__":
    main()
