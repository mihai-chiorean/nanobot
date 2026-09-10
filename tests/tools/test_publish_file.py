"""Security and durability tests for private Markdown publication."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from nanobot.agent.tools.publish_file import (
    PublishFileTool,
    PublishFileTurn,
    bind_publish_file_turn,
    reset_publish_file_turn,
)
from nanobot.session.manager import Session, SessionManager


@pytest.mark.asyncio
async def test_publish_file_snapshots_workspace_markdown_and_rejects_unsafe_paths(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    report = workspace / "report.md"
    report.write_text("first version", encoding="utf-8")
    outside = tmp_path / "outside.md"
    outside.write_text("private", encoding="utf-8")
    (workspace / "linked.md").symlink_to(outside)
    (workspace / "dirlink").symlink_to(tmp_path, target_is_directory=True)
    fifo = workspace / "stream.md"
    os.mkfifo(fifo)
    oversized = workspace / "large.md"
    oversized.write_bytes(b"x" * (2 * 1024 * 1024 + 1))

    manager = SessionManager(workspace)
    turn = PublishFileTurn(manager, "websocket:private")
    token = bind_publish_file_turn(turn)
    try:
        tool = PublishFileTool(workspace)
        published = await tool.execute("report.md")
        assert "/api/sessions/websocket%3Aprivate/files/" in published
        assert len(turn.publications) == 1
        for unsafe in ("../outside.md", "linked.md", "dirlink/outside.md", "stream.md", "large.md"):
            result = await tool.execute(unsafe)
            assert result.startswith("Error: Cannot publish file:")
    finally:
        reset_publish_file_turn(token)

    file_id = next(iter(turn.publications))
    session = Session(key="websocket:private")
    url = manager.published_file_url(session.key, file_id)
    assert manager.read_published_file(session.key, file_id) is None
    session.add_message("assistant", f"Download it: [report.md]({url})")
    manager.grant_published_files(session, turn.publications, message_start=0)
    manager.save(session)
    report.write_text("mutated source", encoding="utf-8")

    restarted = SessionManager(workspace)
    fetched = restarted.read_published_file("websocket:private", file_id)
    assert fetched == ("report.md", b"first version")


def test_room_clone_only_copies_provenanced_exact_links(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manager = SessionManager(workspace)
    source = Session(key="websocket:source")
    file_id = manager.store_published_snapshot("report.md", b"snapshot")
    url = manager.published_file_url(source.key, file_id)
    source.add_message("assistant", f"A real report: [report.md]({url})")
    manager.grant_published_files(source, {file_id: "report.md"}, message_start=0)
    # A copied URL in another visible message does not have matching server
    # message provenance, and an invented id is never a grant.
    fake = "f" * 32
    source.add_message(
        "assistant",
        f"Copied [report.md]({url}); invented [fake.md]({manager.published_file_url(source.key, fake)})",
    )
    manager.save(source)

    clone = manager.clone_session(
        source.key,
        "websocket:room",
        metadata={"shared_room": True},
        shared_room_owner="Owner",
    )
    room_url = manager.published_file_url(clone.key, file_id)
    assert clone.messages[0]["content"] == f"A real report: [report.md]({room_url})"
    assert url in clone.messages[1]["content"]
    grants = clone.metadata["published_file_grants"]
    assert grants == {file_id: {"filename": "report.md"}}
    assert manager.read_published_file(clone.key, file_id) == ("report.md", b"snapshot")
    assert manager.read_published_file(clone.key, fake) is None


def test_published_file_rejects_safe_filename_alias_key(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manager = SessionManager(workspace)
    first = Session(key="websocket:a:b")
    file_id = manager.store_published_snapshot("report.md", b"snapshot")
    first.add_message("assistant", f"[report.md]({manager.published_file_url(first.key, file_id)})")
    manager.grant_published_files(first, {file_id: "report.md"}, message_start=0)
    manager.save(first)

    # ``safe_key`` maps these two inputs to the same on-disk stem. The stored
    # session key is an additional authorization check, not just a filename.
    assert manager.read_published_file("websocket:a_b", file_id) is None


def test_room_clone_provenance_survives_session_message_cap(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manager = SessionManager(workspace)
    source = Session(key="websocket:source")
    source.add_message("user", "discarded prefix")
    source.add_message("user", "retained anchor")
    file_id = manager.store_published_snapshot("report.md", b"snapshot")
    url = manager.published_file_url(source.key, file_id)
    source.add_message("assistant", f"[report.md]({url})")
    manager.grant_published_files(source, {file_id: "report.md"}, message_start=2)
    # History trimming changes the assistant's list position. Its server-made
    # message id, rather than an unstable array index, remains provenance.
    for number in range(1_999):
        source.add_message("assistant", f"filler {number}")
    source.enforce_file_cap()
    assert len(source.messages) == 2_000
    manager.save(source)
    clone = manager.clone_session(
        source.key,
        "websocket:room",
        metadata={"shared_room": True},
        shared_room_owner="Owner",
    )
    assert any(
        manager.published_file_url(clone.key, file_id) in message.get("content", "")
        for message in clone.messages
    )
