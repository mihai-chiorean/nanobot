"""Security and durability tests for private Markdown publication (MIT-1030).

Ported from the 0.2.x ``feat/shared-rooms`` lineage and re-integrated into the
0.3.0 session-store layout: publication snapshots live in the runtime-owned
``.nanobot-published-files`` namespace beside the session files, and serving
authorization is a per-session grant, never the bare id.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from nanobot.agent.tools.publish_file import (
    PublishFileTool,
    PublishFileTurn,
    bind_publish_file_turn,
    current_publish_file_turn,
    reset_publish_file_turn,
)
from nanobot.session.manager import JsonlSessionStore, Session, SessionManager


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
        tool = PublishFileTool(workspace, manager)
        published = await tool.execute("report.md")
        assert "/api/sessions/websocket%3Aprivate/files/" in published
        assert len(turn.publications) == 1
        for unsafe in (
            "../outside.md",
            "linked.md",
            "dirlink/outside.md",
            "stream.md",
            "large.md",
            "not-markdown.txt",
        ):
            (workspace / "not-markdown.txt").write_text("x", encoding="utf-8")
            result = await tool.execute(unsafe)
            assert result.startswith("Error: Cannot publish file:")
        assert len(turn.publications) == 1
    finally:
        reset_publish_file_turn(token)

    assert current_publish_file_turn() is None
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


@pytest.mark.asyncio
async def test_publish_file_is_inert_without_a_bound_turn(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "report.md").write_text("secret", encoding="utf-8")
    manager = SessionManager(workspace)
    tool = PublishFileTool(workspace, manager)
    result = await tool.execute("report.md")
    assert result == "Error: File publication is unavailable in this conversation."


@pytest.mark.asyncio
async def test_disabled_turn_rejects_publication(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "report.md").write_text("secret", encoding="utf-8")
    manager = SessionManager(workspace)
    turn = PublishFileTurn(manager, "websocket:room", enabled=False)
    token = bind_publish_file_turn(turn)
    try:
        tool = PublishFileTool(workspace, manager)
        result = await tool.execute("report.md")
        assert result == "Error: File publication is unavailable in this conversation."
        assert turn.publications == {}
    finally:
        reset_publish_file_turn(token)


def test_grant_requires_the_exact_canonical_link_in_a_visible_answer(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manager = SessionManager(workspace)
    source = Session(key="websocket:source")
    file_id = manager.store_published_snapshot("report.md", b"snapshot")
    url = manager.published_file_url(source.key, file_id)
    # An intermediate tool-bearing message that repeats the link must not
    # grant: only final assistant answers (no tool_calls) carry provenance.
    source.add_message("assistant", f"Draft: [report.md]({url})", tool_calls=[{"id": "c1"}])
    manager.grant_published_files(source, {file_id: "report.md"}, message_start=0)
    assert source.metadata.get("published_file_grants", {}) == {}
    assert manager.read_published_file(source.key, file_id) is None

    source.add_message("assistant", f"A real report: [report.md]({url})")
    manager.grant_published_files(source, {file_id: "report.md"}, message_start=0)
    manager.save(source)
    assert source.metadata["published_file_grants"] == {file_id: {"filename": "report.md"}}
    assert manager.read_published_file(source.key, file_id) == ("report.md", b"snapshot")

    # A copied URL in another message without matching server-side
    # provenance, and an invented id, never become grants.
    fake = "f" * 32
    source.add_message(
        "assistant",
        f"Copied [report.md]({url}); invented [fake.md]"
        f"({manager.published_file_url(source.key, fake)})",
    )
    manager.grant_published_files(source, {fake: "fake.md"}, message_start=0)
    assert fake not in source.metadata["published_file_grants"]
    assert manager.read_published_file(source.key, fake) is None


def test_published_file_rejects_safe_filename_alias_key(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manager = SessionManager(workspace)
    first = Session(key="websocket:a:b")
    file_id = manager.store_published_snapshot("report.md", b"snapshot")
    first.add_message(
        "assistant",
        f"[report.md]({manager.published_file_url(first.key, file_id)})",
    )
    manager.grant_published_files(first, {file_id: "report.md"}, message_start=0)
    manager.save(first)

    # ``safe_key`` maps these two inputs to the same on-disk stem. The stored
    # session key is an additional authorization check, not just a filename.
    assert manager.safe_key("websocket:a:b") == manager.safe_key("websocket:a_b")
    assert manager.read_published_file("websocket:a_b", file_id) is None


def test_published_file_url_rejects_malformed_ids(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manager = SessionManager(workspace)
    with pytest.raises(ValueError):
        manager.published_file_url("websocket:x", "../etc/passwd")
    with pytest.raises(ValueError):
        manager.published_file_url("websocket:x", "A" * 32)
    assert manager._snapshot_bytes("A" * 32) is None
    assert manager._snapshot_bytes("z" * 32) is None


def test_store_published_snapshot_rejects_bad_inputs(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manager = SessionManager(workspace)
    with pytest.raises(ValueError):
        manager.store_published_snapshot("report.txt", b"ok")
    with pytest.raises(ValueError):
        manager.store_published_snapshot("report.md", "not-bytes")
    with pytest.raises(ValueError):
        manager.store_published_snapshot("report.md", b"x" * (2 * 1024 * 1024 + 1))


def test_publication_store_lives_outside_the_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manager = SessionManager(workspace)
    file_id = manager.store_published_snapshot("report.md", b"snapshot")
    assert manager.published_files_dir.is_dir()
    assert workspace not in manager.published_files_dir.parents
    assert (manager.published_files_dir / file_id).read_bytes() == b"snapshot"
    assert oct(os.stat(manager.published_files_dir).st_mode)[-3:] == "700"


def test_publication_store_is_created_lazily_on_first_snapshot(tmp_path: Path) -> None:
    """Constructing a manager must not touch the shared sessions root."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sessions_root = tmp_path / "sessions"
    manager = SessionManager(workspace, sessions_root=sessions_root)
    store_parent = sessions_root / ".nanobot-published-files"
    assert manager.published_files_dir.parent == store_parent
    assert not store_parent.exists()

    # Reads and grant checks against a store that does not exist yet are
    # plain misses, and do not create it either.
    assert manager.read_published_file("websocket:chat", "a" * 32) is None
    assert manager._snapshot_bytes("a" * 32) is None
    assert not store_parent.exists()

    file_id = manager.store_published_snapshot("report.md", b"snapshot")
    assert manager._snapshot_bytes(file_id) == b"snapshot"
    assert oct(os.stat(store_parent).st_mode)[-3:] == "700"
    assert oct(os.stat(manager.published_files_dir).st_mode)[-3:] == "700"


