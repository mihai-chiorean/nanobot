"""``GET /api/memory/items`` — owner-facing "what Ziggy knows about you" (MIT-1880).

Design §6: testers can't see what Ziggy believes about them. The route serves
the runtime's own ``USER.md`` and ``memory/MEMORY.md`` as section-grouped
items keyed by the same stable ``provenance_key`` id ``memory_explain`` uses,
annotated with each fact's ``provenance.jsonl`` source. Because every tester
runtime is its own container the route sees only that tester's files — and it
takes no parameters at all, so there is no path argument to forge either.

Auth mirrors ``/api/activity/audit`` (MIT-1450): the owner API token only.
The trusted-proxy shortcut must not admit bare proxied requests (repo rule
for owner routes) and room credentials are a distinct audience and refused —
a room guest must never read what Ziggy remembers about the owner.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from nanobot import __file__ as _nanobot_init
from nanobot.agent.memory import MemoryStore, provenance_key
from nanobot.channels.websocket.rooms import SharedRoomStore
from nanobot.session.manager import SessionManager

_ROOM_CHAT = "11111111-2222-3333-4444-555555555555"
_ROOM_ID = "room_" + "a" * 32
_PARTICIPANT_ID = "participant_" + "b" * 32
_SESSION_KEY = "websocket:tester"
_TITLE = "Estuary survey planning"

_USER_MD = """# User Profile

Information about the user to help personalize interactions.

## Basic Information

- **Name**: Mihai
- Lives in Albany and works from home

## Preferences

- Prefers terse answers
"""

_MEMORY_MD = """# Long-term Memory

## User Information

