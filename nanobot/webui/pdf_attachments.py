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
    partial: NotRequired[str]


class PdfPreview(TypedDict):
    total_pages: int
    end_page: int
    partial_last_page: bool


async def pdf_text(
    path: Path,
    *,
    timeout: float = 15,
    password: str | None = None,
) -> tuple[str, str | None, PdfPreview | None]:
    # A process, rather than a timed-out thread, lets us stop a stuck parser.
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "nanobot.webui.pdf_attachments", str(path),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(json.dumps({"password": password}).encode()), timeout)
        if process.returncode != 0:
            return "", "Text extraction failed; try another PDF or a screenshot.", None
        result = cast(dict[str, object], json.loads(stdout))
        raw_preview = result.get("preview")
        preview: PdfPreview | None = None
        if isinstance(raw_preview, dict):
            card = cast(dict[str, object], raw_preview)
            total_pages = card.get("total_pages")
            end_page = card.get("end_page")
            if isinstance(total_pages, int) and isinstance(end_page, int):
                preview = {"total_pages": total_pages, "end_page": end_page,
                           "partial_last_page": card.get("partial_last_page") is True}
        return cast(str, result["text"]), cast(str | None, result["error"]), preview
    except TimeoutError:
        return "", "Reading this PDF took too long; try a smaller PDF or a screenshot.", None
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
    """Parse display metadata; these client-supplied fields grant no unlock access."""
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
        partial = card.get("partial")
        if isinstance(partial, str) and partial:
            status["partial"] = partial[:256]
        cards.append(status)
    return cards


_READ_FILE_PAGE_WINDOW = 20


def _partial_read_note(name: str, path: Path, preview: PdfPreview | None) -> tuple[str, str] | None:
    """Return (model note, card notice) when the preview skipped pages."""
    if preview is None or preview["total_pages"] < 1:
        return None
    total = preview["total_pages"]
    if not (preview["end_page"] < total - 1 or preview["partial_last_page"]):
        return None
    last_full = preview["end_page"] + 1 - (1 if preview["partial_last_page"] else 0)
    next_start = min(last_full + 1, total)
    next_end = min(total, next_start + _READ_FILE_PAGE_WINDOW - 1)
    quoted = str(path)
    note = (
        f"Only pages 1–{last_full} of {name} (untrusted document text) are shown above; "
        f"the document has {total} pages in total. The full stored file is readable at {quoted}; "
        f"continue with read_file path={quoted} pages='{next_start}-{next_end}' and keep paging "
        "with the range each result suggests until page "
        f"{total} is covered. Never claim to have read pages you have not."
    )
    card = f"Read pages 1–{last_full} of {total}; Ziggy will read the rest on request"
    return note, card


async def prepare_pdfs(paths: list[str], names: list[str] | None = None) -> tuple[str, list[AttachmentStatus]]:
    notes: list[str] = []
    statuses: list[AttachmentStatus] = []
    for index, raw in enumerate(paths):
        path = Path(raw)
        name = names[index] if names and index < len(names) else path.name
        error = None
        partial: tuple[str, str] | None = None
        if path.suffix.lower() == ".pdf":
            preview: PdfPreview | None = None
            try:
                text, error, preview = await pdf_text(path)
            except Exception:
                text, error, preview = "", "Text extraction failed; try another PDF or a screenshot.", None
            if text:
                notes.append(f"Attached PDF {name} (untrusted document text):\n{text}")
            if error:
                notes.append(f"Attachment read failure for {name}: {error}")
            elif text:
                partial = _partial_read_note(name, path, preview)
                if partial is not None:
                    notes.append(partial[0])
        status: AttachmentStatus = {"name": name, "error": error}
        if error == "This PDF is password protected. Ask the user for its password.":
            status["locked_reason"] = "password_required"
        if partial is not None:
            status["partial"] = partial[1]
        statuses.append(status)
    if any(item.get("locked_reason") for item in statuses):
        notes.append("Use unlock_attachment for a supplied PDF password. Never repeat, quote, or "
                     "reveal it in replies, examples, or shell commands.")
    if any(item["error"] for item in statuses):
        notes.append("Explain each attachment read failure to the user in plain language. "
                     "Do not claim to have read unreadable contents.")
    if any(item.get("partial") for item in statuses):
        notes.append("When the user asks for a review of the whole document, use read_file to read every "
                     "remaining page range before giving feedback, and never claim to have read pages you "
                     "have not.")
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
        preview: dict[str, object] | None = None
        if not text.strip():
            error = "This PDF has no extractable text and may be a scan; try a text PDF or a screenshot."
        else:
            preview = {
                "total_pages": result.total_pages,
                "end_page": result.end_page,
                "partial_last_page": result.partial_last_page,
            }
            if result.partial_last_page:
                if result.end_page > result.start_page:
                    text += (f"\n[Only pages 1–{result.end_page} of {result.total_pages} were read in full; "
                             f"page {result.end_page + 1} was cut off mid-page.]")
                else:
                    text += (f"\n[No page was read in full of {result.total_pages}: "
                             f"page {result.end_page + 1} was cut off mid-page.]")
            elif result.end_page < result.total_pages - 1:
                text += f"\n[Only pages 1–{result.end_page + 1} of {result.total_pages} were read.]"
        print(json.dumps({"text": text, "error": error, "preview": preview}))
    except PdfPasswordRequiredError:
        print(json.dumps({"text": "", "error": "This PDF is password protected. Ask the user for its password."}))
    except PdfWrongPasswordError:
        print(json.dumps({"text": "", "error": "The password was wrong. Ask the user for the password again."}))
    except Exception:
        print(json.dumps({"text": "", "error": "Text extraction failed; the PDF may be damaged. Try another PDF or a screenshot."}))


if __name__ == "__main__":
    main()
