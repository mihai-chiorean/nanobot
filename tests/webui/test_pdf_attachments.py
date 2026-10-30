import json
import re
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from nanobot.webui.pdf_attachments import (
    ATTACHMENT_CONTEXT_TOKEN_SHARE,
    PDF_TEXT_TIMEOUT_SECONDS,
    UPLOAD_MAX_CHARS,
    UPLOAD_MAX_PAGES,
    attachment_token_budget,
    attachment_token_estimate,
    parse_attachment_cards,
    pdf_text,
    prepare_pdfs,
)
from nanobot.webui.transcript import append_transcript_object, build_webui_thread_response


@pytest.fixture(autouse=True)
def isolate_attachment_transcripts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Earlier agent tests may select an active config root. Attachment history
    # fixtures must not inherit that root or authorize a stale test attachment.
    monkeypatch.setattr("nanobot.config.paths.get_data_dir", lambda: tmp_path / "data")


@pytest.fixture(autouse=True)
def deterministic_tool_result_config(monkeypatch: pytest.MonkeyPatch) -> None:
    # Unlock page budget follows the configured max_tool_result_chars; pin the
    # default so machine-level ~/.nanobot/config.json cannot move the goalposts.
    from nanobot.config.schema import Config

    monkeypatch.setattr("nanobot.config.loader.load_config", lambda *a, **k: Config())
    # Admission decisions read real token estimates; a fake encoding left in
    # the cached _get_token_encoding by another module's tiktoken fakes would
    # silently change which PDFs fit the budget, so start from the real one.
    from nanobot.utils import helpers as _helpers

    _helpers._get_token_encoding.cache_clear()
    _helpers._TOOLS_TOKEN_CACHE.clear()


def make_pdf(path: Path, text: str = "", *, encrypted: bool = False, algorithm: str = "AES-256") -> None:
    writer = PdfWriter()
    page = writer.add_blank_page(width=200, height=200)
    if text:
        stream = DecodedStreamObject()
        stream.set_data(f"BT /F1 12 Tf 10 10 Td ({text}) Tj ET".encode())
        font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                                 NameObject("/Subtype"): NameObject("/Type1"),
                                 NameObject("/BaseFont"): NameObject("/Helvetica")})
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
        page[NameObject("/Contents")] = writer._add_object(stream)
    if encrypted:
        writer.encrypt("password", algorithm=algorithm)
    writer.write(path)


def make_long_pdf(path: Path, pages: int, *, page_chars: int, encrypted: bool = False) -> None:
    """Build a multi-page PDF whose marker text sits at the END of each page.

    Marker-last placement makes any silently dropped page tail visible: the
    page is only counted when its trailing marker was actually returned.
    """
    writer = PdfWriter()
    filler = "the quick brown fox jumps over the lazy dog clause seventeen "
    for index in range(1, pages + 1):
        page = writer.add_blank_page(width=200, height=900)
        body = (filler * (page_chars // len(filler) + 1))[:page_chars]
        ops = ["BT /F1 12 Tf 10 860 Td", f"({body}) Tj 0 -14 Td",
               f"(PAGE-{index:03d}-MARKER) Tj 0 -14 Td", "ET"]
        stream = DecodedStreamObject()
        stream.set_data(" ".join(ops).encode())
        font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                                 NameObject("/Subtype"): NameObject("/Type1"),
                                 NameObject("/BaseFont"): NameObject("/Helvetica")})
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
        page[NameObject("/Contents")] = writer._add_object(stream)
    if encrypted:
        writer.encrypt("fixture-password")
    writer.write(path)


@pytest.mark.asyncio
async def test_whole_document_loads_without_partial_notice(tmp_path: Path) -> None:
    # Owner scope addition + MIT-1967 review: on the served model's 262k window
    # the 147-page fixture (~446k chars) is loaded whole, so no page is hidden
    # and no partial notice, path hint or paging instruction may appear.
    assert UPLOAD_MAX_PAGES == 200 and UPLOAD_MAX_CHARS == 600_000
    path = tmp_path / "lease.pdf"
    make_long_pdf(path, 147, page_chars=3000)
    note, statuses = await prepare_pdfs([str(path)], ["lease.pdf"], context_window_tokens=262_144)
    assert statuses[0]["error"] is None
    assert "partial" not in statuses[0]
    assert "read_file" not in note
    assert "pages=" not in note
    assert "read every remaining page range" not in note
    assert "were read" not in note
    for index in range(1, 148):
        assert f"PAGE-{index:03d}-MARKER" in note, f"page {index} missing from the whole-document load"
    assert len(note) > 400_000


@pytest.mark.asyncio
async def test_over_cap_pdf_preview_is_labelled_and_points_to_the_full_document(tmp_path: Path) -> None:
    path = tmp_path / "statute.pdf"
    make_long_pdf(path, 250, page_chars=3000)  # ~755k chars: past every budget
    note, statuses = await prepare_pdfs([str(path)], ["statute.pdf"], context_window_tokens=262_144)
    status = statuses[0]
    assert status["error"] is None
    notice = re.fullmatch(
        r"Read pages 1–(\d+) of (\d+); Ziggy will read the rest on request",
        status.get("partial", ""),
    )
    assert notice is not None, status
    last_full, total = int(notice.group(1)), int(notice.group(2))
    assert total == 250
    assert 0 < last_full < 250
    assert f"the document has {total} pages in total" in note
    assert f"pages 1–{last_full} of statute.pdf" in note
    assert str(path) in note
    page_range = re.search(r"pages='(\d+)-(\d+)'", note)
    assert page_range is not None
    assert int(page_range.group(1)) == last_full + 1
    assert int(page_range.group(2)) == min(total, last_full + 20)
    assert f"PAGE-{last_full:03d}-MARKER" in note
    assert f"PAGE-{last_full + 1:03d}-MARKER" not in note  # cut page dropped whole, re-read on paging
    assert "read every remaining page range" in note
    assert "never claim to have read pages" in note
    # The session-sized budget is the binding constraint, and the framing text
    # never lets the note balloon past it. A dropped cut page must not leave
    # the collector's "... (truncated at N chars)" suffix behind (item 5).
    assert 400_000 < len(note)
    assert attachment_token_estimate(note) <= attachment_token_budget(262_144) + 5_000
    assert "(truncated at" not in note