- Ships on Fridays
"""


def _seed_workspace(
    tmp_path: Path, *, user_md: str = _USER_MD, memory_md: str = _MEMORY_MD
) -> Path:
    workspace = tmp_path / "ws"
    (workspace / "memory").mkdir(parents=True, exist_ok=True)
    (workspace / "USER.md").write_text(user_md, encoding="utf-8")
    (workspace / "memory" / "MEMORY.md").write_text(memory_md, encoding="utf-8")
    return workspace


def _channel(sessions: SessionManager, port: int, workspace: Path) -> Any:
    from nanobot.channels.websocket.tests.test_websocket_http_routes import _ch

    return _ch(MagicMock(), session_manager=sessions, port=port, workspace_path=workspace)


async def _get(channel: Any, port: int, path: str, *, token: str | None = None) -> Any:
    from nanobot.channels.websocket.tests.ws_test_client import http_get

    headers = {"Authorization": f"Bearer {token}"} if token is not None else None
    return await http_get(f"http://127.0.0.1:{port}{path}", headers=headers)


class _Headers(dict):
    def get(self, key: str, default: Any = None) -> Any:  # type: ignore[reportUnknownParameter]
        for existing, value in self.items():
            if existing.lower() == key.lower():
                return value
        return default


@pytest.fixture(autouse=True)
def _isolate_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("nanobot.config.paths.get_data_dir", lambda: tmp_path)


def _free_port_and_channel(
    tmp_path: Path, *, user_md: str = _USER_MD, memory_md: str = _MEMORY_MD
) -> tuple[Any, int, SessionManager, Path]:
    from nanobot.channels.websocket.tests.test_websocket_http_routes import _free_port

    workspace = _seed_workspace(tmp_path, user_md=user_md, memory_md=memory_md)
    sessions = SessionManager(workspace)
    port = _free_port()
    return _channel(sessions, port, workspace), port, sessions, workspace


def _items(payload: dict[str, Any], file_label: str) -> list[dict[str, Any]]:
    entry = next(f for f in payload["files"] if f["file"] == file_label)
    return entry["items"]


@pytest.mark.asyncio
async def test_items_grouped_by_section_with_stable_ids(tmp_path: Path) -> None:
    channel, port, _sessions, _workspace = _free_port_and_channel(tmp_path)
    try:
        token = channel.gateway.tokens.issue_api_token(300)
        response = await _get(channel, port, "/api/memory/items", token=token)
        assert response.status_code == 200, response.content
        payload = response.json()

        assert [f["file"] for f in payload["files"]] == ["USER.md", "memory/MEMORY.md"]
        assert payload["budget"] == 2000
        # The documented fallback estimator: the spec's own formula
        # (len(text) // 4) over the seeded core text; SM-10's shared
        # count_tokens is not merged on this base.
        assert payload["core_tokens"] == len(f"{_USER_MD}\n\n{_MEMORY_MD}") // 4

        by_text = {item["text"]: item for f in payload["files"] for item in f["items"]}
        assert by_text["Prefers terse answers"]["section"] == "Preferences"
        assert by_text["Lives in Albany and works from home"]["section"] == "Basic Information"
        assert by_text["Ships on Fridays"]["file"] == "memory/MEMORY.md"
        # The leading "- " is stripped from the text but NOT from the id's
        # source line: the id must equal provenance_key of the physical line
        # as memory_explain and the provenance writer compute it.
        assert by_text["Prefers terse answers"]["id"] == provenance_key("- Prefers terse answers")
        assert by_text["Ships on Fridays"]["id"] == provenance_key("- Ships on Fridays")
        assert not any(text.startswith("- ") for text in by_text)

        # Stable ids: a second read yields the identical payload.
        second = await _get(channel, port, "/api/memory/items", token=token)
        assert second.status_code == 200
        assert second.json() == payload
    finally:
        await channel.stop()


@pytest.mark.asyncio
async def test_template_lines_skipped(tmp_path: Path) -> None:
    """An untouched template contributes nothing; a real fact containing
    parentheses still shows (negative control against over-skipping)."""
    templates = Path(_nanobot_init).parent / "templates"
    memory_tpl = templates / "memory" / "MEMORY.md"
    user_tpl = templates / "USER.md"
    assert "(Important facts about the user)" in memory_tpl.read_text(encoding="utf-8")
    memory_md = (
        memory_tpl.read_text(encoding="utf-8") + "\n- User prefers terse (one-line) answers\n"
    )
    channel, port, _sessions, _workspace = _free_port_and_channel(
        tmp_path, user_md=user_tpl.read_text(encoding="utf-8"), memory_md=memory_md
    )
    try:
        token = channel.gateway.tokens.issue_api_token(300)
        response = await _get(channel, port, "/api/memory/items", token=token)
        assert response.status_code == 200, response.content
        payload = response.json()
        texts = [item["text"] for f in payload["files"] for item in f["items"]]

        assert "User prefers terse (one-line) answers" in texts
        assert not any(re.fullmatch(r"\(.*\)", text) for text in texts), texts
        assert not any(text.startswith("*This file is automatically updated") for text in texts)
        assert not any(text.startswith("*Edit this file to customize") for text in texts)
        assert "---" not in texts
        # The headings themselves are structure, never items.
        assert "Long-term Memory" not in texts
        assert "Important Notes" not in texts
    finally:
        await channel.stop()


def _record_provenance(workspace: Path, facts: list[str], session_key: str) -> None:
    """Attach provenance to *facts* via the real Dream-sidecar writer.

    git-init the store, write the facts into MEMORY.md and record from the
    real working-tree diff — the exact sequence a Dream run performs, the one
    MIT-1441's own tests use — so the records the route reads were not made
    to order for the route.
    """
    store = MemoryStore(workspace)
    store.git.init()
    store.write_memory(
        "# Long-term Memory\n\n## User Information\n\n" + "".join(f"- {f}\n" for f in facts)
    )
    batch = [
        {
            "cursor": cursor,
            "timestamp": "2026-09-21 09:00",
            "content": f"consolidated entry {cursor}",
            "session_key": session_key,
        }
        for cursor in (5, 6)
    ]
    store.record_dream_provenance(store.dream_content_diff(), batch)


@pytest.mark.asyncio
async def test_source_attached_from_provenance(tmp_path: Path) -> None:
    """A fact with a provenance record (written by the real sidecar writer,
    read through the same sidecar ``memory_explain`` reads) comes back with
    ``source: {title, messages, date}``; an unrecorded fact has none."""
    fact = "Prefers terse answers"
    unrecorded = "Drinks builder tea"
    channel, port, sessions, workspace = _free_port_and_channel(
        tmp_path, user_md="# User Profile\n", memory_md=""
    )
    try:
        _record_provenance(workspace, [fact], _SESSION_KEY)
        # A second fact added after the run was recorded: no source exists.
        MemoryStore(workspace).write_memory(
            f"# Long-term Memory\n\n## User Information\n\n- {fact}\n- {unrecorded}\n"
        )
        assert MemoryStore(workspace).find_provenance(f"- {fact}") is not None

        session = sessions.get_or_create(_SESSION_KEY)
        session.metadata["title"] = _TITLE
        sessions.save(session)

        token = channel.gateway.tokens.issue_api_token(300)
        response = await _get(channel, port, "/api/memory/items", token=token)
        assert response.status_code == 200, response.content
        by_text = {item["text"]: item for f in response.json()["files"] for item in f["items"]}

        source = by_text[fact]["source"]
        # Same citation semantics format_citation uses: the human session
        # title, the 1-based message band, a YYYY-MM-DD date.
        assert source["title"] == _TITLE
        assert source["messages"] == "5\u20136"
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", source["date"]), source
        assert source["date"] == datetime.now(timezone.utc).strftime("%Y-%m-%d")
        assert "source" not in by_text[unrecorded]
    finally:
        await channel.stop()


def test_source_title_falls_back_to_session_key(tmp_path: Path) -> None:
    """No session metadata, no title: the citation names the session key
    itself, exactly as format_citation labels an untitled hit."""
    from nanobot.webui.memory_api import list_items

    workspace = _seed_workspace(tmp_path, user_md="# User Profile\n", memory_md="")
    _record_provenance(workspace, ["Prefers terse answers"], _SESSION_KEY)
    payload = list_items(MemoryStore(workspace))
    item = next(
        i for f in payload["files"] for i in f["items"] if i["text"] == "Prefers terse answers"
    )
    assert item["source"]["title"] == _SESSION_KEY
    assert item["source"]["messages"] == "5\u20136"


@pytest.mark.asyncio
async def test_requires_owner_token(tmp_path: Path) -> None:
    """No owner token -> 401. Includes the repo-rule case: a request carrying
    the trusted-proxy mark but no Authorization header must NOT be admitted by
    the ``check_api_token`` shortcut (model: the work-routes proxy test)."""
    from nanobot.channels.websocket.transport import TransportRequest

    channel, port, _sessions, _workspace = _free_port_and_channel(tmp_path)
    try:
        anonymous = await _get(channel, port, "/api/memory/items")
        assert anonymous.status_code == 401, anonymous.content
        wrong = await _get(channel, port, "/api/memory/items", token="not-a-real-token")
        assert wrong.status_code == 401, wrong.content

        handler = channel.gateway.http
        proxied = TransportRequest(
            method="GET",
            path="/api/memory/items",
            headers=_Headers(),
            body=b"",
            raw_path="/api/memory/items",
        )
        setattr(proxied, "_nanobot_trusted_proxy_authenticated", True)
        assert (await handler._handle_memory_items(proxied)).status_code == 401

        # Non-vacuity: the same route answers 200 for a real owner token.
        token = channel.gateway.tokens.issue_api_token(300)
        ok = await _get(channel, port, "/api/memory/items", token=token)
        assert ok.status_code == 200, ok.content
        assert ok.headers.get("Cache-Control") == "no-store"
    finally:
        await channel.stop()


@pytest.mark.asyncio
async def test_room_credential_refused(tmp_path: Path) -> None:
    """A live shared-room credential (nbrt_, a distinct audience in the room
    store) is refused the way /api/activity/audit refuses it: what Ziggy
    remembers about the owner is not a room guest's reading matter."""
    channel, port, sessions, _workspace = _free_port_and_channel(tmp_path)
    try:
        store = SharedRoomStore(sessions, token_ttl_s=300)
        room_token, credential = store.mint(
            room_id=_ROOM_ID,
            chat_id=_ROOM_CHAT,
            participant_id=_PARTICIPANT_ID,
            display_name="Guest",
            role="contributor",
        )
        assert room_token.startswith("nbrt_")
        assert store.api_credential(room_token) is credential  # live, not revoked
        refused = await _get(channel, port, "/api/memory/items", token=room_token)
        assert refused.status_code == 401, refused.content
        # Sanity: the owner token still works, so the 401 is the audience
        # gate, not a broken route.
        owner = channel.gateway.tokens.issue_api_token(300)
        assert (await _get(channel, port, "/api/memory/items", token=owner)).status_code == 200
    finally:
        await channel.stop()


