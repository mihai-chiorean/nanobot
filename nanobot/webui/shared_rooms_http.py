"""Shared-room HTTP routes (Ziggy-local, MIT-1010).

The snapshot served these from the 3267-line ``channels/websocket.py``.  0.3.0
split that file into ``channels/websocket/runtime.py`` (transport) and
``webui/ws_http.py`` (HTTP), so the room routes live here and
``GatewayHTTPHandler._dispatch_resolved`` delegates to them.

Two authorization families, deliberately kept apart:

* ``/auth/shared-room*`` -- the **control plane**.  ``ziggy-control`` holds the
  tenant ``tokenIssueSecret``; nobody else can create, token, retitle or revoke
  a room.  These are POSTs with JSON bodies, which is why the aiohttp transport
  is required when shared rooms are enabled (see ``channels/websocket/transport.py``).
* ``/api/sessions/<key>/messages`` -- the **guest read**.  Upstream deleted this
  route in favour of ``/webui-thread`` (``cdb2a474``).  It is reinstated here
  for room credentials **only**: an ``nbrt_`` bearer reads exactly its own
  room session, projected through ``shareable_messages``.  Owner and
  API tokens are not accepted; those callers migrate to ``/webui-thread``.
* ``/api/sessions/<key>/files/<id>`` -- the **guest download**.  The room twin
  of the owner publication route in ``ws_http.py`` (MIT-1030): an ``nbrt_``
  bearer serves one granted snapshot, but only from the credential's own room
  session -- other keys, including the owner's private conversation, answer
  the same 404 as a missing publication (MIT-1407).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from contextlib import suppress
from typing import Any
from urllib.parse import quote, unquote

from loguru import logger
from websockets.http11 import Request as WsRequest
from websockets.http11 import Response

from nanobot.channels.websocket.rooms import (
    ROOM_MODE_COLLABORATIVE,
    ROOM_MODE_LEGACY,
    ROOM_MODES,
    SharedRoomStore,
    valid_display_name,
    valid_participant_id,
    valid_room_id,
)
from nanobot.webui.http_utils import (
    bearer_token,
    http_error,
    http_json_response,
    http_response,
    issue_route_secret_matches,
)
from nanobot.webui.session_identity import (
    is_valid_webui_chat_id,
    is_webui_session_key,
    webui_session_key,
)

# Preview responses are bounded so a huge conversation fails visibly instead of
# being shared partially unseen.
MAX_PREVIEW_BYTES = 192 * 1024
MAX_SELECTED_RESULTS = 10
MAX_SELECTED_RESULT_BYTES = 16_000

SESSION_KEY_RE = re.compile(r"^[A-Za-z0-9_:.@+-]{1,512}$")

# Same grammar as the owner route's publication ids (``ws_http`` MIT-1030 and
# ``SessionManager._PUBLISHED_FILE_ID_RE``): a server-minted lowercase hex id.
# The room route re-checks the literal request path against this shape so an
# un-decoded ``%2F`` can never smuggle a different target into the lookup.
_PUBLISHED_FILE_ID_RE = re.compile(r"^[0-9a-f]{32}$")

ROOM_EDITORIAL_ACTIONS = frozenset(
    {"preview", "upgrade", "state", "prepare", "approve", "publish", "decline"}
)


def request_json(request: Any) -> dict[str, Any] | None:
    """Parse a JSON request body. ``{}`` for an empty body, ``None`` for garbage."""
    body = getattr(request, "body", b"") or b""
    if not body:
        return {}
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def valid_session_key(value: Any) -> bool:
    return isinstance(value, str) and SESSION_KEY_RE.fullmatch(value) is not None


def valid_chat_id(value: Any) -> bool:
    return is_valid_webui_chat_id(value)


def valid_room_title(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    stripped = value.strip()
    return 1 <= len(stripped) <= 120 and all(ord(ch) >= 32 and ch != "\x7f" for ch in stripped)


class SharedRoomRouter:
    """Serve the shared-room control plane and the room-scoped session read."""

    def __init__(
        self,
        *,
        config: Any,
        sessions: Any,
        store: SharedRoomStore,
        channel: Any = None,
    ) -> None:
        self.config = config
        self.sessions = sessions
        self.store = store
        self.channel = channel

    # -- dispatch -----------------------------------------------------------

    async def dispatch(self, request: WsRequest, path: str) -> Response | None:
        method = getattr(request, "method", "GET")

        if path == "/auth/shared-rooms":
            if method != "POST":
                return http_error(405, "Method Not Allowed")
            return await self.create_room(request)
        if path == "/auth/shared-rooms/title":
            if method != "POST":
                return http_error(405, "Method Not Allowed")
            return self.set_title(request)
        if path == "/auth/shared-room-token":
            if method != "POST":
                return http_error(405, "Method Not Allowed")
            return self.issue_token(request)
        if path == "/auth/shared-room-revoke":
            if method != "POST":
                return http_error(405, "Method Not Allowed")
            return await self.revoke_room(request)
        if path.startswith("/auth/shared-rooms/"):
            action = path.rsplit("/", 1)[-1]
            if action in ROOM_EDITORIAL_ACTIONS:
                if method != "POST":
                    return http_error(405, "Method Not Allowed")
                return await self.editorial(request, action)

        match = re.fullmatch(r"/api/sessions/([^/]+)/messages", path)
        if match:
            if method != "GET":
                return http_error(405, "Method Not Allowed")
            return self.room_session_messages(request, match.group(1))

        match = re.fullmatch(r"/api/sessions/([^/]+)/files/([^/]+)$", path)
        if match:
            if method != "GET":
                return http_error(405, "Method Not Allowed")
            return await self.room_published_file(request, match.group(1), match.group(2))
        return None

    # -- shared helpers -----------------------------------------------------

    def _control_plane_guard(self, request: WsRequest) -> Response | None:
        secret = str(getattr(self.config, "token_issue_secret", "") or "").strip()
        if not secret:
            # Fail closed: without a shared secret every caller would be trusted.
            return http_error(503, "room issuance is unavailable")
        if not issue_route_secret_matches(request.headers, secret):
            return http_error(401, "Unauthorized")
        if self.sessions is None:
            return http_error(503, "session manager unavailable")
        return None

    def _room_guard(self, room_id: Any, chat_id: Any) -> Response | None:
        if not valid_room_id(room_id):
            return http_error(400, "invalid room_id")
        if not valid_chat_id(chat_id):
            return http_error(400, "invalid chat_id")
        metadata = self.store.room_metadata(str(chat_id))
        if metadata.get("shared_room") is not True or metadata.get("room_id") != room_id:
            return http_error(404, "room not found")
        if not self.store.is_active(str(chat_id)):
            return http_error(410, "Room expired or revoked")
        return None

    # -- control plane ------------------------------------------------------

    async def create_room(self, request: WsRequest) -> Response:
        guard = self._control_plane_guard(request)
        if guard is not None:
            return guard
        body = request_json(request)
        if body is None:
            return http_error(400, "invalid JSON body")

        source_key = body.get("source_session_key")
        chat_id = body.get("chat_id")
        room_id = body.get("room_id")
        if not valid_session_key(source_key) or not is_webui_session_key(str(source_key)):
            return http_error(400, "invalid source_session_key")
        if not valid_chat_id(chat_id):
            return http_error(400, "invalid chat_id")
        if not valid_room_id(room_id):
            return http_error(400, "invalid room_id")

        title = str(body.get("title") or "Shared conversation").strip()
        owner_display_name = str(body.get("owner_display_name") or "Owner").strip()
        if not valid_room_title(title) or not valid_display_name(owner_display_name):
            return http_error(400, "invalid room metadata")

        mode = body.get("mode", ROOM_MODE_LEGACY)
        if mode not in ROOM_MODES or (
            mode == ROOM_MODE_COLLABORATIVE
            and not getattr(self.config, "shared_room_collaboration_enabled", False)
        ):
            return http_error(409, "Room mode unavailable")

        # ziggy-control builds this payload as a Go map, so an empty
        # []map[string]string marshals to JSON ``null`` rather than being
        # omitted (shared_rooms.go:218). Treat null as "none supplied".
        selected_results = body.get("selected_results") or []
        if (
            not isinstance(selected_results, list)
            or len(selected_results) > MAX_SELECTED_RESULTS
            or any(
                not isinstance(item, dict)
                or not isinstance(item.get("content"), str)
                or len(item["content"].encode()) > MAX_SELECTED_RESULT_BYTES
                for item in selected_results
            )
        ):
            return http_error(400, "Invalid selected results")

        destination_key = webui_session_key(str(chat_id))
        try:
            await asyncio.to_thread(
                self.sessions.clone_session_for_shared_room,
                str(source_key),
                destination_key,
                metadata={
                    "shared_room": True,
                    "room_mode": mode,
                    "shared_room_expires_at": body.get("expires_at"),
                    "room_id": room_id,
                    "title": title,
                    "title_user_defined": True,
                    "shared_room_title_revision": 0,
                    "owner_display_name": owner_display_name,
                },
                owner_display_name=owner_display_name,
                snapshot_message_count=body.get("snapshot_message_count"),
                snapshot_sha256=body.get("snapshot_sha256"),
            )
        except ValueError:
            return http_error(409, "Preview changed. Review it again.")
        except FileNotFoundError:
            return http_error(404, "source session not found")
        except FileExistsError:
            return http_error(409, "room session already exists")
        except Exception:
            logger.exception("failed to create shared room session {}", room_id)
            return http_error(500, "failed to create room")

        if selected_results:
            destination = self.sessions.get_or_create(destination_key)
            for item in selected_results:
                destination.add_message("assistant", item["content"], room_publication=True)
            self.sessions.save(destination, fsync=True)

        return http_json_response(
            {
                "room_id": room_id,
                "session_key": destination_key,
                "chat_id": chat_id,
                "title": title,
            },
            status=201,
        )

    def set_title(self, request: WsRequest) -> Response:
        guard = self._control_plane_guard(request)
        if guard is not None:
            return guard
        body = request_json(request)
        if body is None:
            return http_error(400, "invalid JSON body")
        room_id = body.get("room_id")
        chat_id = body.get("chat_id")
        title = body.get("title")
        revision = body.get("title_revision")
        if not valid_room_id(room_id):
            return http_error(400, "invalid room_id")
        if not valid_chat_id(chat_id) or not valid_room_title(title):
            return http_error(400, "invalid room title")
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
            return http_error(400, "invalid title_revision")

        # Revision comparison and the durable save stay on one event-loop turn:
        # there is no await between them, so a delayed old mirror write cannot
        # win over a newer title or race the active session cache.
        result = self.sessions.set_session_title(
            webui_session_key(str(chat_id)),
            str(title).strip(),
            room_id=str(room_id),
            title_revision=revision,
        )
        if result == "missing":
            return http_error(404, "room not found")
        if result == "older":
            return http_error(409, "older room title revision")
        return http_json_response(
            {
                "room_id": room_id,
                "chat_id": chat_id,
                "title": str(title).strip(),
                "title_revision": revision,
            }
        )

    def issue_token(self, request: WsRequest) -> Response:
        guard = self._control_plane_guard(request)
        if guard is not None:
            return guard
        body = request_json(request)
        if body is None:
            return http_error(400, "invalid JSON body")
        room_id = body.get("room_id")
        chat_id = body.get("chat_id")
        participant_id = body.get("participant_id")
        display_name = str(body.get("display_name") or "").strip()
        role = str(body.get("role") or "contributor").strip().lower()

        if not valid_room_id(room_id):
            return http_error(400, "invalid room_id")
        if not valid_chat_id(chat_id):
            return http_error(400, "invalid chat_id")
        if not valid_participant_id(participant_id):
            return http_error(400, "invalid participant_id")
        # Only 'contributor' is mintable. 'owner' is implicit and never issued.
        if not valid_display_name(display_name) or role != "contributor":
            return http_error(400, "invalid participant")

        room_error = self._room_guard(room_id, chat_id)
        if room_error is not None:
            return room_error
        if self.store.at_capacity():
            return http_json_response({"error": "too many outstanding room tokens"}, status=429)

        token, _credential = self.store.mint(
            room_id=str(room_id),
            chat_id=str(chat_id),
            participant_id=str(participant_id),
            display_name=display_name,
            role=role,
        )
        mode = self.store.room_mode(str(chat_id))
        return http_json_response(
            {
                "token": token,
                "ws_path": getattr(self.config, "path", "/") or "/",
                "expires_in": getattr(self.config, "token_ttl_s", 300),
                "mode": mode,
                "capabilities": (
                    ["discussion", "ask_ziggy", "connected_read_proposals"]
                    if mode == ROOM_MODE_COLLABORATIVE
                    else []
                ),
            }
        )

    async def revoke_room(self, request: WsRequest) -> Response:
        guard = self._control_plane_guard(request)
        if guard is not None:
            return guard
        body = request_json(request)
        if body is None:
            return http_error(400, "invalid JSON body")
        room_id = body.get("room_id")
        chat_id = body.get("chat_id")
        if not valid_room_id(room_id):
            return http_error(400, "invalid room_id")
        if not valid_chat_id(chat_id):
            return http_error(400, "invalid chat_id")
        metadata = self.store.room_metadata(str(chat_id))
        if metadata.get("shared_room") is not True or metadata.get("room_id") != room_id:
            return http_error(404, "room not found")

        session = self.sessions.get_or_create(webui_session_key(str(chat_id)))
        session.metadata["shared_room_revoked"] = True
        self.sessions.save(session, fsync=True)

        invalidated, connections = self.store.revoke(
            room_id=str(room_id),
            chat_id=str(chat_id),
        )
        # Await the close rather than firing it off. Combined with the store
        # marking (not dropping) the credential, this leaves no window in which
        # a revoked socket is still open but no longer reads as a guest.
        for connection in connections:
            close = getattr(connection, "close", None)
            if close is not None:
                with suppress(Exception):
                    await close(code=1008, reason="shared room revoked")
            if self.channel is not None:
                with suppress(Exception):
                    await self.channel.forget_room_connection(connection)
        return http_json_response(
            {
                "room_id": room_id,
                "invalidated_tokens": invalidated,
                "closed_connections": len(connections),
            }
        )

    # -- owner review and publication --------------------------------------

    async def editorial(self, request: WsRequest, action: str) -> Response:
        guard = self._control_plane_guard(request)
        if guard is not None:
            return guard
        body = request_json(request)
        if body is None:
            return http_error(400, "Invalid JSON")

        if action == "preview":
            return self._preview(body)

        if not getattr(self.config, "shared_room_collaboration_enabled", False):
            return http_error(404, "Room collaboration is not enabled")
        if self.channel is None:
            return http_error(503, "Room collaboration is unavailable")
        room_id, chat_id = body.get("room_id"), body.get("chat_id")
        room_error = self._room_guard(room_id, chat_id)
        if room_error is not None:
            return room_error
        return await self.channel.handle_room_editorial(
            action,
            room_id=str(room_id),
            chat_id=str(chat_id),
            body=body,
        )

    def _preview(self, body: dict[str, Any]) -> Response:
        key = body.get("source_session_key")
        if not valid_session_key(key) or not is_webui_session_key(key):
            return http_error(400, "Invalid source")
        stored = self.sessions.read_session_file(str(key))
        if stored is None:
            return http_error(404, "Conversation not found")
        source = self.sessions.get_or_create(str(key))
        raw = source.messages
        public = self.sessions.shareable_messages(raw, "Owner")
        if len(json.dumps(public).encode()) > MAX_PREVIEW_BYTES:
            return http_error(
                413,
                "This conversation is too large to preview. Share a shorter conversation.",
            )
        return http_json_response(
            {
                "message_count": len(raw),
                "snapshot_sha256": hashlib.sha256(
                    json.dumps(raw, sort_keys=True, default=str).encode()
                ).hexdigest(),
                "messages": public,
                "collaborative": bool(
                    getattr(self.config, "shared_room_collaboration_enabled", False)
                ),
            }
        )

    # -- guest read ---------------------------------------------------------

    def room_session_messages(self, request: WsRequest, raw_key: str) -> Response | None:
        """Reinstated ``/api/sessions/<key>/messages`` for room credentials.

        Upstream removed this route (``cdb2a474``). For a room token it is
        restored with a *narrower* contract than it had: the token authorizes
        exactly its own session, and the body is the ``shareable_messages``
        projection rather than the raw session file.

        A request that carries no live room token falls through (``None``) to
        the owner route in ``ws_http`` (MIT-1404), which requires the owner API
        token and answers 401 otherwise, exactly as production does.
        """
        credential = self.store.api_credential(bearer_token(request.headers))
        if credential is None:
            return None
        if self.sessions is None:
            return http_error(503, "session manager unavailable")
        from urllib.parse import unquote

        key = unquote(raw_key)
        if not valid_session_key(key):
            return http_error(400, "invalid session key")
        if not self.store.authorizes_session(credential, key):
            # Same response for an absent, wrong-session, expired or revoked
            # credential: a prober learns nothing about which rooms exist.
            return http_error(404, "session not found")
        assert credential is not None
        data = self.sessions.read_session_file(key)
        if data is None:
            return http_error(404, "session not found")
        messages = data.get("messages")
        owner = str(
            (data.get("metadata") or {}).get("owner_display_name") or "Owner"
        )
        return http_json_response(
            {
                "key": key,
                "metadata": self._public_room_metadata(data.get("metadata")),
                "messages": self.sessions.shareable_messages(
                    messages if isinstance(messages, list) else [],
                    owner,
                ),
            }
        )

    async def room_published_file(
        self,
        request: WsRequest,
        raw_key: str,
        raw_file_id: str,
    ) -> Response | None:
        """Room-credential twin of the owner publication route (MIT-1407).

        ``ziggy-control`` forwards guest ``/api/sessions/<key>/files/<id>``
        requests to this origin (``shared_rooms.go`` allow-lists the exact
        shape), and production answers them from its single
        ``_handle_published_file`` by accepting a room credential beside the
        owner token (``feat/shared-rooms`` @ 83028651).  This fork splits the
        handlers, so the same rule lives here: the request must carry a live
        REST room credential whose own room session *is* ``key`` -- checked
        against ``credential.session_key``, never against the raw target, so
        percent-encoding cannot make a different session look authorized.
        A wrong, revoked or foreign credential, a non-canonical path and a
        missing grant all answer the same 404 as a missing publication: the
        route never discloses which room exists or which id was ever granted.

        A request with no room credential at all is not answered here -- it
        falls through to the owner route in ``ws_http``, exactly as the
        reinstated ``/messages`` read above does.
        """
        credential = self.store.api_credential(bearer_token(request.headers))
        if credential is None:
            return None
        if self.sessions is None:
            return http_error(503, "session manager unavailable")
        # Owner-route parity (``ws_http._handle_published_file``): the route
        # is exact, so the original request target -- not the decoded routing
        # path -- is what must agree with the re-canonicalised address; a
        # already-decoded value arriving back at the route would be a
        # re-encoded spelling of the same target and stays forbidden.
        request_target = getattr(request, "raw_path", None) or getattr(request, "path", "")
        if request_target != f"/api/sessions/{raw_key}/files/{raw_file_id}":
            return http_error(404, "Not Found")
        key = unquote(raw_key)
        file_id = unquote(raw_file_id)
        # One canonical spelling per target: re-encoding the decoded segment
        # must reproduce the raw target exactly, so an equivalently-decoding
        # but un-canonical key cannot reach a store lookup -- and a smuggled
        # encoded separator addresses a different session or file, which the
        # credential comparison below (and the store's own id pattern) refuses.
        if quote(key, safe="") != raw_key or quote(file_id, safe="") != raw_file_id:
            return http_error(404, "Not Found")
        if not self.store.authorizes_session(credential, key):
            return http_error(404, "Not Found")
        published = await asyncio.to_thread(
            self.sessions.read_published_file,
            key,
            file_id,
        )
        if published is None:
            return http_error(404, "Not Found")
        filename, payload = published
        # The response is the owner route's: only server-minted names reach a
        # client, the id and filename come from the validated grant rather than
        # the request, and ``read_published_file`` already refused an id whose
        # shape it did not mint -- so an id that reached here yet fails the
        # pattern is a corrupt store, answered by revealing nothing (the same
        # 404 as any other unresolvable id, never a 500).
        if _PUBLISHED_FILE_ID_RE.fullmatch(file_id) is None:
            return http_error(404, "Not Found")
        # Never reflect a control byte into a header, even if a host filesystem
        # permits one in a stored filename.
        clean_name = "".join(ch for ch in filename if ord(ch) >= 32 and ch != "\x7f")
        ascii_name = "".join(
            ch if ord(ch) < 127 and ch not in {'"', '\\'} else "_" for ch in clean_name
        )
        if not ascii_name or not ascii_name.endswith(".md"):
            ascii_name = "download.md"
        utf8_name = quote(clean_name or "download.md", safe="")
        return http_response(
            payload,
            content_type="text/markdown; charset=utf-8",
            extra_headers=[
                ("Cache-Control", "private, no-store"),
                ("X-Content-Type-Options", "nosniff"),
                (
                    "Content-Disposition",
                    f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{utf8_name}",
                ),
            ],
        )

    @staticmethod
    def _public_room_metadata(metadata: Any) -> dict[str, Any]:
        """Only the room facts a guest already knows; never private session state."""
        if not isinstance(metadata, dict):
            return {}
        return {
            key: metadata.get(key)
            for key in (
                "title",
                "room_id",
                "room_mode",
                "shared_room",
                "shared_room_title_revision",
                "owner_display_name",
            )
            if key in metadata
        }


__all__ = ["ROOM_EDITORIAL_ACTIONS", "SharedRoomRouter", "request_json"]