def _assert_request_fits(note: str, window: int) -> None:
    """The turn's request must survive ensure_request_fits, which raises
    rather than trims (MIT-1967 review blocking item 1)."""
    from unittest.mock import MagicMock

    from nanobot.agent.context_governance import ContextGovernanceConfig, ContextGovernor

    config = ContextGovernanceConfig(
        provider=object(), model="test-model", tools=MagicMock(), workspace=None,
        session_key="websocket:budget", max_tool_result_chars=16_000,
        context_window_tokens=window,
    )
    ContextGovernor().ensure_request_fits(
        config,
        [{"role": "user", "content": "Review the documents.\n\n[Attachment processing note]\n" + note}],
        tool_definitions=None,
    )


@pytest.mark.asyncio
async def test_budget_sizes_whole_load_on_262k_and_partial_on_65k(tmp_path: Path) -> None:
    # Review acceptance: a 147-page PDF loads whole at 262k and is partial at
    # 65k, and neither case may raise ContextWindowExceededError. Admission is
    # measured with the governor's own estimator (re-review item 1), so the
    # budget is expressed in estimated tokens, not chars.
    path = tmp_path / "lease.pdf"
    make_long_pdf(path, 147, page_chars=3000)
    note, statuses = await prepare_pdfs([str(path)], ["lease.pdf"], context_window_tokens=65_536)
    assert attachment_token_budget(65_536) == int(65_536 * ATTACHMENT_CONTEXT_TOKEN_SHARE)
    status = statuses[0]
    assert status["error"] is None
    notice = re.fullmatch(
        r"Read pages 1–(\d+) of (\d+); Ziggy will read the rest on request",
        status.get("partial", ""),
    )
    assert notice is not None, status
    last_full, total = int(notice.group(1)), int(notice.group(2))
    assert total == 147 and 0 < last_full < 147
    assert f"pages 1–{last_full} of lease.pdf" in note
    assert str(path) in note
    assert f"PAGE-{last_full + 1:03d}-MARKER" not in note
    assert attachment_token_estimate(note) <= attachment_token_budget(65_536) + 5_000
    _assert_request_fits(note, 65_536)
    whole_note, whole_statuses = await prepare_pdfs(
        [str(path)], ["lease.pdf"], context_window_tokens=262_144,
    )
    assert "partial" not in whole_statuses[0]
    assert "PAGE-147-MARKER" in whole_note
    _assert_request_fits(whole_note, 262_144)


@pytest.mark.asyncio
async def test_byte_fallback_estimator_never_over_admits_at_any_preset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Re-review blocking item 1: the budget must use the same estimator the
    # governor uses in ensure_request_fits, including its byte-count fallback.
    # With the fallback forced on, an English PDF in a 65,536-token session
    # used to admit ~118k estimated tokens against a 56k governor budget and
    # raised ContextWindowExceededError; same at 200k and 262k.
    import nanobot.utils.helpers as helpers

    def broken_encoding() -> object:
        raise RuntimeError("estimator fallback forced by test")

    monkeypatch.setattr(helpers, "_get_token_encoding", broken_encoding)
    path = tmp_path / "lease.pdf"
    make_long_pdf(path, 147, page_chars=3000)
    for window in (65_536, 200_000, 262_144):
        note, statuses = await prepare_pdfs(
            [str(path)], ["lease.pdf"], context_window_tokens=window)
        assert statuses[0]["error"] is None
        assert "partial" in statuses[0], f"byte-estimated 446k payload cannot fit {window}"
        assert attachment_token_estimate(note) <= attachment_token_budget(window) + 5_000
        _assert_request_fits(note, window)


@pytest.mark.asyncio
async def test_six_large_pdfs_share_the_budget_and_later_ones_are_partial(
    tmp_path: Path,
) -> None:
    # Review acceptance: six attachments together stay inside the one
    # aggregate budget — the 600k-per-file era let six files add 600k each.
    paths: list[str] = []
    names: list[str] = []
    for index in range(6):
        path = tmp_path / f"doc{index}.pdf"
        make_long_pdf(path, 60, page_chars=3000)  # ~182k chars each
        paths.append(str(path))
        names.append(f"doc{index}.pdf")
    note, statuses = await prepare_pdfs(paths, names, context_window_tokens=262_144)
    budget = attachment_token_budget(262_144)
    assert attachment_token_estimate(note) <= budget + 6 * 600  # framing bounded per attachment
    for status in statuses:
        assert status["error"] is None
    # ~33k estimated tokens per file against a ~118k budget: the first files
    # fit whole in order, then the budget-cut file and the deferred tail carry
    # the partial card, but the message stays inside its budget.
    assert "partial" not in statuses[0]
    assert "partial" not in statuses[1]
    assert "partial" not in statuses[2]
    for status, name in zip(statuses[3:], names[3:]):
        assert status.get("partial"), (status, name)
    assert "read every remaining page range" in note
    _assert_request_fits(note, 262_144)


@pytest.mark.asyncio
async def test_page_beyond_budget_reports_zero_pages_not_one_to_zero(tmp_path: Path) -> None:
    # Review item 4: when even page 1 does not fit, the note/card must not say
    # "pages 1–0". The continuation starts after the disclosed oversized page,
    # as read_file's own walk does (re-review item 4).
    path = tmp_path / "wide.pdf"
    make_long_pdf(path, 3, page_chars=3000)
    note, statuses = await prepare_pdfs([str(path)], ["wide.pdf"], context_window_tokens=100)
    assert attachment_token_budget(100) == int(100 * ATTACHMENT_CONTEXT_TOKEN_SHARE)
    notice = statuses[0].get("partial", "")
    assert notice == "Read 0 of 3 pages; Ziggy will read the rest on request"
    assert "pages 1–0" not in note
    assert "No page of wide.pdf (untrusted document text) was included in full above" in note
    assert "pages='2-3'" in note


def test_attachment_token_budget_falls_back_to_smallest_preset() -> None:
    # Re-review blocking item 2: an unresolvable session window must be
    # treated as the smallest preset the settings offer (65,536), never the
    # 200,000 schema default — over-assuming the window is what let the
    # governor reject the whole turn.
    assert attachment_token_budget(None) == attachment_token_budget(65_536)
    assert attachment_token_budget(None) == int(65_536 * ATTACHMENT_CONTEXT_TOKEN_SHARE)
    assert attachment_token_budget(-5) == attachment_token_budget(None)
    assert attachment_token_budget(262_144) == int(262_144 * ATTACHMENT_CONTEXT_TOKEN_SHARE)