def test_publication_dotdir_does_not_disturb_sessions_root_consumers(
    tmp_path: Path,
) -> None:
    """The sessions root is shared by every workspace namespace; the store's
    dotdir must not be mistaken for one by namespace discovery or listing."""
    sessions_root = tmp_path / "sessions"
    first_ws = tmp_path / "first"
    first_ws.mkdir()
    first = SessionManager(first_ws, sessions_root=sessions_root)
    first.store_published_snapshot("report.md", b"snapshot")
    session = first.get_or_create("websocket:chat")
    session.add_message("user", "hi")
    first.save(session)

    # A second workspace, and a restart of the first, both resolve their own
    # namespace next to the dotdir without error.
    second_ws = tmp_path / "second"
    second_ws.mkdir()
    second = SessionManager(second_ws, sessions_root=sessions_root)
    assert second.workspace_id != first.workspace_id
    restarted = SessionManager(first_ws, sessions_root=sessions_root)
    assert restarted.workspace_id == first.workspace_id
    assert [row["key"] for row in restarted.list_sessions()] == ["websocket:chat"]
    assert second.list_sessions() == []

    # Namespace recovery scans the root; it must skip the dotdir.
    assert (
        JsonlSessionStore._find_workspace_namespace(first_ws.resolve(), sessions_root.resolve())
        == first.workspace_id
    )
