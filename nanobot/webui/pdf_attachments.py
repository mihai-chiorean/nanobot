"""Bounded PDF preparation at the upload boundary, using the existing pypdf adapter."""
from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import NotRequired, TypedDict, cast

from nanobot.utils.helpers import estimate_prompt_tokens, safe_filename


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


# MIT-1967 follow-up: load the whole document when it fits. Beyond the caps
# and past the session's aggregate char budget, the partial-preview path
# (note + read_file paging) applies.
UPLOAD_MAX_PAGES = 200
UPLOAD_MAX_CHARS = 600_000
# MIT-1967 review: the whole-document budget is sized per message from the
# active session's context window so a 65k session never receives a note that
# `ContextGovernor.ensure_request_fits` would reject outright (it raises
# instead of trimming). Attachments get full text in order while the budget
# allows; the rest take the partial-preview path. Admission is measured in
# *estimated tokens* with the very estimator chain the governor uses
# (`estimate_prompt_tokens`, tiktoken with the conservative byte fallback),
# never a fixed chars/token ratio: the byte fallback charges ~1 token per
# byte, so a chars/token budget of 4 let an English PDF admit 4x more text
# than the governor then accepted (MIT-1967 re-review, blocking item 1).
ATTACHMENT_CONTEXT_TOKEN_SHARE = 0.45
# Smallest context window the WebUI settings offer (settings_models.py
# _CONTEXT_WINDOW_TOKEN_OPTIONS). An unresolvable window is treated as this
# size, never a larger guess: over-assuming a window is what made the
# governor reject the whole turn (re-review blocking item 2).
MINIMUM_KNOWN_CONTEXT_WINDOW = 65_536
# Even a budget-exhausted attachment keeps a real first-page preview so its
# note and partial card can name the true total page count and next range.
MIN_PREVIEW_CHARS = 2_048
# Byte-fallback floor for a MIN_PREVIEW_CHARS ASCII preview: extraction is
# only attempted while this many estimated tokens remain.
MIN_PREVIEW_TOKENS = MIN_PREVIEW_CHARS
# 200 pages of pypdf extraction can exceed the old 15 s budget on the Spark.
PDF_TEXT_TIMEOUT_SECONDS = 60


def attachment_token_budget(context_window_tokens: int | None) -> int:
    """Aggregate estimated-token budget one message's PDF texts may share.

    The per-file ``UPLOAD_MAX_CHARS`` ceiling still caps how much any single
    document contributes; the budget below then admits documents only while
    their summed *estimated* tokens fit the session's window share.
    """
    window = context_window_tokens if context_window_tokens and context_window_tokens > 0 \
        else MINIMUM_KNOWN_CONTEXT_WINDOW
    return int(window * ATTACHMENT_CONTEXT_TOKEN_SHARE)


def attachment_token_estimate(text: str) -> int:
    """Estimated prompt tokens the governor will charge for one note body."""
    return estimate_prompt_tokens([{"role": "user", "content": text}])


async def _run_pdf_request(
    path: Path,
    request: dict[str, object],
    timeout: float,
) -> tuple[str, str | None, PdfPreview | None]:
    # A process, rather than a timed-out thread, lets us stop a stuck parser.
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "nanobot.webui.pdf_attachments", str(path),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(json.dumps(request).encode()), timeout)
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


async def pdf_text(
    path: Path,
    *,
    timeout: float = PDF_TEXT_TIMEOUT_SECONDS,
    password: str | None = None,
    max_chars: int = UPLOAD_MAX_CHARS,
) -> tuple[str, str | None, PdfPreview | None]:
    return await _run_pdf_request(
        path, {"password": password, "max_chars": max_chars}, timeout)


async def pdf_encryption_error(
    path: Path,
    *,
    timeout: float = PDF_TEXT_TIMEOUT_SECONDS,
) -> str | None:
    """Run only the encryption check, so a deferred attachment still gets
    the locked flow (re-review blocking item 3: a deferred encrypted PDF
    used to miss ``locked_reason`` and its note pointed at ``read_file``,
    which cannot decrypt)."""
    _, error, _ = await _run_pdf_request(path, {"check_encrypted": True}, timeout)
    return error


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


def _partial_read_note(
    name: str,
    path: Path | None,
    preview: PdfPreview | None,
    *,
    continuation: Callable[[int, int], str] | None = None,
    mentions_stored_file: bool = True,
) -> tuple[str, str] | None:
    """Return (model note, card notice) when the preview skipped pages.

    ``continuation(next_start, next_end)`` replaces the default
    ``read_file path=... pages='a-b'`` instruction for readers that cannot
    open the stored file directly (for example an encrypted PDF, which only
    ``unlock_attachment`` can page).
    """
    if preview is None or preview["total_pages"] < 1:
        return None
    total = preview["total_pages"]
    if not (preview["end_page"] < total - 1 or preview["partial_last_page"]):
        return None
    last_full = preview["end_page"] + 1 - (1 if preview["partial_last_page"] else 0)
    # Continue at the page after the one the result walked to, mirroring
    # read_file: an oversized partial page was disclosed by the text itself,
    # and pointing the continuation back at it would re-serve the same
    # truncated prefix forever (re-review blocking item 4).
    has_more = preview["end_page"] < total - 1
    next_start = min(preview["end_page"] + 2, total)
    next_end = min(total, next_start + _READ_FILE_PAGE_WINDOW - 1)
    quoted = str(path) if path is not None else ""
    if continuation is None:
        def read_file_continuation(start: int, end: int) -> str:
            return f"read_file path={quoted} pages='{start}-{end}'"
        continuation = read_file_continuation
    shown = (
        f"No page of {name} (untrusted document text) was included in full above"
        if last_full <= 0
        else f"Only pages 1–{last_full} of {name} (untrusted document text) are shown above"
    )
    readable = f" The full stored file is readable at {quoted}." if mentions_stored_file else ""
    walk = (
        f" Continue with {continuation(next_start, next_end)} and keep paging "
        f"with the range each result suggests until page {total} is covered."
        if has_more
        else ""
    )
    note = (
        f"{shown}; the document has {total} pages in total.{readable}{walk} "
        "Never claim to have read pages you have not."
    )
    card = (
        f"Read 0 of {total} pages; Ziggy will read the rest on request"
        if last_full <= 0
        else f"Read pages 1–{last_full} of {total}; Ziggy will read the rest on request"
    )
    return note, card