@pytest.mark.asyncio
async def test_unknown_window_prepares_like_the_smallest_preset(tmp_path: Path) -> None:
    # The same 147-page fixture that loads whole at 262k must stay partial —
    # and inside the governor — when the gateway could not resolve the
    # session's window (context_window_tokens=None).
    path = tmp_path / "lease.pdf"
    make_long_pdf(path, 147, page_chars=3000)
    note, statuses = await prepare_pdfs([str(path)], ["lease.pdf"])
    assert "partial" in statuses[0]  # a 200k default would have loaded it whole
    assert attachment_token_estimate(note) <= attachment_token_budget(None) + 5_000
    _assert_request_fits(note, 65_536)


@pytest.mark.asyncio
async def test_budget_spent_attachments_get_deferred_note_and_card(tmp_path: Path) -> None:
    # Once the aggregate budget is spent, later PDFs are not extracted at all;
    # they still get the note, stored path, next range and partial card.
    first = tmp_path / "first.pdf"
    second = tmp_path / "second.pdf"
    third = tmp_path / "third.pdf"
    make_long_pdf(first, 250, page_chars=3000)  # ~755k chars: exhausts the token budget
    make_long_pdf(second, 60, page_chars=3000)  # budget-cut continuation candidate
    make_long_pdf(third, 40, page_chars=3000)   # deferred outright
    note, statuses = await prepare_pdfs(
        [str(first), str(second), str(third)],
        ["first.pdf", "second.pdf", "third.pdf"],
        context_window_tokens=262_144,
    )
    assert re.fullmatch(r"Read pages 1–\d+ of 250; Ziggy will read the rest on request",
                        statuses[0]["partial"])
    assert re.fullmatch(r"Read pages 1–\d+ of 60; Ziggy will read the rest on request",
                        statuses[1]["partial"])
    assert statuses[2]["partial"] == "Not yet read; Ziggy will read it on request"
    assert "No pages of third.pdf (untrusted document text) are shown above" in note
    assert "attachment budget" in note
    assert str(third) in note
    assert "pages='1-20'" in note
    assert note.count("Attached PDF third.pdf") == 0  # nothing of it was extracted
    _assert_request_fits(note, 262_144)


@pytest.mark.asyncio
async def test_deferred_encrypted_attachment_still_gets_locked_flow(tmp_path: Path) -> None:
    # Re-review blocking item 3: once the budget is spent, a later encrypted
    # PDF used to skip parsing entirely — no locked_reason, no unlock
    # registration, and its note sent the model to read_file, which cannot
    # decrypt. The cheap encryption check must still run for deferred files.
    from nanobot.webui.attachment_unlock import AttachmentVault

    first = tmp_path / "first.pdf"
    second = tmp_path / "second.pdf"
    sealed = tmp_path / "sealed.pdf"
    make_long_pdf(first, 250, page_chars=3000)  # exhausts the 262k token budget
    make_long_pdf(second, 60, page_chars=3000)  # budget-cut: drains the remainder
    make_pdf(sealed, "Deferred secret text", encrypted=True)
    note, statuses = await prepare_pdfs(
        [str(first), str(second), str(sealed)],
        ["first.pdf", "second.pdf", "sealed.pdf"],
        context_window_tokens=262_144,
    )
    assert "partial" not in statuses[2]  # locked flow, not the deferred read_file note
    assert statuses[2]["error"] == "This PDF is password protected. Ask the user for its password."
    assert statuses[2]["locked_reason"] == "password_required"
    assert "No pages of sealed.pdf" not in note  # no read_file pointer for an encrypted file
    assert f"read_file path={sealed}" not in note
    assert "Attachment read failure for sealed.pdf" in note
    assert "Use unlock_attachment for a supplied PDF password" in note
    # The normal locked flow: registration, note listing, and a real unlock.
    vault = AttachmentVault(tmp_path / "vault-deferred")
    vault.register("chat", "user", statuses, [str(first), str(second), str(sealed)], tmp_path)
    attachment_id = statuses[2].get("attachment_id")
    assert attachment_id, statuses[2]
    assert "sealed.pdf" in vault.note("chat")
    unlocked = await vault.unlock("chat", attachment_id, "password")
    assert "Deferred secret text" in unlocked
    _assert_request_fits(note, 262_144)


@pytest.mark.asyncio
async def test_pdf_text_default_timeout_covers_two_hundred_page_extraction() -> None:
    # 200 pages of pypdf extraction can exceed the old 15 s budget on the
    # Spark; the default handed to wait_for must be the raised one.
    process = AsyncMock()
    process.returncode = 0
    process.communicate = AsyncMock(
        return_value=(json.dumps({"text": "ok", "error": None, "preview": None}).encode(), b""))
    seen: dict[str, object] = {}
    async def capture_wait_for(awaitable, timeout=None):
        seen["timeout"] = timeout
        return await awaitable
    with patch("asyncio.create_subprocess_exec", return_value=process), \
            patch("asyncio.wait_for", side_effect=capture_wait_for):
        text, error, preview = await pdf_text(Path("report.pdf"))
    assert (text, error, preview) == ("ok", None, None)
    assert seen["timeout"] == PDF_TEXT_TIMEOUT_SECONDS == 60


@pytest.mark.asyncio
async def test_600k_note_survives_sanitize_and_session_replay(tmp_path: Path) -> None:
    # Review step 3: the note is appended server-side, after the 64 KB ingress
    # text check, so the stages it actually crosses are sanitize_input, the
    # bus, Session persistence and get_history replay. None may cut it.
    from nanobot.session.manager import SessionManager
    from nanobot.utils.security import sanitize_input

    path = tmp_path / "legal.pdf"
    make_long_pdf(path, 147, page_chars=3000)
    note, statuses = await prepare_pdfs([str(path)], ["legal.pdf"], context_window_tokens=262_144)
    assert "partial" not in statuses[0]
    dispatch = "Review the attached document.\n\n[Attachment processing note]\n" + note
    sanitized, was_injection = sanitize_input(dispatch)
    assert not was_injection
    assert sanitized == dispatch  # legal text passes the injection guard byte-identical
    sessions = SessionManager(tmp_path / "ws", sessions_root=tmp_path / "sessions")
    session = sessions.get_or_create("websocket:big-pdf")
    session.add_message("user", dispatch)
    sessions.save(session)
    reloaded = SessionManager(tmp_path / "ws", sessions_root=tmp_path / "sessions")
    replayed = reloaded.get_or_create("websocket:big-pdf").get_history()
    assert replayed[-1]["content"] == dispatch
    for index in (1, 74, 147):
        assert f"PAGE-{index:03d}-MARKER" in replayed[-1]["content"]


