from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from nanobot.webui.pdf_attachments import pdf_text, prepare_pdfs
from nanobot.webui.transcript import append_transcript_object, build_webui_thread_response


@pytest.fixture(autouse=True)
def isolate_attachment_transcripts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Earlier agent tests may select an active config root. Attachment history
    # fixtures must not inherit that root or authorize a stale test attachment.
    monkeypatch.setattr("nanobot.config.paths.get_data_dir", lambda: tmp_path / "data")


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