def unlock_partial_note(
    name: str,
    attachment_id: str,
    preview: PdfPreview | None,
) -> tuple[str, str] | None:
    """Partial note/card for an unlocked encrypted PDF whose text was capped.

    ``read_file`` cannot decrypt the stored file, so the continuation points
    at ``unlock_attachment`` page reads instead (MIT-1967 review).
    """

    def unlock_continuation(start: int, end: int) -> str:
        return f"unlock_attachment attachment_id='{attachment_id}' pages='{start}-{end}'"

    return _partial_read_note(
        name, None, preview,
        continuation=unlock_continuation, mentions_stored_file=False,
    )


def _deferred_read_note(name: str, path: Path) -> tuple[str, str]:
    """Note/card for an attachment left out once the aggregate budget is spent."""
    quoted = str(path)
    next_start, next_end = 1, _READ_FILE_PAGE_WINDOW
    note = (
        f"No pages of {name} (untrusted document text) are shown above: this message's "
        f"attachment budget (sized from the session's context window) is already spent. "
        f"The full stored file is readable at {quoted}; read it with "
        f"read_file path={quoted} pages='{next_start}-{next_end}' and keep paging "
        "with the range each result suggests before reviewing the whole document. "
        "Never claim to have read pages you have not."
    )
    card = "Not yet read; Ziggy will read it on request"
    return note, card


async def _extract_pdf_within_budget(
    path: Path,
    remaining_tokens: int,
) -> tuple[str, str | None, PdfPreview | None]:
    """Extract a PDF whose admitted text stays inside the turn's token budget.

    The first pass runs under the per-file char ceiling; when its measured
    estimate overshoots, one re-extraction takes the slice sized from the
    document's own tokens-per-char density. Char maths alone cannot size the
    retry: the byte fallback (and CJK-dense text) charges far more tokens
    than the char count suggests (re-review blocking item 1).
    """
    text, error, preview = await pdf_text(path, max_chars=UPLOAD_MAX_CHARS)
    if not text or error:
        return text, error, preview
    estimated = attachment_token_estimate(text)
    if estimated <= remaining_tokens:
        return text, error, preview
    scaled = int(len(text) * remaining_tokens / max(estimated, 1) * 0.9)
    scaled = max(MIN_PREVIEW_CHARS, min(UPLOAD_MAX_CHARS, scaled))
    if scaled >= len(text):
        return text, error, preview  # Cannot shrink further; the clamp below pays for it.
    return await pdf_text(path, max_chars=scaled)


async def prepare_pdfs(
    paths: list[str],
    names: list[str] | None = None,
    *,
    context_window_tokens: int | None = None,
) -> tuple[str, list[AttachmentStatus]]:
    notes: list[str] = []
    statuses: list[AttachmentStatus] = []
    remaining = attachment_token_budget(context_window_tokens)
    extracted_any = False
    for index, raw in enumerate(paths):
        path = Path(raw)
        name = names[index] if names and index < len(names) else path.name
        error = None
        partial: tuple[str, str] | None = None
        if path.suffix.lower() == ".pdf":
            preview: PdfPreview | None = None
            deferred = extracted_any and remaining < MIN_PREVIEW_TOKENS
            if deferred:
                # The aggregate budget is spent: later attachments keep the
                # partial-preview path without extracting, so the message
                # total stays inside the budget. The cheap encryption check
                # still runs so a protected file gets the normal locked flow
                # instead of a read_file instruction it cannot follow.
                text = ""
                error = await pdf_encryption_error(path)
            else:
                try:
                    text, error, preview = await _extract_pdf_within_budget(path, remaining)
                except Exception:
                    text, error, preview = "", "Text extraction failed; try another PDF or a screenshot.", None
                if text:
                    note_body = f"Attached PDF {name} (untrusted document text):\n{text}"
                    remaining -= min(remaining, attachment_token_estimate(note_body))
                    extracted_any = True
            if text:
                notes.append(f"Attached PDF {name} (untrusted document text):\n{text}")
            if error:
                notes.append(f"Attachment read failure for {name}: {error}")
            elif deferred:
                partial = _deferred_read_note(name, path)
                if partial is not None:
                    notes.append(partial[0])
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
        if request.get("check_encrypted") is True:
            # Deferred attachments (budget spent) need only the encryption
            # bit, at reader-open cost, to keep the locked flow alive.
            from pypdf import PdfReader
            reader = PdfReader(Path(sys.argv[1]), strict=False)
            locked = reader.is_encrypted and not reader.decrypt("")
            print(json.dumps({
                "text": "",
                "error": ("This PDF is password protected. Ask the user for its password."
                          if locked else None),
                "preview": None,
            }))
            return
        raw_max_chars = request.get("max_chars")
        max_chars = (raw_max_chars if isinstance(raw_max_chars, int) and 1 <= raw_max_chars <= UPLOAD_MAX_CHARS
                     else UPLOAD_MAX_CHARS)
        result = extract_pdf_pages(Path(sys.argv[1]), max_pages=UPLOAD_MAX_PAGES,
                                   max_chars=max_chars,
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
