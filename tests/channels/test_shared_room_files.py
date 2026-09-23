"""Room-guest published-file downloads over the shared-room routes (MIT-1407).

Production (``feat/shared-rooms`` @ 83028651) served
``GET /api/sessions/<key>/files/<id>`` from one handler that accepted a room
credential beside the owner token, so the product's ``ziggy-control`` guest
proxy (``shared_rooms.go`` forwards exactly
``/api/sessions/websocket:<chat>/files/<id>`` with the room bearer) got
bytes.  0.3.0 split the route: the owner half lives in ``ws_http``
(MIT-1030), and this file pins the room half on ``SharedRoomRouter`` --
without it every guest download 404s (the P11 bug).

The gate is the credential, never the path text: the route authenticates
via ``SharedRoomStore.api_credential`` and then compares the decoded
``key`` against ``credential.session_key`` -- the credential's own room
session -- so a request aimed at another room's session or the owner's
private conversation is a 404, while the query/matrix/fragment spellings
that survive routing onto this test surface are refused without ever
reaching the store.  The clone half is the other half of the fix:
``clone_session_for_shared_room`` rewrites transcript links and copies
grants, but only for publications whose server-side provenance record still
matches the published bytes -- a pasted or forged link in a message body
grants nothing and private data never rides along.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest

from nanobot.channels.websocket.rooms import SharedRoomStore
from nanobot.session.automation_turns import AUTOMATION_HISTORY_META
from nanobot.session.history_visibility import HIDDEN_HISTORY_META
from nanobot.session.manager import (
    _PUBLISHED_GRANTS_KEY,
    _PUBLISHED_MESSAGE_ID_KEY,
    _PUBLISHED_PROVENANCE_KEY,
    SessionManager,
)
from nanobot.webui.shared_rooms_http import SharedRoomRouter

SECRET = "tenant-issue-secret"
OWNER_CHAT = "chat_owner"
ROOM_CHAT = "11111111-2222-3333-4444-555555555555"
ROOM_ID = "room_" + "a" * 32
OTHER_ROOM_CHAT = "22222222-3333-4444-5555-666666666666"
OTHER_ROOM_ID = "room_" + "b" * 32
# A session that is never cloned into any room: its data must never ride along
# on a room credential, whichever pipeline reads the projection.
OTHER_PRIVATE_CHAT = "chat_never_shared"
PARTICIPANT_ID = "participant_" + "b" * 32
DOWNLOAD_NAME = "report.md"
PRIVATE_NOTE = "Private: ZERBA-DO-NOT-LEAK"


class _Config:
    path = "/"
    token_issue_secret = SECRET
    token_ttl_s = 300
    shared_room_collaboration_enabled = False


class _Request:
    """The request shape the aiohttp transport hands ``SharedRoomRouter.dispatch``."""

    def __init__(
        self,
        path: str,
        *,
        method: str = "GET",
        raw_path: str | None = None,
        body: bytes = b"",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.method = method
        self.path = path
        # The transport keeps the original target for routes whose authorization
        # is canonical (published files), exactly as ``TransportRequest`` does.
        self.raw_path = path if raw_path is None else raw_path
        self.body = body
        self.headers = headers or {}


def _room_key(chat_id: str) -> str:
    return f"websocket:{chat_id}"


def _enclosed_key(chat_id: str) -> str:
    return quote(_room_key(chat_id), safe="")


def _target(chat_id: str, file_id: str) -> str:
    return f"/api/sessions/{_enclosed_key(chat_id)}/files/{file_id}"


def _routing_path(raw_target: str) -> str:
    """What ``GatewayHTTPHandler._dispatch_http`` parses the target into."""
    from urllib.parse import urlsplit

    return urlsplit(raw_target).path or "/"


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _publish(
    sessions: SessionManager,
    key: str,
    *,
    name: str = DOWNLOAD_NAME,
    content: bytes,
    linked: bool = True,
) -> str:
    """Seed one publication the way the publish tool's success path does.

    Stores the snapshot, appends the assistant message that carries the
    canonical markdown link, and records the grant through the real
    ``grant_published_files`` so the server-side provenance entry (digest of
    the published bytes + the minute of the sharing message) is written
    exactly as in production.  Returns the minted id.
    """
    file_id = sessions.store_published_snapshot(name, content)
    session = sessions.get_or_create(key)
    body = f"Here is the report: [{name}](/api/sessions/{quote(key, safe='')}/files/{file_id})"
    session.add_message("assistant", body if linked else f"report filed for {file_id}")
    sessions.grant_published_files(session, {file_id: name}, messages=session.messages[-1:])
    sessions.save(session, fsync=True)
    return file_id


async def _create_room(
    router: SharedRoomRouter,
    source_key: str,
    chat_id: str,
    room_id: str,
) -> None:
    request = _Request(
        "/auth/shared-rooms",
        method="POST",
        headers={"X-Nanobot-Auth": SECRET},
        body=json.dumps(
            {
                "source_session_key": source_key,
                "chat_id": chat_id,
                "room_id": room_id,
                "title": "Shared",
                "owner_display_name": "Owner",
                "snapshot_message_count": None,
                "snapshot_sha256": "",
                "selected_results": None,
                "expires_at": None,
            }
        ).encode(),
    )
    response = await router.dispatch(request, "/auth/shared-rooms")
    assert response is not None
    assert response.status_code == 201, response.status_code


def _mint(store: SharedRoomStore, room_id: str, chat_id: str) -> str:
    token, _credential = store.mint(
        room_id=room_id,
        chat_id=chat_id,
        participant_id=PARTICIPANT_ID,
        display_name="Guest",
        role="contributor",
    )
    return token


async def _guest_get(router: SharedRoomRouter, raw_target: str, token: str | None) -> Any:
    headers = _headers(token) if token is not None else {}
    return await router.dispatch(
        _Request(raw_target, raw_path=raw_target, headers=headers),
        _routing_path(raw_target),
    )


@pytest.fixture
def sessions(tmp_path: Path) -> SessionManager:
    manager = SessionManager(tmp_path)
    owner = manager.get_or_create(_room_key(OWNER_CHAT))
    owner.add_message("user", "private question")
    owner.add_message("assistant", f"private answer; {PRIVATE_NOTE} kept in the source")
    manager.save(owner, fsync=True)
    return manager


@pytest.fixture
def store(sessions: SessionManager) -> SharedRoomStore:
    return SharedRoomStore(sessions, token_ttl_s=300)


@pytest.fixture
def router(sessions: SessionManager, store: SharedRoomStore) -> SharedRoomRouter:
    return SharedRoomRouter(config=_Config(), sessions=sessions, store=store)


# ---------------------------------------------------------------------------
# The room route serves its own room's publication to the joined guest --
# the route the product's guest proxy actually uses.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_guest_downloads_its_own_room_publication(
    sessions: SessionManager, store: SharedRoomStore, router: SharedRoomRouter
) -> None:
    await _create_room(router, _room_key(OWNER_CHAT), ROOM_CHAT, ROOM_ID)
    body = b"zebra observations, room-scoped\n"
    file_id = await _publish(sessions, _room_key(ROOM_CHAT), content=body)
    response = await _guest_get(router, _target(ROOM_CHAT, file_id), _mint(store, ROOM_ID, ROOM_CHAT))
    assert response is not None
    assert response.status_code == 200
    assert bytes(response.body) == body
    disposition = response.headers.get("Content-Disposition", "")
    assert "attachment" in disposition
    assert DOWNLOAD_NAME in disposition
    # Exact owner-route parity (``ws_http._handle_published_file``): the room
    # twin shares its header contract, and a client that re-provisions on a
    # miss must not see a different cache policy here.
    assert response.headers.get("Cache-Control") == "private, no-store"
    assert response.headers.get("X-Content-Type-Options") == "nosniff"


@pytest.mark.asyncio
async def test_foreign_or_revoked_credential_is_refused_the_download(
    sessions: SessionManager, store: SharedRoomStore, router: SharedRoomRouter
) -> None:
    """A token only ever authorizes its own room; the shareable helper enforces
    it by comparing the presented ``key`` against the credential's own
    assignment -- a wrong-domain request must be denied, not fetched-and-compared.
    """
    await _create_room(router, _room_key(OWNER_CHAT), ROOM_CHAT, ROOM_ID)
    body = b"room A secret\n"
    file_id = await _publish(sessions, _room_key(ROOM_CHAT), content=body)
    # The room's own credential downloads its file: liveness before revocation.
    token_a = _mint(store, ROOM_ID, ROOM_CHAT)
    live = await _guest_get(router, _target(ROOM_CHAT, file_id), token_a)
    assert live is not None and live.status_code == 200
    assert bytes(live.body) == body
    # A credential minted for room B, replayed against room A's published file,
    # is a cross-room leak attempt -- exactly what must not be served.  The
    # gate compares the presented key against the credential's OWN assignment;
    # a wrong-domain request is denied, never fetched-and-compared.
    await _create_room(router, _room_key(OWNER_CHAT), OTHER_ROOM_CHAT, OTHER_ROOM_ID)
    token_b = _mint(store, OTHER_ROOM_ID, OTHER_ROOM_CHAT)
    response = await _guest_get(router, _target(ROOM_CHAT, file_id), token_b)
    assert response is not None and response.status_code == 404
    # The same credential at its own room still works -- the refusal above is
    # about the target, not the token.
    file_b = await _publish(
        sessions, _room_key(OTHER_ROOM_CHAT), name="other.md", content=b"room B report\n"
    )
    ok = await _guest_get(router, _target(OTHER_ROOM_CHAT, file_b), token_b)
    assert ok is not None and ok.status_code == 200
    # Revocation is a control-plane transition: the route marks the room's
    # session metadata (``shared_room_revoked``), and ``is_active`` -- which
    # every credential path re-reads -- turns on that mark.  ``store.revoke``
    # alone only purges live pools and would let a re-minted credential serve,
    # which is the bug this leg must catch.  Drive the real route.
    revoked = await router.dispatch(
        _Request(
            "/auth/shared-room-revoke",
            method="POST",
            headers={"X-Nanobot-Auth": SECRET},
            body=json.dumps({"room_id": ROOM_ID, "chat_id": ROOM_CHAT}).encode(),
        ),
        "/auth/shared-room-revoke",
    )
    assert revoked is not None and revoked.status_code == 200
    # The token issued BEFORE revocation is dead at the gate...
    stale = await _guest_get(router, _target(ROOM_CHAT, file_id), token_a)
    assert stale is None or stale.status_code == 404, "pre-revocation token still serves"
    assert store.api_credential(token_a) is None, "pooled credential survived revoke"
    # ...and so is a token minted AFTER it: the metadata mark, not the pool
    # purge, is what refuses it.  A room whose revocation mark were lost would
    # serve bytes here -- the exact failure this leg pins.
    fresh = _mint(store, ROOM_ID, ROOM_CHAT)
    refused = await _guest_get(router, _target(ROOM_CHAT, file_id), fresh)
    assert refused is None or refused.status_code == 404, "revoked room must not serve files"
    assert store.api_credential(fresh) is None, "post-revocation credential resolves"
    # Revoking room A must not reach room B: the other room's credential and
    # file keep working off the same store -- a broad-spectrum sweep keyed on
    # the revoked room would wrongly damage this unrelated entry.
    untouched = await _guest_get(router, _target(OTHER_ROOM_CHAT, file_b), token_b)
    assert untouched is not None and untouched.status_code == 200
    assert bytes(untouched.body) == b"room B report\n"


@pytest.mark.asyncio
async def test_unknown_or_unbound_bearer_does_not_download(
    sessions: SessionManager, store: SharedRoomStore, router: SharedRoomRouter
) -> None:
    await _create_room(router, _room_key(OWNER_CHAT), ROOM_CHAT, ROOM_ID)
    file_id = await _publish(sessions, _room_key(ROOM_CHAT), content=b"quiet\n")
    target = _target(ROOM_CHAT, file_id)
    for bearer in ("", "bogus-not-a-token", "nbrt_" + "0" * 32):
        response = await _guest_get(router, target, bearer)
        assert response is None or response.status_code == 404, bearer


# ---------------------------------------------------------------------------
# The shared ``/api/sessions/<key>/messages`` read: authentication happens by
# resolving the stored room member (never a snapshot / no 403); a request with
# no credential, or a foreign one, is refused without reaching the store.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_messages_of_the_room_are_shared_for_a_valid_token(
    sessions: SessionManager, store: SharedRoomStore, router: SharedRoomRouter
) -> None:
    await _create_room(router, _room_key(OWNER_CHAT), ROOM_CHAT, ROOM_ID)
    await _publish(sessions, _room_key(ROOM_CHAT), content=b"served content\n")
    token = _mint(store, ROOM_ID, ROOM_CHAT)
    messages_target = f"/api/sessions/{_enclosed_key(ROOM_CHAT)}/messages"
    response = await router.dispatch(
        _Request(messages_target, raw_path=messages_target, headers=_headers(token)),
        _routing_path(messages_target),
    )
    assert response is not None
    assert response.status_code == 200
    data = json.loads(bytes(response.body).decode())
    assert isinstance(data.get("messages"), list)
    assert len(data["messages"]) >= 1


@pytest.mark.asyncio
async def test_messages_require_a_valid_token_revoked_after_issue(
    sessions: SessionManager, store: SharedRoomStore, router: SharedRoomRouter
) -> None:
    await _create_room(router, _room_key(OWNER_CHAT), ROOM_CHAT, ROOM_ID)
    messages_target = f"/api/sessions/{_enclosed_key(ROOM_CHAT)}/messages"
    # No credential at all -> refused before the store; the route must not
    # serve the conversation unauthenticated (falls through to the owner route).
    no_auth = await router.dispatch(
        _Request(messages_target, raw_path=messages_target), _routing_path(messages_target)
    )
    assert no_auth is None or no_auth.status_code in (401, 404)
    # A token revoked after being issued behaves like a dead credential.
    token = _mint(store, ROOM_ID, ROOM_CHAT)
    live = await router.dispatch(
        _Request(messages_target, raw_path=messages_target, headers=_headers(token)),
        _routing_path(messages_target),
    )
    assert live is not None and live.status_code == 200
    store.revoke(room_id=ROOM_ID, chat_id=ROOM_CHAT)
    revoked = await router.dispatch(
        _Request(messages_target, raw_path=messages_target, headers=_headers(token)),
        _routing_path(messages_target),
    )
    assert revoked is None or revoked.status_code in (401, 404)


@pytest.mark.asyncio
async def test_token_for_one_room_cannot_read_another_rooms_messages(
    sessions: SessionManager, store: SharedRoomStore, router: SharedRoomRouter
) -> None:
    await _create_room(router, _room_key(OWNER_CHAT), ROOM_CHAT, ROOM_ID)
    await _create_room(router, _room_key(OWNER_CHAT), OTHER_ROOM_CHAT, OTHER_ROOM_ID)
    token_b = _mint(store, OTHER_ROOM_ID, OTHER_ROOM_CHAT)
    messages_target = f"/api/sessions/{_enclosed_key(ROOM_CHAT)}/messages"
    response = await router.dispatch(
        _Request(messages_target, raw_path=messages_target, headers=_headers(token_b)),
        _routing_path(messages_target),
    )
    assert response is None or response.status_code == 404


@pytest.mark.asyncio
async def test_clone_rewrites_links_and_copies_grants(
    sessions: SessionManager, store: SharedRoomStore, router: SharedRoomRouter
) -> None:
    """After a share, a guest downloads the source conversation's published
    file through the room key: the clone rewrote the link and carried the
    grant, so the room route serves it with no owner involvement."""
    body = b"shared report body\n"
    file_id = await _publish(sessions, _room_key(OWNER_CHAT), content=body)
    source = sessions.read_session_file(_room_key(OWNER_CHAT))
    source_grants = (source.get("metadata") or {}).get(_PUBLISHED_GRANTS_KEY) or {}
    source_prov = (source.get("metadata") or {}).get(_PUBLISHED_PROVENANCE_KEY) or {}
    assert file_id in source_grants and file_id in source_prov
    # Precondition: the pre-clone message carries the OWNER's key in the link.
    pre = source["messages"][-1]
    assert f"/api/sessions/{_enclosed_key(OWNER_CHAT)}/files/{file_id}" in pre["content"]
    await _create_room(router, _room_key(OWNER_CHAT), ROOM_CHAT, ROOM_ID)
    clone = sessions.read_session_file(_room_key(ROOM_CHAT))
    assert clone is not None
    # The link was rewritten to the room's own address.  Links are built in the
    # canonical percent-encoded spelling (the publish helper quotes each key),
    # so the comparison must use the encoded form -- and it must compare the
    # FULL ref (collection + room id + file id), not just the id tail.
    owner_ref = f"/api/sessions/{_enclosed_key(OWNER_CHAT)}/files/{file_id}"
    room_ref = f"/api/sessions/{_enclosed_key(ROOM_CHAT)}/files/{file_id}"
    assistant = [m for m in clone["messages"] if m.get("role") == "assistant"]
    assert any(room_ref in m["content"] for m in assistant), (
        "clone must rewrite transcript links to the room key (canonical encoded form)"
    )
    # The unshared source ref must be gone from every rewritten line -- comparing
    # raw/encoded key prefixes against each item is what catches a link that was
    # left pointed at the source collection or at the wrong-room entry.
    assert all(
        owner_ref not in m["content"] for m in assistant
    ), "clone must leave no source-keyed link in a room transcript"
    assert all(
        _enclosed_key(OWNER_CHAT) not in m["content"] for m in assistant
    ), "clone must not smuggle the owner's encoded session key into a room line"
    # The room's copy must reference the room's own session on every rewritten
    # line: the line's collection segment is the room id, never the source id.
    for m in assistant:
        if room_ref in m["content"]:
            assert _enclosed_key(ROOM_CHAT) in m["content"]
            assert _enclosed_key(OWNER_CHAT) not in m["content"]
    # The grant was copied: the room session's own metadata carries it, with
    # the same filename and content as the source's.
    clone_grants = (clone.get("metadata") or {}).get(_PUBLISHED_GRANTS_KEY) or {}
    assert file_id in clone_grants, "clone_session_for_shared_room must copy the grant"
    assert clone_grants[file_id] == source_grants[file_id]
    # The stored provenance for the room copy is a translation of the source's,
    # never a re-mint or a hand-written stand-in; verify each recorded digest
    # against an INDEPENDENTLY recomputed hash of the very content it now stores
    # -- not against the other session's record.  A copied file's digest must
    # match the blob's bytes (here the served body == the original body), and a
    # translated line's digest must move because its content changed.
    clone_prov = (clone.get("metadata") or {}).get(_PUBLISHED_PROVENANCE_KEY) or {}
    assert file_id in clone_prov, "clone must carry provenance for the copied grant"
    # And the guest can actually fetch the rewritten id (server re-derives path
    # from the claim + store; the id itself is never a path the client supplies).
    token = _mint(store, ROOM_ID, ROOM_CHAT)
    response = await _guest_get(router, _target(ROOM_CHAT, file_id), token)
    assert response is not None and response.status_code == 200
    assert bytes(response.body) == body
    # The blob is unchanged since publish: the bytes the route served are the
    # bytes the snapshot store holds for the claimed id -- verified against the
    # stored payload rather than trusting the route's own read path.
    assert sessions._snapshot_bytes(file_id) == body, (
        "served bytes must match the stored blob for the claimed id"
    )
    src_records = source_prov[file_id]
    src_records = src_records if isinstance(src_records, list) else [src_records]
    clone_records = clone_prov[file_id]
    clone_records = clone_records if isinstance(clone_records, list) else [clone_records]
    # Exactly one provenance entry per side for this single publication -- the
    # entry that is first in each list is the one under test (not the whole list).
    assert len(src_records) == 1 and len(clone_records) == 1
    src_record = src_records[0]
    clone_record = clone_records[0]
    # Independently recompute the digest each side claims to have recorded, from
    # the content that side actually stores -- never from the other side's record.
    assert src_record["message_sha256"] == hashlib.sha256(
        pre["content"].encode("utf-8")
    ).hexdigest(), "source provenance must match the source's own content"
    room_message = next(m for m in assistant if room_ref in m["content"])
    assert clone_record["message_sha256"] == hashlib.sha256(
        room_message["content"].encode("utf-8")
    ).hexdigest(), "room provenance must match the room's own rewritten content"
    # The translation changed the line (owner key -> room key), so its digest must
    # have moved with it; equal digests would mean the copy was not re-recorded.
    assert clone_record["message_sha256"] != src_record["message_sha256"], (
        "translated room copy must carry its own content digest, not the source's"
    )
    assert clone_record["timestamp"] == src_record["timestamp"], (
        "translation must preserve the sharing moment"
    )
    # The room copy is a different stored message (the source's id is not
    # reused), so the record is a translation, not a copy of the live state.
    marker = room_message.get(_PUBLISHED_MESSAGE_ID_KEY)
    assert marker is not None, "clone must stamp the server-minted marker on the room copy"
    assert marker == clone_record["message_id"], "provenance must record the room's own message id"
    assert marker != src_record["message_id"], "room copy must not reuse the source marker"
    # The raw stored data still holds the marker; the served projection must
    # strip it.
    raw = sessions.read_session_file(_room_key(ROOM_CHAT))
    assert _PUBLISHED_MESSAGE_ID_KEY in json.dumps(raw["messages"])
    token2 = _mint(store, ROOM_ID, ROOM_CHAT)
    served = await router.dispatch(
        _Request(
            f"/api/sessions/{_enclosed_key(ROOM_CHAT)}/messages",
            raw_path=f"/api/sessions/{_enclosed_key(ROOM_CHAT)}/messages",
            headers=_headers(token2),
        ),
        f"/api/sessions/{_enclosed_key(ROOM_CHAT)}/messages",
    )
    assert served is not None and served.status_code == 200
    body_text = bytes(served.body).decode()
    assert _PUBLISHED_MESSAGE_ID_KEY not in body_text, "served projection must strip the marker"


@pytest.mark.asyncio
async def test_forged_link_without_provenance_is_not_copied(
    sessions: SessionManager, store: SharedRoomStore, router: SharedRoomRouter
) -> None:
    """A message that merely *mentions* a file id, with no server-recorded
    publication for it, must not have its grant copied into the room."""
    # Forge a claim that references a well-formed but never-published id.
    fake_id = hashlib.sha256(b"never-published").hexdigest()[:32]
    owner = sessions.get_or_create(_room_key(OWNER_CHAT))
    owner.add_message(
        "assistant",
        f"mention: [report](/api/sessions/{_enclosed_key(OWNER_CHAT)}/files/{fake_id})",
    )
    sessions.save(owner, fsync=True)
    await _create_room(router, _room_key(OWNER_CHAT), ROOM_CHAT, ROOM_ID)
    clone = sessions.read_session_file(_room_key(ROOM_CHAT))
    clone_grants = (clone.get("metadata") or {}).get(_PUBLISHED_GRANTS_KEY) or {}
    assert fake_id not in clone_grants, "unrecorded id must not become a grant via clone"
    token = _mint(store, ROOM_ID, ROOM_CHAT)
    response = await _guest_get(router, _target(ROOM_CHAT, fake_id), token)
    assert response is None or response.status_code == 404


@pytest.mark.asyncio
async def test_ungranted_file_cannot_be_reached_by_path_or_principal(
    sessions: SessionManager, store: SharedRoomStore, router: SharedRoomRouter
) -> None:
    """The positive control for the same shape: a file published into the room
    IS reachable through the room route, while the owner's *private* file is
    not reachable by the guest via any spelling of the request."""
    await _create_room(router, _room_key(OWNER_CHAT), ROOM_CHAT, ROOM_ID)
    room_id = await _publish(sessions, _room_key(ROOM_CHAT), content=b"room owned\n")
    private_id = await _publish(
        sessions, _room_key(OWNER_CHAT), name="private.md", content=b"owner only\n"
    )
    token = _mint(store, ROOM_ID, ROOM_CHAT)
    served = await _guest_get(router, _target(ROOM_CHAT, room_id), token)
    assert served is not None and served.status_code == 200
    # The owner's private file is not in the room: the guest must not fetch it
    # through the room route, however the request is spelled.
    private_spelling = _target(ROOM_CHAT, private_id)
    blocked = await _guest_get(router, private_spelling, token)
    assert blocked is None or blocked.status_code == 404
    # Same id, owner key, room credential: the session gate must refuse it --
    # the request is aimed at a session the credential does not authorize.
    direct = await _guest_get(router, _target(OWNER_CHAT, private_id), token)
    assert direct is None or direct.status_code == 404
    # A different, unguessable id in the room is not listed or guessable: the
    # route only ever serves ids the session's grant store holds.
    other = hashlib.sha256(b"unheard-of zebra").hexdigest()[:32]
    response = await _guest_get(router, _target(ROOM_CHAT, other), token)
    assert response is None or response.status_code == 404


@pytest.mark.asyncio
async def test_path_norm_and_case_and_query_cannot_reach_the_store(
    sessions: SessionManager, store: SharedRoomStore, router: SharedRoomRouter
) -> None:
    """Traversal / dot-segment / percent-encoded / query-suffix spellings are
    refused at the route, not re-routed into the store."""
    await _create_room(router, _room_key(OWNER_CHAT), ROOM_CHAT, ROOM_ID)
    body = b"canonical bytes\n"
    file_id = await _publish(sessions, _room_key(ROOM_CHAT), content=body)
    token = _mint(store, ROOM_ID, ROOM_CHAT)
    canonical = _target(ROOM_CHAT, file_id)
    # Positive control for the very same target string.
    ok = await _guest_get(router, canonical, token)
    assert ok is not None and ok.status_code == 200
    refused = [
        # Segment-traversal: escape the files collection via a dot-segment.
        f"/api/sessions/{_enclosed_key(ROOM_CHAT)}/files/..%2F{file_id}",
        f"/api/sessions/{_enclosed_key(ROOM_CHAT)}/files/../{file_id}",
        f"/api/sessions/{_enclosed_key(ROOM_CHAT)}/files/%2E%2E%2F{file_id}",
        # Ambiguous: encoded separators inside the *file id*.
        f"/api/sessions/{_enclosed_key(ROOM_CHAT)}/files/%2E{file_id}",
        f"/api/sessions/{_enclosed_key(ROOM_CHAT)}/files/{file_id}%2F",
        f"/api/sessions/{_enclosed_key(ROOM_CHAT)}/files/{file_id}%2Fx",
        # Ambiguous: encoded separator inside the collection segment.
        f"/api/sessions/{_enclosed_key(ROOM_CHAT)}%2Ffiles%2F{file_id}",
        f"/api/sessions/{_enclosed_key(ROOM_CHAT)}/%2E%2E%2Ffiles%2F{file_id}",
        # Dot-segment in the session segment.
        f"/api/sessions/./..%2Fwebsocket%3A{ROOM_CHAT}/files/{file_id}",
        # Case variation on the id (ids are lowercase hex; must not match).
        f"/api/sessions/{_enclosed_key(ROOM_CHAT)}/files/{file_id.upper()}",
        # Suffix query/fragment on the id: the id must stay the whole segment.
        f"/api/sessions/{_enclosed_key(ROOM_CHAT)}/files/{file_id}?redact=1",
        f"/api/sessions/{_enclosed_key(ROOM_CHAT)}/files/{file_id}#frag",
        f"/api/sessions/{_enclosed_key(ROOM_CHAT)}/files/{file_id}%3Fx",
        f"/api/sessions/{_enclosed_key(ROOM_CHAT)}/files/{file_id}%23x",
    ]
    for raw_target in refused:
        response = await _guest_get(router, raw_target, token)
        assert response is None or response.status_code == 404, raw_target


@pytest.mark.asyncio
async def test_listed_public_pairs_cover_every_recorded_grant(
    sessions: SessionManager, store: SharedRoomStore, router: SharedRoomRouter
) -> None:
    """``_shareable_room_messages`` output must cover every recorded
    publication.  An empty result would vaciously pass the equality check, so
    each room first publishes a bounded number of files and the test asserts
    non-zero pairs -- a result with no entries would fail here, not the
    feature."""
    await _create_room(router, _room_key(OWNER_CHAT), ROOM_CHAT, ROOM_ID)
    room_key = _room_key(ROOM_CHAT)
    mine = [
        await _publish(
            sessions,
            room_key,
            name=f"r{i}.md",
            content=f"recorded body {i}\n".encode(),
        )
        for i in range(3)
    ]
    room = sessions.read_session_file(room_key)
    recorded = (room.get("metadata") or {}).get(_PUBLISHED_GRANTS_KEY) or {}
    assert len(recorded) == 3, "the publish path must record every publication"
    token = _mint(store, ROOM_ID, ROOM_CHAT)
    for file_id in mine:
        response = await _guest_get(router, _target(ROOM_CHAT, file_id), token)
        assert response is not None and response.status_code == 200, file_id


@pytest.mark.asyncio
async def test_content_redaction_at_the_source(
    sessions: SessionManager, store: SharedRoomStore, router: SharedRoomRouter
) -> None:
    """The room projection must never carry data the credential does not authorize.

    Production ``_shareable_messages`` (feat/shared-rooms @ 83028651) copies
    shared content *verbatim* and filters by role allow-list, hidden/automation
    markers and the internal-field allow-list -- it has no content-masking
    step, so a canary planted in shared conversation content is legitimately
    visible and asserting otherwise would contradict the reference.  The real
    never-served positions are: hidden-history and automation-marker records
    (dropped whole), internal fields such as ``toolResults``/``reasoning_content``
    (stripped field-wise while the turn stays visible), and any session the
    credential does not authorize.  Those are what this test pins, with the
    canary placed only there -- and it re-checks the raw store so the absence
    is proven non-vacuous (the marker exists, it was just never served).
    """
    # A second, entirely unshared session: its data must never ride along on
    # the room credential, no matter which pipeline reads the projection.
    other = sessions.get_or_create(_room_key(OTHER_PRIVATE_CHAT))
    other.add_message("user", f"unseeded secret for {OTHER_PRIVATE_CHAT}")
    other.add_message("assistant", f"UNSHARED-DATA {PRIVATE_NOTE} {OTHER_PRIVATE_CHAT}")
    sessions.save(other, fsync=True)

    # Plant the canaries in never-served positions on the SOURCE (pre-share):
    # a hidden-history turn, an automation turn, and an internal tool field on
    # an otherwise-visible turn.
    owner = sessions.get_or_create(_room_key(OWNER_CHAT))
    owner.add_message("assistant", f"HIDDEN-LEAK {PRIVATE_NOTE}", **{HIDDEN_HISTORY_META: True})
    owner.add_message("assistant", f"AUTOMATED-LEAK {PRIVATE_NOTE}", **{AUTOMATION_HISTORY_META: True})
    owner.add_message(
        "user",
        f"visible turn for {OWNER_CHAT}",
        toolResults=[{"output": f"TOOLSECRET {PRIVATE_NOTE}"}],
        reasoning_content=f"REASONING-SECRET {PRIVATE_NOTE}",
    )
    sessions.save(owner, fsync=True)

    await _create_room(router, _room_key(OWNER_CHAT), ROOM_CHAT, ROOM_ID)
    token = _mint(store, ROOM_ID, ROOM_CHAT)

    # The projection (not the raw file) is what is served on the messages read.
    messages_target = f"/api/sessions/{_enclosed_key(ROOM_CHAT)}/messages"
    served = await router.dispatch(
        _Request(messages_target, raw_path=messages_target, headers=_headers(token)),
        _routing_path(messages_target),
    )
    assert served is not None and served.status_code == 200
    data = json.loads(bytes(served.body).decode())
    served_text = bytes(served.body).decode()

    # (1) Canaries in hidden/automation/internal positions never surface.
    assert "HIDDEN-LEAK" not in served_text
    assert "AUTOMATED-LEAK" not in served_text
    assert "TOOLSECRET" not in served_text
    assert "REASONING-SECRET" not in served_text
    # The internal marker field is stripped by the allow-list projection even
    # though the record was copied.
    assert _PUBLISHED_MESSAGE_ID_KEY not in served_text
    assert "toolResults" not in served_text and "reasoning_content" not in served_text

    # (2) The unshared session's data never rode along on the room's projection.
    assert "UNSHARED-DATA" not in served_text
    assert OTHER_PRIVATE_CHAT not in served_text

    # (3) Non-vacuous: the canaries really were persisted in the source/other
    # stores (raw store read bypassing the projection) -- so their absence above
    # is a redaction result, not an artifact of seeding nothing.
    raw_owner = sessions.read_session_file(_room_key(OWNER_CHAT))
    raw_text = json.dumps(raw_owner)
    assert "HIDDEN-LEAK" in raw_text and "AUTOMATED-LEAK" in raw_text
    assert "TOOLSECRET" in raw_text and "REASONING-SECRET" in raw_text
    raw_other = sessions.read_session_file(_room_key(OTHER_PRIVATE_CHAT))
    assert "UNSHARED-DATA" in json.dumps(raw_other)

    # (4) The projection is COMPLETE: entry count equals the independently
    # computed number of authorized shareable entries.  The fixture + test
    # seeded the source with exactly: 2 pre-share shared turns (the fixture's
    # private Q/A pair) + the hidden + automation turns (dropped) + the one
    # visible tool turn; and the room publishes its own 1 assistant turn after
    # sharing.  Counting authorized user/assistant records on the room's raw
    # store -- excluding hidden/automation markers, which the route drops --
    # gives the reference; it is computed here, not returned by the route.
    room_raw = sessions.read_session_file(_room_key(ROOM_CHAT))
    expected = [
        m
        for m in room_raw.get("messages", [])
        if m.get("role") in ("user", "assistant")
        and not m.get(HIDDEN_HISTORY_META)
        and not m.get(AUTOMATION_HISTORY_META)
    ]
    assert len(data["messages"]) == len(expected), (
        f"projection dropped/added entries: served={len(data['messages'])} "
        f"expected={len(expected)}"
    )
    # And each served entry preserves its claimed id/title pairing -- the served
    # items must match the FULL authorized set, not a subset.
    served_ids = [m.get("client_message_id") or m.get("timestamp") for m in data["messages"]]
    expected_ids = [m.get("client_message_id") or m.get("timestamp") for m in expected]
    assert served_ids == expected_ids

    # (5) A published file's stored content is served verbatim -- the bytes the
    # owner wrote are the bytes the room member gets, unchanged by any pipeline.
    secret = b"balance sheet, tenant-scoped\n"
    file_id = await _publish(sessions, _room_key(ROOM_CHAT), name="q3.md", content=secret)
    response = await _guest_get(router, _target(ROOM_CHAT, file_id), token)
    assert response is not None and response.status_code == 200
    assert bytes(response.body) == secret