@pytest.mark.asyncio
async def test_large_pdf_frame_reaches_model_whole_and_stays_out_of_transcript(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nanobot.channels.websocket.tests.test_websocket_envelope_media import (
        _data_url,
        _make_channel,
    )

    monkeypatch.setattr("nanobot.config.paths.get_data_dir", lambda: tmp_path / "data")
    media = tmp_path / "media"
    media.mkdir()
    path = tmp_path / "lease.pdf"
    make_long_pdf(path, 147, page_chars=3000)
    channel = _make_channel(session_context_window=lambda _key: 262_144)
    origin = AsyncMock()
    with patch("nanobot.webui.media_gateway.get_media_dir", return_value=media):
        await channel._dispatch_envelope(origin, "client", {
            "type": "message", "chat_id": "big-pdf", "webui": True,
            "content": "Review the whole document",
            "media": [{"name": "lease.pdf", "data_url": _data_url("application/pdf", path.read_bytes())}],
        })
    content = channel._handle_message.call_args.kwargs["content"]
    assert len(content) > 400_000  # the pre-scope cut point was ~60k (page ~20)
    assert "PAGE-147-MARKER" in content
    # Transcript rows (UI replay) keep only the user-visible text; the model
    # note never passes through the per-turn transcript byte limits.
    row = build_webui_thread_response("websocket:big-pdf")["messages"][0]
    assert row["content"] == "Review the whole document"
    assert "PAGE-001-MARKER" not in str(row)


@pytest.mark.asyncio
async def test_short_pdf_has_no_partial_notice_or_path_hint(tmp_path: Path) -> None:
    path = tmp_path / "short.pdf"
    make_long_pdf(path, 2, page_chars=100)
    note, statuses = await prepare_pdfs([str(path)], ["short.pdf"])
    assert "partial" not in statuses[0]
    assert statuses[0]["error"] is None
    assert "read_file" not in note
    assert "pages=" not in note
    assert "PAGE-001-MARKER" in note and "PAGE-002-MARKER" in note


def test_parse_attachment_cards_keeps_partial_notice_only_when_usable() -> None:
    cards = parse_attachment_cards([
        {"name": "a.pdf", "partial": "Read pages 1–9 of 147; Ziggy will read the rest on request"},
        {"name": "b.pdf", "partial": "x" * 600},
        {"name": "c.pdf", "partial": 42},
        {"name": "d.pdf", "partial": ""},
    ])
    assert cards[0]["partial"] == "Read pages 1–9 of 147; Ziggy will read the rest on request"
    assert len(cards[1]["partial"]) == 256
    assert "partial" not in cards[2]
    assert "partial" not in cards[3]


@pytest.mark.parametrize("projection", ["messages", "events"])
def test_partial_notice_survives_history(tmp_path, monkeypatch, projection):
    monkeypatch.setattr("nanobot.config.paths.get_data_dir", lambda: tmp_path)
    append_transcript_object("websocket:pdf-partial", {
        "event": "user", "chat_id": "pdf-partial", "text": "",
        "attachments": [{"name": "lease.pdf", "error": None,
                         "partial": "Read pages 1–19 of 147; Ziggy will read the rest on request"}],
    })
    response = build_webui_thread_response("websocket:pdf-partial", projection=projection)
    rows = response["events"] if projection == "events" else response["messages"]
    media = rows[0]["media_urls"] if projection == "events" else rows[0]["media"]
    assert media[0]["partial"] == "Read pages 1–19 of 147; Ziggy will read the rest on request"
    assert media[0]["error"] is None


@pytest.mark.asyncio
async def test_text_pdf_is_fed_to_model_with_filename(tmp_path: Path) -> None:
    path = tmp_path / "report.pdf"
    make_pdf(path, "Quarterly revenue is 42")
    note, statuses = await prepare_pdfs([str(path)])
    assert "report.pdf" in note
    assert "Quarterly revenue is 42" in note
    assert statuses == [{"name": "report.pdf", "error": None}]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["scan", "corrupt", "encrypted"])
async def test_unreadable_pdf_has_a_named_actionable_error(tmp_path: Path, kind: str) -> None:
    path = tmp_path / "report.pdf"
    if kind == "corrupt":
        path.write_bytes(b"%PDF-corrupt")
    else:
        make_pdf(path, encrypted=kind == "encrypted")
    note, statuses = await prepare_pdfs([str(path)])
    assert statuses[0]["name"] == "report.pdf"
    assert statuses[0]["error"]
    if kind != "encrypted":
        assert "screenshot" in note
    assert "Explain each attachment read failure" in note
    if kind == "scan":
        assert "no extractable text" in note


@pytest.mark.asyncio
async def test_stuck_parser_is_killed_and_returns_an_error() -> None:
    process = AsyncMock()
    process.returncode = None
    process.kill = lambda: setattr(process, "returncode", -9)
    async def stuck(*args):
        import asyncio
        await asyncio.sleep(60)
    process.communicate.side_effect = stuck
    with patch("asyncio.create_subprocess_exec", return_value=process):
        text, error, preview = await pdf_text(Path("report.pdf"), timeout=0.01)
    assert text == ""
    assert "too long" in error
    assert preview is None
    assert process.returncode == -9
    process.wait.assert_awaited_once()


@pytest.mark.parametrize("projection", ["messages", "events"])
def test_attachment_error_survives_history_without_stored_bytes(tmp_path, monkeypatch, projection):
    monkeypatch.setattr("nanobot.config.paths.get_data_dir", lambda: tmp_path)
    append_transcript_object("websocket:pdf", {
        "event": "user", "chat_id": "pdf", "text": "",
        "attachments": [{"name": "report.pdf", "error": "Upload exceeded 10 MB."}],
    })
    response = build_webui_thread_response("websocket:pdf", projection=projection)
    rows = response["events"] if projection == "events" else response["messages"]
    media = rows[0]["media_urls"] if projection == "events" else rows[0]["media"]
    assert media == [{"name": "report.pdf", "error": "Upload exceeded 10 MB."}]


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["Quarterly revenue is 42", ""])
async def test_pdf_frame_reaches_model_and_reloads_named_card(tmp_path, monkeypatch, text):
    from nanobot.channels.websocket.tests.test_websocket_envelope_media import (
        _data_url,
        _make_channel,
    )
    from nanobot.runtime_context import RUNTIME_CONTEXT_INPUT_META

    monkeypatch.setattr("nanobot.config.paths.get_data_dir", lambda: tmp_path / "data")
    media = tmp_path / "media"
    media.mkdir()
    path = tmp_path / "report.pdf"
    make_pdf(path, text)
    channel = _make_channel()
    origin = AsyncMock()
    with patch("nanobot.webui.media_gateway.get_media_dir", return_value=media):
        await channel._dispatch_envelope(origin, "client", {
            "type": "message", "chat_id": "pdf-round-trip", "webui": True, "content": "",
            "media": [{"name": "report.pdf", "data_url": _data_url("application/pdf", path.read_bytes())}],
        })
    call = channel._handle_message.call_args.kwargs
    assert "report.pdf" in call["content"]
    assert call["metadata"][RUNTIME_CONTEXT_INPUT_META][0].source == "attachment_processing"
    assert text in call["content"]
    for projection in ["messages", "events"]:
        response = build_webui_thread_response("websocket:pdf-round-trip", projection=projection)
        row = response["events"][0] if projection == "events" else response["messages"][0]
        cards = row["media_urls"] if projection == "events" else row["media"]
        assert cards[0]["name"] == "report.pdf"
        assert bool(cards[0]["error"]) == (not text)


