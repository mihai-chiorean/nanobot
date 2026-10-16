from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from nanobot.webui.pdf_attachments import pdf_text, prepare_pdfs
from nanobot.webui.transcript import append_transcript_object, build_webui_thread_response


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
        text, error = await pdf_text(Path("report.pdf"), timeout=0.01)
    assert text == ""
    assert "too long" in error
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
    for _ in range(5):
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
    connection = object()
    async def send(event, **fields):
        assert event == "attachment.unlock"
        response = {"request_id": fields["request_id"], "input": {"text": "Unlocked text"}}
        vault.resolve(object(), response)
        assert not vault.pending[fields["request_id"]][1].done()
        vault.resolve(connection, response)
    vault.bridges["chat"] = (connection, send)
    result = await asyncio.wait_for(vault.unlock("chat", cards[0]["attachment_id"], "password"), 1)
    assert "Unlocked text" in result


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
