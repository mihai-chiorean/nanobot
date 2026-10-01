"""MIT-1489: transcript backfill for journal-less websocket sessions."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from nanobot.cli.transcript_backfill import backfill_journalless_webui_transcripts
from nanobot.session.manager import SessionManager
from nanobot.webui.transcript import (
    _legacy_webui_thread_path,
    read_transcript_lines,
    webui_transcript_path,
    write_session_messages_as_transcript,
)

TURNS = [
    ("user", "hello"),
    ("assistant", "answer"),
    ("user", "again"),
    ("assistant", "done"),
]


@pytest.fixture
def manager(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SessionManager:
    monkeypatch.setattr("nanobot.config.paths.get_data_dir", lambda: tmp_path)
    return SessionManager(tmp_path / "workspace")


def _add_session(
    manager: SessionManager,
    key: str,
    turns: list[tuple[str, str]] = TURNS,
) -> None:
    session = manager.get_or_create(key)
    for role, text in turns:
        session.add_message(role, text)
    manager.save(session, fsync=True)


def test_backfill_writes_transcript_matching_direct_write(
    manager: SessionManager,
) -> None:
    _add_session(manager, "websocket:journalless")
    # Negative control: a non-websocket session must never get a transcript.
    _add_session(manager, "cli:not-webui")
    assert not webui_transcript_path("websocket:journalless").exists()
    assert not _legacy_webui_thread_path("websocket:journalless").exists()

    result = backfill_journalless_webui_transcripts(manager)

    assert result.scanned == 1
    assert result.backfilled == 1
    assert result.backfilled_keys == ["websocket:journalless"]
    assert webui_transcript_path("websocket:journalless").is_file()
    assert not webui_transcript_path("cli:not-webui").exists()
    assert not _legacy_webui_thread_path("cli:not-webui").exists()
    backfilled_bytes = webui_transcript_path("websocket:journalless").read_bytes()
    lines = read_transcript_lines("websocket:journalless")
    assert [line["text"] for line in lines] == [text for _, text in TURNS]
    assert all(line["chat_id"] == "journalless" for line in lines)

    # The rows must match what write_session_messages_as_transcript produces
    # directly from the same stored messages.
    stored_messages: Any = manager.read_session_file("websocket:journalless")
    assert isinstance(stored_messages, dict)
    write_session_messages_as_transcript("websocket:journalless", stored_messages["messages"])
    assert webui_transcript_path("websocket:journalless").read_bytes() == backfilled_bytes


def test_backfill_is_idempotent(manager: SessionManager) -> None:
    _add_session(manager, "websocket:twice")

    first = backfill_journalless_webui_transcripts(manager)
    assert first.backfilled == 1
    path = webui_transcript_path("websocket:twice")
    content = path.read_bytes()
    mtime_ns = path.stat().st_mtime_ns

    second = backfill_journalless_webui_transcripts(manager)

    assert second.scanned == 1
    assert second.backfilled == 0
    assert second.skipped_existing == 1
    assert path.read_bytes() == content
    assert path.stat().st_mtime_ns == mtime_ns
    assert len(read_transcript_lines("websocket:twice")) == len(TURNS)


def test_backfill_skips_malformed_sessions_without_aborting(
    manager: SessionManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _add_session(manager, "websocket:ok")
    # Real parse failure on disk: valid metadata line, undecodable message line.
    corrupt_key = "websocket:garbage"
    corrupt_path = manager._get_session_path(corrupt_key)
    corrupt_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_line = json.dumps(
        {
            "_type": "metadata",
            "key": corrupt_key,
            "created_at": "2026-10-01T00:00:00",
            "updated_at": "2026-10-01T00:00:00",
            "metadata": {},
        }
    )
    corrupt_path.write_text(metadata_line + '\n{"role": "user", "content": \n', encoding="utf-8")
    # An unreadable session file must not abort the run either: one raises out
    # of the store, one returns no payload at all.
    _add_session(manager, "websocket:boom")
    _add_session(manager, "websocket:vanished")
    original_read = manager.read_session_file

    def flaky_read(key: str) -> dict[str, Any] | None:
        if key == "websocket:boom":
            raise RuntimeError("session file unreadable")
        if key == "websocket:vanished":
            return None
        return original_read(key)

    monkeypatch.setattr(manager, "read_session_file", flaky_read)

    result = backfill_journalless_webui_transcripts(manager)

    assert result.backfilled == 1
    assert result.scanned == 3
    assert result.backfilled_keys == ["websocket:ok"]
    assert webui_transcript_path("websocket:ok").is_file()
    assert [line["text"] for line in read_transcript_lines("websocket:ok")] == [
        text for _, text in TURNS
    ]
    assert not webui_transcript_path("websocket:boom").exists()
    assert not webui_transcript_path("websocket:garbage").exists()
    assert not webui_transcript_path("websocket:vanished").exists()
    # The corrupt file is dropped by the store's own repair pass (never
    # enumerated, never rewritten); boom raises and vanished returns no
    # payload, so both must be counted as unreadable without aborting.
    assert corrupt_path.read_text(encoding="utf-8").endswith('{"role": "user", "content": \n')
    assert result.skipped_unreadable == 2


def test_backfill_leaves_existing_transcripts_untouched(manager: SessionManager) -> None:
    _add_session(manager, "websocket:active")
    _add_session(manager, "websocket:legacy")
    active_path = webui_transcript_path("websocket:active")
    active_path.parent.mkdir(parents=True, exist_ok=True)
    # Pre-existing content deliberately differs from what a rebuild would write,
    # so a rewrite would be visible.
    write_session_messages_as_transcript(
        "websocket:active",
        [{"role": "user", "content": "pre-existing"}],
    )
    active_bytes = active_path.read_bytes()
    active_mtime_ns = active_path.stat().st_mtime_ns
    legacy_path = _legacy_webui_thread_path("websocket:legacy")
    legacy_path.write_text('{"rows": ["pre-existing legacy"]}', encoding="utf-8")
    legacy_bytes = legacy_path.read_bytes()
    legacy_mtime_ns = legacy_path.stat().st_mtime_ns

    result = backfill_journalless_webui_transcripts(manager)

    assert result.scanned == 2
    assert result.backfilled == 0
    assert result.skipped_existing == 2
    assert active_path.read_bytes() == active_bytes
    assert active_path.stat().st_mtime_ns == active_mtime_ns
    assert legacy_path.read_bytes() == legacy_bytes
    assert legacy_path.stat().st_mtime_ns == legacy_mtime_ns
    assert not webui_transcript_path("websocket:legacy").exists()


def test_sessions_backfill_transcripts_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from typer.testing import CliRunner

    from nanobot.cli import commands
    from nanobot.config.loader import load_config

    monkeypatch.setattr("nanobot.config.paths.get_data_dir", lambda: tmp_path)
    workspace = tmp_path / "workspace"
    config = load_config(tmp_path / "instance" / "config.json")
    config.agents.defaults.workspace = str(workspace)
    manager = SessionManager(workspace)
    session = manager.get_or_create("websocket:cmd")
    session.add_message("user", "backfill me")
    manager.save(session, fsync=True)
    monkeypatch.setattr(commands, "_load_runtime_config", lambda *_args: config)

    result = CliRunner().invoke(commands.app, ["sessions", "backfill-transcripts"])

    assert result.exit_code == 0, result.output
    assert "backfilled 1" in result.output
    assert webui_transcript_path("websocket:cmd").is_file()
    assert [line["text"] for line in read_transcript_lines("websocket:cmd")] == [
        "backfill me"
    ]