@pytest.mark.asyncio
@pytest.mark.parametrize("algorithm", ["RC4-128", "AES-128", "AES-256"])
async def test_password_round_trip_and_persistent_attempt_cap(tmp_path: Path, algorithm: str) -> None:
    from nanobot.webui.attachment_unlock import AttachmentVault

    path = tmp_path / "protected.pdf"
    make_pdf(path, "Fixture document text", encrypted=True, algorithm=algorithm)
    note, cards = await prepare_pdfs([str(path)])
    assert cards[0]["locked_reason"] == "password_required"
    vault = AttachmentVault(tmp_path / "tenant-a")
    vault.register("chat", "original-user", cards, [str(path)], tmp_path)
    attachment_id = cards[0]["attachment_id"]
    assert "unlock_attachment" in vault.note("chat")
    assert "not found" in await vault.unlock("other-chat", attachment_id, "password")
    foreign = AttachmentVault(tmp_path / "tenant-b")
    assert "not found" in await foreign.unlock("chat", attachment_id, "password")
    assert "wrong" in await vault.unlock("chat", attachment_id, "incorrect")
    unlocked = await vault.unlock("chat", attachment_id, "password")
    assert "Fixture document text" in unlocked
    assert not vault.note("chat")
    assert '"password":' not in vault.path.read_text()

    _, capped_cards = await prepare_pdfs([str(path)])
    vault.register("chat", "original-user", capped_cards, [str(path)], tmp_path)
    capped_id = capped_cards[0]["attachment_id"]
    for _ in range(3):  # Both earlier decrypt calls consumed the shared budget.
        assert "wrong" in await vault.unlock("chat", capped_id, "incorrect")
    reloaded = AttachmentVault(tmp_path / "tenant-a")
    assert "5 password attempts" in await reloaded.unlock("chat", capped_id, "password")


@pytest.mark.asyncio
async def test_worker_rpc_matches_connection_and_returns_text(tmp_path: Path) -> None:
    import asyncio

    from nanobot.webui.attachment_unlock import AttachmentVault

    vault = AttachmentVault(tmp_path)
    cards = [{"name": "protected.pdf", "attachment_id": "att_" + "a" * 32,
              "locked_reason": "password_required"}]
    vault.register("chat", "user", cards, [], tmp_path)
    assert not vault.records  # An envelope claim alone grants no capability.
    append_transcript_object("websocket:chat", {
        "event": "user", "chat_id": "chat", "client_message_id": "user",
        "attachments": cards,
    })
    vault.register_history("chat", "websocket:chat")
    vault = AttachmentVault(tmp_path)
    connection = object()
    async def send(event, **fields):
        assert event == "attachment.unlock"
        response = {"request_id": fields["request_id"], "input": {"text": "Unlocked text"}}
        vault.resolve(object(), response)
        assert not vault.pending[fields["request_id"]][1].done()
        vault.resolve(connection, response)
    vault.bridges[("chat", "websocket:chat")] = (connection, send)
    assert "not found" in await vault.unlock("other-chat", cards[0]["attachment_id"], "password")
    assert "not found" in await vault.unlock("chat", cards[0]["attachment_id"], "password",
                                             session="temporary:chat")
    result = await asyncio.wait_for(vault.unlock("chat", cards[0]["attachment_id"], "password"), 1)
    assert "Unlocked text" in result


@pytest.mark.asyncio
async def test_reuploads_share_five_attempts_across_chats_and_restarts(tmp_path: Path) -> None:
    from nanobot.webui.attachment_unlock import AttachmentVault

    first = tmp_path / "first.pdf"
    second = tmp_path / "renamed.pdf"
    make_pdf(first, "Private text", encrypted=True)
    second.write_bytes(first.read_bytes())
    vault = AttachmentVault(tmp_path / "tenant")
    _, cards = await prepare_pdfs([str(first)])
    vault.register("chat", "user", cards, [str(first)], tmp_path)
    for _ in range(3):
        assert "wrong" in await vault.unlock("chat", cards[0]["attachment_id"], "incorrect")

    vault = AttachmentVault(tmp_path / "tenant")
    _, reuploaded = await prepare_pdfs([str(second)])
    vault.register("another-chat", "user", reuploaded, [str(second)], tmp_path)
    assert cards[0]["attachment_id"] != reuploaded[0]["attachment_id"]
    for _ in range(2):
        assert "wrong" in await vault.unlock("another-chat", reuploaded[0]["attachment_id"], "incorrect")

    vault = AttachmentVault(tmp_path / "tenant")
    with patch("nanobot.webui.attachment_unlock.pdf_text", new_callable=AsyncMock) as parser:
        assert "5 password attempts" in await vault.unlock("chat", cards[0]["attachment_id"], "password")
        assert "5 password attempts" in await vault.unlock("another-chat", reuploaded[0]["attachment_id"], "password")
        parser.assert_not_awaited()
    foreign = AttachmentVault(tmp_path / "other-tenant")
    _, foreign_cards = await prepare_pdfs([str(second)])
    foreign.register("chat", "user", foreign_cards, [str(second)], tmp_path)
    assert "Private text" in await foreign.unlock("chat", foreign_cards[0]["attachment_id"], "password")


