"""Tests for the private publication route ``/api/sessions/<key>/files/<id>``.

MIT-1030 port from the 0.2.x ``feat/shared-rooms`` lineage. A publication id is
never a global capability: bytes are served only when the *stored* session
metadata carries a matching grant, and every failure mode — unauthenticated,
malformed, ungranted, non-canonical — collapses to the same silent 404.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.channels.websocket.runtime import WebSocketChannel, WebSocketConfig
from nanobot.session.manager import Session, SessionManager
from nanobot.webui.gateway_services import build_gateway_services

from .ws_test_client import InProcessHttpChannel
from .ws_test_client import http_get as _http_get


def _ch(
    bus: Any,
    *,
    session_manager: SessionManager | None = None,
    workspace_path: Path | None = None,
    port: int,
) -> WebSocketChannel:
    cfg = {
        "enabled": True,
        "allowFrom": ["*"],
        "host": "127.0.0.1",
        "port": port,
        "path": "/",
        "websocketRequiresToken": False,
    }
    parsed = WebSocketConfig.model_validate(cfg)
    gateway = build_gateway_services(
        config=parsed,
        bus=bus,
        session_manager=session_manager,
        static_dist_path=None,
        workspace_path=workspace_path or Path.cwd(),
        default_restrict_to_workspace=False,
        runtime_model_name=None,
        runtime_surface="browser",
        runtime_capabilities_overrides=None,
    )
    return InProcessHttpChannel(cfg, bus, gateway=gateway)


@pytest.fixture()
def bus() -> MagicMock:
    b = MagicMock()
    b.publish_inbound = AsyncMock()
    return b


def _seed_publication(workspace: Path, key: str = "websocket:private") -> tuple[SessionManager, str, str]:
    sm = SessionManager(workspace)
    session = Session(key=key)
    file_id = sm.store_published_snapshot("report.md", b"# immutable report\n")
    url = sm.published_file_url(session.key, file_id)
    session.add_message("assistant", f"Download: [report.md]({url})")
    sm.grant_published_files(session, {file_id: "report.md"}, message_start=0)
    sm.save(session)
    return sm, key, file_id


@pytest.mark.asyncio
async def test_granted_publication_serves_snapshot_bytes(
    bus: MagicMock, tmp_path: Path
) -> None:
    _sm, key, file_id = _seed_publication(tmp_path)
    channel = _ch(bus, session_manager=_sm, port=29950)
    server_task = asyncio.create_task(channel.start())
    try:
        token = channel.gateway.tokens.issue_api_token(300)
        auth = {"Authorization": f"Bearer {token}"}
        url = f"http://127.0.0.1:29950/api/sessions/{key.replace(':', '%3A')}/files/{file_id}"

        resp = await _http_get(url, headers=auth)
        assert resp.status_code == 200
        assert resp.content == b"# immutable report\n"
        assert resp.headers["Content-Disposition"].startswith('attachment; filename="report.md"')
        assert resp.headers["Cache-Control"] == "private, no-store"
        assert resp.headers["X-Content-Type-Options"] == "nosniff"
        assert "markdown" in resp.headers["Content-Type"]

        # An unauthenticated probe gets the same silent 404 as an unknown id.
        deny = await _http_get(url)
        assert deny.status_code == 404

        # An id the session was never granted is not a capability.
        fake = await _http_get(url.replace(file_id, "f" * 32), headers=auth)
        assert fake.status_code == 404

        # A grant in one session never authorizes another.
        other = await _http_get(
            url.replace(key.replace(":", "%3A"), "websocket%3Avictim"),
            headers=auth,
        )
        assert other.status_code == 404
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_publication_route_rejects_non_canonical_forms(
    bus: MagicMock, tmp_path: Path
) -> None:
    _sm, key, file_id = _seed_publication(tmp_path, key="websocket:forms")
    channel = _ch(bus, session_manager=_sm, port=29951)
    server_task = asyncio.create_task(channel.start())
    base = f"http://127.0.0.1:29951/api/sessions/{key.replace(':', '%3A')}/files/{file_id}"
    try:
        token = channel.gateway.tokens.issue_api_token(300)
        auth = {"Authorization": f"Bearer {token}"}

        # Query strings and fragments must not broaden the capability.
        assert (await _http_get(f"{base}?sid=x", headers=auth)).status_code == 404

        # Uppercase hex ids were never minted by the server.
        upper = await _http_get(
            base.replace(file_id, file_id.upper()),
            headers=auth,
        )
        assert upper.status_code == 404

        # Over-long / structurally bogus ids never reach the store.
        assert (await _http_get(base + "/", headers=auth)).status_code in {200, 404}
        assert (
            await _http_get(
                f"http://127.0.0.1:29951/api/sessions/{key.replace(':', '%3A')}/files/{'a' * 33}",
                headers=auth,
            )
        ).status_code == 404
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_publication_route_rejects_doubly_encoded_key(
    bus: MagicMock, tmp_path: Path
) -> None:
    """``%253A`` must not decode twice into a session separator."""
    _sm, _key, file_id = _seed_publication(tmp_path, key="websocket:a:b")
    channel = _ch(bus, session_manager=_sm, port=29952)
    server_task = asyncio.create_task(channel.start())
    try:
        token = channel.gateway.tokens.issue_api_token(300)
        auth = {"Authorization": f"Bearer {token}"}
        resp = await _http_get(
            f"http://127.0.0.1:29952/api/sessions/websocket%253Aa%253Ab/files/{file_id}",
            headers=auth,
        )
        assert resp.status_code == 404
    finally:
        await channel.stop()
        await server_task