@pytest.mark.asyncio
async def test_no_path_parameter_honoured(tmp_path: Path) -> None:
    """The route takes no parameters: a path-ish query is ignored, and the
    response is byte-for-byte the owner's own two files."""
    channel, port, _sessions, _workspace = _free_port_and_channel(tmp_path)
    try:
        token = channel.gateway.tokens.issue_api_token(300)
        plain = await _get(channel, port, "/api/memory/items", token=token)
        assert plain.status_code == 200, plain.content
        for forged in ("../x", "/etc/passwd", "memory/archive.md"):
            response = await _get(channel, port, f"/api/memory/items?file={forged}", token=token)
            assert response.status_code == 200, response.content
            assert response.json() == plain.json()
        assert [f["file"] for f in plain.json()["files"]] == ["USER.md", "memory/MEMORY.md"]
    finally:
        await channel.stop()


@pytest.mark.asyncio
async def test_post_not_allowed(tmp_path: Path) -> None:
    from nanobot.channels.websocket.transport import TransportRequest

    channel, _port, _sessions, _workspace = _free_port_and_channel(tmp_path)
    try:
        handler = channel.gateway.http
        token = channel.gateway.tokens.issue_api_token(300)
        request = TransportRequest(
            method="POST",
            path="/api/memory/items",
            headers=_Headers({"Authorization": f"Bearer {token}"}),
            body=b"{}",
            raw_path="/api/memory/items",
        )
        response = await handler._dispatch_misc_routes(MagicMock(), request, "/api/memory/items")
        assert response is not None
        assert response.status_code == 405
    finally:
        await channel.stop()