@pytest.mark.asyncio
async def test_original_registry_attempts_are_migrated(tmp_path: Path) -> None:
    import json

    from nanobot.webui.attachment_unlock import AttachmentVault

    path = tmp_path / "protected.pdf"
    make_pdf(path, encrypted=True)
    registry = tmp_path / "attachment-locks.json"
    registry.write_text(json.dumps({f"local_{i}": {
        "chat": "chat", "origin": "user", "name": "protected.pdf", "path": str(path),
        "attempts": attempts, "locked": True,
    } for i, attempts in enumerate([3, 2])}))
    vault = AttachmentVault(tmp_path)
    _, cards = await prepare_pdfs([str(path)])
    vault.register("chat", "user", cards, [str(path)], tmp_path)
    assert "5 password attempts" in await vault.unlock("chat", cards[0]["attachment_id"], "password")
    assert "5 password attempts" in await AttachmentVault(tmp_path).unlock("chat", "local_0", "password")


@pytest.mark.asyncio
@pytest.mark.parametrize("forgery", ["unknown_id", "lock_reason", "other_chat", "local_reason"])
async def test_client_cards_cannot_authorize_unlocks(tmp_path: Path, forgery: str) -> None:
    from nanobot.channels.websocket.tests.test_websocket_envelope_media import _make_channel
    from nanobot.session.manager import SessionManager
    from nanobot.webui.attachment_unlock import attachment_vault

    sessions = SessionManager(tmp_path / "workspace", sessions_root=tmp_path / "sessions")
    channel = _make_channel(sessions)
    vault = attachment_vault(sessions.sessions_dir)
    attachment_id = "att_" + "a" * 32
    card = {"name": "protected.pdf", "attachment_id": attachment_id, "error": None,
            "locked_reason": "password_required"}
    if forgery in {"lock_reason", "other_chat"}:
        historical = dict(card)
        historical_chat = "another-chat" if forgery == "other_chat" else "chat"
        if forgery == "lock_reason":
            historical.pop("locked_reason")
        append_transcript_object(f"websocket:{historical_chat}", {
            "event": "user", "chat_id": historical_chat, "attachments": [historical],
        })
        vault.register_history(historical_chat, f"websocket:{historical_chat}")
    if forgery == "local_reason":
        card.pop("attachment_id")
    connection = AsyncMock()
    await channel._dispatch_envelope(connection, "client", {
        "type": "message", "chat_id": "chat", "content": "Read this PDF",
        "display_content": "Read this PDF", "webui": True,
        "attachment_cards": [card], "attachment_unlock_bridge": True,
    })
    connection.send.reset_mock()
    assert "not found" in await vault.unlock("chat", attachment_id, "password")
    assert not vault.note("chat")
    connection.send.assert_not_awaited()
    if forgery == "other_chat":
        assert vault.records[attachment_id]["chat"] == "another-chat"


@pytest.mark.asyncio
async def test_password_only_turn_registers_work_attachment_from_prior_history(tmp_path: Path) -> None:
    from nanobot.channels.websocket.tests.test_websocket_envelope_media import _make_channel
    from nanobot.runtime_context import RUNTIME_CONTEXT_INPUT_META
    from nanobot.session.manager import SessionManager
    from nanobot.webui.attachment_unlock import attachment_vault

    sessions = SessionManager(tmp_path / "workspace", sessions_root=tmp_path / "sessions")
    channel = _make_channel(sessions)
    attachment_id = "att_" + "a" * 32
    await channel._dispatch_envelope(AsyncMock(), "client", {
        "type": "message", "chat_id": "chat", "content": "Read the PDF",
        "display_content": "Read the PDF", "webui": True, "attachment_cards": [{
            "name": "protected.pdf", "attachment_id": attachment_id,
            "error": "Password protected", "locked_reason": "password_required",
        }],
    })
    assert not attachment_vault(sessions.sessions_dir).records
    await channel._dispatch_envelope(AsyncMock(), "client", {
        "type": "message", "chat_id": "chat", "content": "Here is the password",
        "attachment_unlock_bridge": True,
    })
    record = attachment_vault(sessions.sessions_dir).records[attachment_id]
    assert record["session"] == "websocket:chat"
    note = channel._handle_message.call_args.kwargs["metadata"][RUNTIME_CONTEXT_INPUT_META][0].content
    assert attachment_id in note


@pytest.mark.asyncio
@pytest.mark.parametrize("chat,session,channel,metadata,allowed", [
    ("chat", "websocket:chat", "websocket", {}, True),
    ("other-chat", "websocket:other-chat", "websocket", {}, False),
    ("chat", "temporary:chat", "websocket", {}, False),
    ("chat", "websocket:chat", "telegram", {}, False),
    ("chat", "websocket:chat", "websocket", {"shared_room": True}, False),
])
async def test_unlock_tool_uses_request_chat_and_session(tmp_path, chat, session, channel, metadata, allowed):
    from nanobot.agent.tools.context import RequestContext, request_context
    from nanobot.agent.tools.unlock_attachment import UnlockAttachmentTool
    from nanobot.webui.attachment_unlock import AttachmentVault

    vault = AttachmentVault(tmp_path)
    vault.records["local_test"] = {
        "chat": "chat", "session": "websocket:chat", "name": "protected.pdf",
        "locked": False, "text": "Private text",
    }
    with request_context(RequestContext(channel=channel, chat_id=chat, session_key=session,
                                        metadata=metadata)):
        result = await UnlockAttachmentTool(vault).execute("local_test", "password")
    assert ("Private text" in result) == allowed
    if not allowed:
        assert "not found" in result


@pytest.mark.parametrize("projection", ["messages", "events"])
def test_unlocked_chip_survives_history(tmp_path, monkeypatch, projection):
    from nanobot.webui.transcript import update_attachment_display_state
    monkeypatch.setattr("nanobot.config.paths.get_data_dir", lambda: tmp_path)
    append_transcript_object("websocket:pdf", {
        "event": "user", "chat_id": "pdf", "text": "",
        "attachments": [{"name": "report.pdf", "attachment_id": "local_123",
                         "error": "Password protected", "locked_reason": "password_required"}],
    })
    update_attachment_display_state("websocket:pdf", "local_123")
    response = build_webui_thread_response("websocket:pdf", projection=projection)
    assert "password_required" not in str(response)
    assert "report.pdf" in str(response)


def test_unlock_password_redacted_in_tool_records():
    from nanobot.agent.tools.audit import _sanitise_arguments
    from nanobot.providers.base import ToolCallRequest
    from nanobot.utils.progress_events import build_tool_event_start_payload
    from nanobot.utils.tool_hints import format_tool_hints
    call = ToolCallRequest(id="id", name="unlock_attachment",
                           arguments={"password": "fixture-secret", "attachment_id": "local_123"})
    assert "fixture-secret" not in str(call.to_openai_tool_call())
    assert "fixture-secret" not in str(build_tool_event_start_payload(call))
    assert "fixture-secret" not in format_tool_hints([call])
    assert "fixture-secret" not in str(_sanitise_arguments(call.arguments))
    assert call.arguments["password"] == "fixture-secret"


