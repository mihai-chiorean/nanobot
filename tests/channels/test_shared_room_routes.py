"""Shared-room HTTP contract (Ziggy-local, MIT-1010).

Pins the wire shape ``services/ziggy-control/internal/httpapi/shared_rooms.go``
depends on, plus the reinstated room-scoped ``/api/sessions/<key>/messages``
read.  Upstream deleted that route (``cdb2a474``) and its own test asserts 404;
these tests pin the *narrower* contract it was restored with -- room credentials
only, one session each, redacted body.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from websockets.datastructures import Headers

from nanobot.channels.websocket.rooms import SharedRoomStore
from nanobot.session.manager import SessionManager
from nanobot.webui.shared_rooms_http import SharedRoomRouter

SECRET = "tenant-issue-secret"
OWNER_CHAT = "chat_owner"
ROOM_CHAT = "11111111-2222-3333-4444-555555555555"
ROOM_ID = "room_" + "a" * 32
PARTICIPANT_ID = "participant_" + "b" * 32


class _Config:
    path = "/"
    token_issue_secret = SECRET
    token_ttl_s = 300
    shared_room_collaboration_enabled = False


class _Request:
    """The request shape the aiohttp transport hands the router."""

    def __init__(
        self,
        path: str,
        *,
        method: str = "POST",
        body: Any = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.path = path
        self.raw_path = path
        self.method = method
        self.body = json.dumps(body).encode() if body is not None else b""
        self.headers = Headers(list((headers or {}).items()))


def _authed(body: Any) -> _Request:
    return _Request("/auth/shared-rooms", body=body, headers={"X-Nanobot-Auth": SECRET})


@pytest.fixture
def sessions(tmp_path: Path) -> SessionManager:
    manager = SessionManager(tmp_path)
    session = manager.get_or_create(f"websocket:{OWNER_CHAT}")
    session.add_message("user", "private question", reasoning="chain of thought")
    session.add_message("assistant", "private answer")
    manager.save(session, fsync=True)
    return manager


@pytest.fixture
def router(sessions: SessionManager) -> SharedRoomRouter:
    return SharedRoomRouter(
        config=_Config(),
        sessions=sessions,
        store=SharedRoomStore(sessions, token_ttl_s=300),
    )


def _body(response: Any) -> Any:
    return json.loads(bytes(response.body).decode())


async def _create_room(router: SharedRoomRouter) -> Any:
    request = _authed(
        {
            "source_session_key": f"websocket:{OWNER_CHAT}",
            "chat_id": ROOM_CHAT,
            "room_id": ROOM_ID,
            "title": "Shared conversation",
            "owner_display_name": "Mihai",
        }
    )
    return await router.dispatch(request, "/auth/shared-rooms")


# --------------------------------------------------------------------------
# Control plane
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_room_requires_the_issue_secret(router: SharedRoomRouter) -> None:
    request = _Request(
        "/auth/shared-rooms",
        body={"chat_id": ROOM_CHAT, "room_id": ROOM_ID},
        headers={"X-Nanobot-Auth": "wrong"},
    )
    response = await router.dispatch(request, "/auth/shared-rooms")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_create_room_returns_the_control_plane_shape(router: SharedRoomRouter) -> None:
    response = await _create_room(router)
    assert response.status_code == 201
    payload = _body(response)
    assert payload == {
        "room_id": ROOM_ID,
        "session_key": f"websocket:{ROOM_CHAT}",
        "chat_id": ROOM_CHAT,
        "title": "Shared conversation",
    }


@pytest.mark.asyncio
async def test_create_room_is_not_idempotent(router: SharedRoomRouter) -> None:
    await _create_room(router)
    response = await _create_room(router)
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_collaborative_mode_is_refused_when_disabled(router: SharedRoomRouter) -> None:
    request = _authed(
        {
            "source_session_key": f"websocket:{OWNER_CHAT}",
            "chat_id": ROOM_CHAT,
            "room_id": ROOM_ID,
            "mode": "collaborative-v1",
        }
    )
    response = await router.dispatch(request, "/auth/shared-rooms")
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_token_issue_returns_the_client_shape(router: SharedRoomRouter) -> None:
    await _create_room(router)
    request = _Request(
        "/auth/shared-room-token",
        body={
            "room_id": ROOM_ID,
            "chat_id": ROOM_CHAT,
            "participant_id": PARTICIPANT_ID,
            "display_name": "Guest",
            "role": "contributor",
        },
        headers={"X-Nanobot-Auth": SECRET},
    )
    response = await router.dispatch(request, "/auth/shared-room-token")
    assert response.status_code == 200
    payload = _body(response)
    assert payload["token"].startswith("nbrt_")
    assert payload["ws_path"] == "/"
    assert payload["expires_in"] == 300
    assert payload["mode"] == "legacy"
    assert payload["capabilities"] == []


@pytest.mark.asyncio
async def test_owner_role_cannot_be_minted(router: SharedRoomRouter) -> None:
    await _create_room(router)
    request = _Request(
        "/auth/shared-room-token",
        body={
            "room_id": ROOM_ID,
            "chat_id": ROOM_CHAT,
            "participant_id": PARTICIPANT_ID,
            "display_name": "Guest",
            "role": "owner",
        },
        headers={"X-Nanobot-Auth": SECRET},
    )
    response = await router.dispatch(request, "/auth/shared-room-token")
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_token_issue_for_an_unknown_room_is_404(router: SharedRoomRouter) -> None:
    request = _Request(
        "/auth/shared-room-token",
        body={
            "room_id": ROOM_ID,
            "chat_id": ROOM_CHAT,
            "participant_id": PARTICIPANT_ID,
            "display_name": "Guest",
        },
        headers={"X-Nanobot-Auth": SECRET},
    )
    response = await router.dispatch(request, "/auth/shared-room-token")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_title_revision_protocol(router: SharedRoomRouter) -> None:
    await _create_room(router)

    def title_request(title: str, revision: int) -> _Request:
        return _Request(
            "/auth/shared-rooms/title",
            body={
                "room_id": ROOM_ID,
                "chat_id": ROOM_CHAT,
                "title": title,
                "title_revision": revision,
            },
            headers={"X-Nanobot-Auth": SECRET},
        )

    ok = await router.dispatch(title_request("v2", 2), "/auth/shared-rooms/title")
    assert ok.status_code == 200
    assert _body(ok)["title_revision"] == 2
    stale = await router.dispatch(title_request("v1", 1), "/auth/shared-rooms/title")
    assert stale.status_code == 409


@pytest.mark.asyncio
async def test_revoke_invalidates_tokens(router: SharedRoomRouter) -> None:
    await _create_room(router)
    token_response = await router.dispatch(
        _Request(
            "/auth/shared-room-token",
            body={
                "room_id": ROOM_ID,
                "chat_id": ROOM_CHAT,
                "participant_id": PARTICIPANT_ID,
                "display_name": "Guest",
            },
            headers={"X-Nanobot-Auth": SECRET},
        ),
        "/auth/shared-room-token",
    )
    token = _body(token_response)["token"]
    assert router.store.api_credential(token) is not None

    revoked = await router.dispatch(
        _Request(
            "/auth/shared-room-revoke",
            body={"room_id": ROOM_ID, "chat_id": ROOM_CHAT},
            headers={"X-Nanobot-Auth": SECRET},
        ),
        "/auth/shared-room-revoke",
    )
    assert revoked.status_code == 200
    assert _body(revoked)["invalidated_tokens"] == 2
    assert router.store.api_credential(token) is None


@pytest.mark.asyncio
async def test_preview_redacts_and_pins_a_snapshot(router: SharedRoomRouter) -> None:
    response = await router.dispatch(
        _Request(
            "/auth/shared-rooms/preview",
            body={"source_session_key": f"websocket:{OWNER_CHAT}"},
            headers={"X-Nanobot-Auth": SECRET},
        ),
        "/auth/shared-rooms/preview",
    )
    assert response.status_code == 200
    payload = _body(response)
    assert payload["message_count"] == 2
    assert len(payload["snapshot_sha256"]) == 64
    assert all("reasoning" not in message for message in payload["messages"])


@pytest.mark.asyncio
async def test_control_plane_fails_closed_without_a_secret(
    sessions: SessionManager,
) -> None:
    class _NoSecret(_Config):
        token_issue_secret = ""

    router = SharedRoomRouter(
        config=_NoSecret(),
        sessions=sessions,
        store=SharedRoomStore(sessions, token_ttl_s=300),
    )
    response = await router.dispatch(_authed({}), "/auth/shared-rooms")
    assert response.status_code == 503


# --------------------------------------------------------------------------
# Guest read: the reinstated /messages route
# --------------------------------------------------------------------------


async def _room_with_token(router: SharedRoomRouter) -> str:
    await _create_room(router)
    response = await router.dispatch(
        _Request(
            "/auth/shared-room-token",
            body={
                "room_id": ROOM_ID,
                "chat_id": ROOM_CHAT,
                "participant_id": PARTICIPANT_ID,
                "display_name": "Guest",
            },
            headers={"X-Nanobot-Auth": SECRET},
        ),
        "/auth/shared-room-token",
    )
    return _body(response)["token"]


@pytest.mark.asyncio
async def test_guest_reads_its_own_room(router: SharedRoomRouter) -> None:
    token = await _room_with_token(router)
    path = f"/api/sessions/websocket:{ROOM_CHAT}/messages"
    response = await router.dispatch(
        _Request(path, method="GET", headers={"Authorization": f"Bearer {token}"}),
        path,
    )
    assert response.status_code == 200
    payload = _body(response)
    assert payload["key"] == f"websocket:{ROOM_CHAT}"
    assert payload["metadata"]["room_id"] == ROOM_ID
    assert [m["content"] for m in payload["messages"]] == [
        "private question",
        "private answer",
    ]
    # The redaction boundary holds on the read path too.
    assert all("reasoning" not in m for m in payload["messages"])


@pytest.mark.asyncio
async def test_guest_cannot_read_another_session_over_http(
    router: SharedRoomRouter,
) -> None:
    token = await _room_with_token(router)
    path = f"/api/sessions/websocket:{OWNER_CHAT}/messages"
    response = await router.dispatch(
        _Request(path, method="GET", headers={"Authorization": f"Bearer {token}"}),
        path,
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_messages_route_rejects_an_unauthenticated_read(
    router: SharedRoomRouter,
) -> None:
    await _create_room(router)
    path = f"/api/sessions/websocket:{ROOM_CHAT}/messages"
    # No room token: not the room router's request. It falls through to the
    # owner route, which requires the owner API token (401 otherwise).
    assert await router.dispatch(_Request(path, method="GET"), path) is None
    response = await router.dispatch(
        _Request(path, method="GET", headers={"Authorization": "Bearer owner-api-token"}),
        path,
    )
    assert response is None


@pytest.mark.asyncio
async def test_revoked_credential_cannot_read(router: SharedRoomRouter) -> None:
    token = await _room_with_token(router)
    await router.dispatch(
        _Request(
            "/auth/shared-room-revoke",
            body={"room_id": ROOM_ID, "chat_id": ROOM_CHAT},
            headers={"X-Nanobot-Auth": SECRET},
        ),
        "/auth/shared-room-revoke",
    )
    path = f"/api/sessions/websocket:{ROOM_CHAT}/messages"
    response = await router.dispatch(
        _Request(path, method="GET", headers={"Authorization": f"Bearer {token}"}),
        path,
    )
    # A revoked token is no longer a room credential; the room router does not
    # serve it and the owner route rejects it (it is not an owner API token).
    assert response is None
    assert router.store.api_credential(token) is None


@pytest.mark.asyncio
async def test_unrelated_paths_fall_through(router: SharedRoomRouter) -> None:
    assert await router.dispatch(_Request("/api/sessions", method="GET"), "/api/sessions") is None
    assert await router.dispatch(_Request("/webui/bootstrap", method="GET"), "/webui/bootstrap") is None


@pytest.mark.asyncio
async def test_control_plane_routes_reject_get(router: SharedRoomRouter) -> None:
    for path in (
        "/auth/shared-rooms",
        "/auth/shared-rooms/title",
        "/auth/shared-room-token",
        "/auth/shared-room-revoke",
    ):
        response = await router.dispatch(_Request(path, method="GET"), path)
        assert response.status_code == 405, path


@pytest.mark.asyncio
async def test_bearer_secret_is_accepted(router: SharedRoomRouter) -> None:
    """ziggy-control sends the secret as ``Authorization: Bearer`` (shared_rooms.go:725)."""
    request = _Request(
        "/auth/shared-rooms",
        body={
            "source_session_key": f"websocket:{OWNER_CHAT}",
            "chat_id": ROOM_CHAT,
            "room_id": ROOM_ID,
            "mode": "legacy",
            "snapshot_message_count": None,
            "snapshot_sha256": "",
            "selected_results": None,
            "expires_at": None,
            "title": "Shared conversation",
            "owner_display_name": "Mihai",
        },
        headers={"Authorization": f"Bearer {SECRET}"},
    )
    response = await router.dispatch(request, "/auth/shared-rooms")
    assert response.status_code == 201