@pytest.mark.asyncio
@pytest.mark.parametrize("filename", ["rc4-r4.pdf", "rc4-r4-legacy-info.pdf"])
async def test_rc4_r4_legacy_metadata_subprocess_end_to_end(tmp_path: Path, filename: str) -> None:
    """Same synthetic encrypted fixture as Work; exercise the real subprocess."""
    from nanobot.webui.attachment_unlock import AttachmentVault

    path = Path(__file__).parents[1] / "fixtures" / "pdf" / filename
    note, cards = await prepare_pdfs([str(path)])
    assert cards[0]["locked_reason"] == "password_required"
    assert "Never repeat" in note
    print("ingest: locked_reason=password_required (subprocess)")
    vault = AttachmentVault(tmp_path / "vault")
    vault.register("chat", "original", cards, [str(path)], path.parent)
    attachment_id = cards[0]["attachment_id"]
    result = await vault.unlock("chat", attachment_id, "incorrect-fixture-password")
    assert "password was wrong" in result
    assert vault.records[attachment_id]["locked"]
    print("wrong password: wrong_password; attachment remains locked (subprocess)")
    result = await vault.unlock("chat", attachment_id, "fixture-password")
    assert "RC4 R4 fixture document text" in result
    assert not vault.records[attachment_id]["locked"]
    assert "fixture-password" not in vault.path.read_text()
    print("correct password: ready; extracted_text=verified (subprocess)")


@pytest.mark.asyncio
async def test_unlocked_encrypted_pdf_walked_to_the_end_returns_every_marker_once(
    tmp_path: Path,
) -> None:
    # MIT-1967 review blocking item 3: paging an unlocked encrypted PDF must
    # reach the final page, return every marker exactly once, keep each page
    # read inside the default 16k tool-result budget, and never route the
    # reader through read_file (which cannot decrypt the stored file).
    from nanobot.webui.attachment_unlock import AttachmentVault, unlock_page_max_chars

    path = tmp_path / "sealed.pdf"
    make_long_pdf(path, 250, page_chars=3000, encrypted=True)
    _, cards = await prepare_pdfs([str(path)], ["sealed.pdf"])
    assert cards[0]["locked_reason"] == "password_required"
    vault = AttachmentVault(tmp_path / "vault")
    vault.register("chat", "user", cards, [str(path)], tmp_path)
    attachment_id = cards[0]["attachment_id"]

    result = await vault.unlock("chat", attachment_id, "fixture-password")
    assert not result.startswith("Error"), result
    results = [result]
    assert re.search(
        r"Read pages 1–\d+ of 250; Ziggy will read the rest on request",
        vault.records[attachment_id]["partial"],
    )
    assert "unlock_attachment attachment_id=" in result
    assert "read_file" not in result  # it cannot decrypt; the note must not suggest it
    assert unlock_page_max_chars() < 16_000  # leaves room for framing under the 16k config

    guard = 0
    while "Continue with unlock_attachment" in results[-1]:
        guard += 1
        assert guard < 100, "page walk did not terminate"
        match = re.search(r"pages='(\d+)-(\d+)'", results[-1])
        assert match is not None
        result = await vault.unlock(
            "chat", attachment_id, "fixture-password",
            pages=f"{match.group(1)}-{match.group(2)}",
        )
        assert not result.startswith("Error"), result
        assert len(result) < 16_000
        results.append(result)
    assert "PAGE-250-MARKER" in results[-1]  # the walk ended at the last page

    combined = "\n".join(results)
    for index in range(1, 251):
        assert combined.count(f"PAGE-{index:03d}-MARKER") == 1, index
    assert vault.attempts[next(iter(vault.attempts))] == 1  # page reads never consume attempts
    assert "fixture-password" not in vault.path.read_text()  # registry never stores it


@pytest.mark.asyncio
async def test_unlock_page_read_with_wrong_password_fails(tmp_path: Path) -> None:
    # Re-review item 7: the in-memory password cache is gone; the paging call
    # carries its own password and a wrong one must not silently serve the
    # range through a cached copy.
    from nanobot.webui.attachment_unlock import AttachmentVault

    path = tmp_path / "sealed2.pdf"
    make_long_pdf(path, 30, page_chars=200, encrypted=True)
    _, cards = await prepare_pdfs([str(path)], ["sealed2.pdf"])
    vault = AttachmentVault(tmp_path / "vault")
    vault.register("chat", "user", cards, [str(path)], tmp_path)
    attachment_id = cards[0]["attachment_id"]
    first = await vault.unlock("chat", attachment_id, "fixture-password")
    assert "PAGE-001-MARKER" in first
    result = await vault.unlock("chat", attachment_id, "not-the-password", pages="25-30")
    assert result.startswith("Error")
    assert "no longer matches the verified password" in result
    assert "PAGE-025-MARKER" not in result
    good = await vault.unlock("chat", attachment_id, "fixture-password", pages="25-30")
    assert "PAGE-025-MARKER" in good and "PAGE-030-MARKER" in good
    assert "PAGE-001-MARKER" not in good  # only the requested range comes back


@pytest.mark.asyncio
async def test_unlock_paging_honours_the_attempt_cap(tmp_path: Path) -> None:
    # Re-review blocking item 6: a re-upload sharing the file digest can
    # exhaust the 5 password attempts; the already-unlocked attachment's page
    # reads must then stop decrypting the stored file, not bypass the cap.
    from nanobot.webui.attachment_unlock import AttachmentVault

    path = tmp_path / "sealed-cap.pdf"
    make_long_pdf(path, 30, page_chars=200, encrypted=True)
    _, cards = await prepare_pdfs([str(path)], ["sealed-cap.pdf"])
    vault = AttachmentVault(tmp_path / "vault")
    vault.register("chat", "user", cards, [str(path)], tmp_path)
    attachment_id = cards[0]["attachment_id"]
    assert "PAGE-001-MARKER" in await vault.unlock("chat", attachment_id, "fixture-password")
    assert await vault.unlock("chat", attachment_id, "fixture-password", pages="3-4")
    digest = vault.records[attachment_id]["sha256"]
    vault.attempts[digest] = 5
    vault.save()
    blocked = await vault.unlock("chat", attachment_id, "fixture-password", pages="5-6")
    assert blocked.startswith("Error: The 5 password attempts were used")
    # The whole-document path (no pages) keeps serving the stored extraction.
    assert "Unlocked PDF" in await vault.unlock("chat", attachment_id, "fixture-password")
    # A fresh vault sees the persisted cap too.
    assert "5 password attempts" in await AttachmentVault(tmp_path / "vault").unlock(
        "chat", attachment_id, "fixture-password", pages="5-6")


@pytest.mark.asyncio
async def test_unlock_page_walk_survives_oversized_pages(tmp_path: Path) -> None:
    # Re-review blocking item 4 (reviewer's case): 30 pages of 14,000 chars.
    # Under the hard-coded 12k page budget every page was truncated to a
    # prefix (marker lost) whose continuation re-read the same page forever.
    # With the budget derived from the configured max_tool_result_chars the
    # walk reaches page 30, returns every marker exactly once, and each call
    # discloses exactly the pages it delivered inside the result budget.
    from nanobot.webui.attachment_unlock import AttachmentVault

    path = tmp_path / "wide-pages.pdf"
    make_long_pdf(path, 30, page_chars=14_000, encrypted=True)
    _, cards = await prepare_pdfs([str(path)], ["wide-pages.pdf"], context_window_tokens=262_144)
    vault = AttachmentVault(tmp_path / "vault")
    vault.register("chat", "user", cards, [str(path)], tmp_path)
    attachment_id = cards[0]["attachment_id"]
    await vault.unlock("chat", attachment_id, "fixture-password")
    results: list[str] = []
    pages = "1-20"
    guard = 0
    while True:
        guard += 1
        assert guard < 40, f"page walk did not terminate after {guard} calls: {results[-1][-200:] if results else pages}"
        start = pages.split("-")[0]
        result = await vault.unlock("chat", attachment_id, "fixture-password", pages=pages)
        assert not result.startswith("Error"), result
        assert len(result) < 16_000  # configured tool-result budget
        # Disclose what was actually delivered: the head names the requested
        # start page and the walk never re-reads the page it just served.
        assert re.search(rf"pages {start}-\d+ of 30 ", result), result[:120]
        results.append(result)
        match = re.search(r"Continue with unlock_attachment attachment_id='[^']+' pages='(\d+)-(\d+)'", result)
        if match is None:
            break
        pages = f"{match.group(1)}-{match.group(2)}"
    assert "PAGE-030-MARKER" in results[-1]
    combined = "\n".join(results)
    for index in range(1, 31):
        assert combined.count(f"PAGE-{index:03d}-MARKER") == 1, index


@pytest.mark.asyncio
async def test_unlock_page_walk_advances_past_truncated_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The livelock case proper: a page larger than the page budget is
    # disclosed as truncated and the continuation points at the FOLLOWING
    # page, never back at the truncated one.
    from types import SimpleNamespace

    from nanobot.webui.attachment_unlock import AttachmentVault

    monkeypatch.setattr(
        "nanobot.config.loader.load_config",
        lambda *a, **k: SimpleNamespace(agents=SimpleNamespace(
            defaults=SimpleNamespace(max_tool_result_chars=8_000))))  # page budget 6,800
    path = tmp_path / "huge-pages.pdf"
    make_long_pdf(path, 5, page_chars=14_000, encrypted=True)
    _, cards = await prepare_pdfs([str(path)], ["huge-pages.pdf"], context_window_tokens=262_144)
    vault = AttachmentVault(tmp_path / "vault")
    vault.register("chat", "user", cards, [str(path)], tmp_path)
    attachment_id = cards[0]["attachment_id"]
    await vault.unlock("chat", attachment_id, "fixture-password")
    result = await vault.unlock("chat", attachment_id, "fixture-password", pages="1-4")
    assert "exceeded the per-call character limit" in result
    assert "Page 1 exceeded" in result
    match = re.search(r"pages='(\d+)-(\d+)'", result)
    assert match is not None, result
    assert match.group(1) == "2"  # advance past the disclosed page, do not repeat it


@pytest.mark.asyncio
async def test_unlock_page_budget_follows_configured_max_tool_result_chars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Re-review item 4: the page budget derives from the configured
    # max_tool_result_chars, not the hard-coded 12k, so the continuation hint
    # is not cut off when the setting is lowered (e.g. to 8,000).
    from types import SimpleNamespace

    from nanobot.webui.attachment_unlock import unlock_page_max_chars

    monkeypatch.setattr(
        "nanobot.config.loader.load_config",
        lambda *a, **k: SimpleNamespace(agents=SimpleNamespace(
            defaults=SimpleNamespace(max_tool_result_chars=8_000))),
    )
    assert unlock_page_max_chars() == 6_800

    from nanobot.webui.attachment_unlock import AttachmentVault

    path = tmp_path / "tight-budget.pdf"
    make_long_pdf(path, 3, page_chars=6_000, encrypted=True)
    _, cards = await prepare_pdfs([str(path)], ["tight-budget.pdf"], context_window_tokens=262_144)
    vault = AttachmentVault(tmp_path / "vault")
    vault.register("chat", "user", cards, [str(path)], tmp_path)
    attachment_id = cards[0]["attachment_id"]
    await vault.unlock("chat", attachment_id, "fixture-password")
    result = await vault.unlock("chat", attachment_id, "fixture-password", pages="2-3")
    assert not result.startswith("Error"), result
    assert "Continue with unlock_attachment" in result
    assert len(result) < 8_000  # hint survives inside the lowered result budget


@pytest.mark.asyncio
async def test_unlock_page_range_error_and_missing_file(tmp_path: Path) -> None:
    from nanobot.webui.attachment_unlock import AttachmentVault

    path = tmp_path / "sealed3.pdf"
    make_long_pdf(path, 5, page_chars=200, encrypted=True)
    _, cards = await prepare_pdfs([str(path)], ["sealed3.pdf"])
    vault = AttachmentVault(tmp_path / "vault")
    vault.register("chat", "user", cards, [str(path)], tmp_path)
    attachment_id = cards[0]["attachment_id"]
    await vault.unlock("chat", attachment_id, "fixture-password")
    bad = await vault.unlock("chat", attachment_id, "fixture-password", pages="9-3")
    assert bad.startswith("Error: Invalid page range")
    gone = await vault.unlock("chat", attachment_id, "fixture-password", pages="1-2")
    assert "PAGE-001-MARKER" in gone
    path.unlink()
    missing = await vault.unlock("chat", attachment_id, "fixture-password", pages="1-2")
    assert missing == "Error: The stored PDF is no longer available; ask the user to re-upload it."
