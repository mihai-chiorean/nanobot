"""WebSocket server channel: nanobot acts as a WebSocket server and serves connected clients."""

from __future__ import annotations

import asyncio
import base64
import binascii
import email.utils
import hashlib
import hmac
import http
import json
import mimetypes
import os
import re
import secrets
import shutil
import ssl
import stat
import time
import uuid
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self
from urllib.parse import parse_qs, unquote, urlparse

from loguru import logger
from pydantic import Field, field_validator, model_validator
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request as WsRequest
from websockets.http11 import Response

from nanobot.agent.reasoning_policy import ReasoningProfile, parse_reasoning_profile
from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.channels.chat_inbox import ChatInboxStore
from nanobot.config.paths import get_media_dir, get_workspace_path
from nanobot.config.schema import Base
from nanobot.security.clerk import (
    ClerkAuthenticationError,
    ClerkAuthorizationError,
    ClerkTokenVerifier,
    ClerkUnavailableError,
)
from nanobot.utils.helpers import safe_filename
from nanobot.utils.media_decode import (
    FileSizeExceeded,
    save_base64_data_url,
)
from nanobot.work.store import ACTIVE_STATUSES, MAX_EVENT_PAGE, WorkStore

if TYPE_CHECKING:
    from nanobot.session.manager import SessionManager


def _strip_trailing_slash(path: str) -> str:
    if len(path) > 1 and path.endswith("/"):
        return path.rstrip("/")
    return path or "/"


def _normalize_config_path(path: str) -> str:
    return _strip_trailing_slash(path)


def _contains_symlink_component(path: Path) -> bool:
    """Return whether *path* contains a symlink component."""
    current = Path(path.anchor)
    for part in path.parts:
        if part == path.anchor:
            continue
        current /= part
        try:
            if current.is_symlink():
                return True
        except OSError:
            return True
    return False


def _append_buttons_as_text(text: str, buttons: list[list[str]]) -> str:
    labels = [label for row in buttons for label in row if label]
    if not labels:
        return text
    fallback = "\n".join(f"{index}. {label}" for index, label in enumerate(labels, 1))
    return f"{text}\n\n{fallback}" if text else fallback


class WebSocketConfig(Base):
    """WebSocket server channel configuration.

    Clients connect with URLs like ``ws://{host}:{port}{path}?client_id=...&token=...``.
    - ``client_id``: Used for ``allow_from`` authorization; if omitted, a value is generated and logged.
    - ``token``: If non-empty, the ``token`` query param may match this static secret; short-lived tokens
      from ``token_issue_path`` are also accepted.
    - ``token_issue_path``: If non-empty, **GET** (HTTP/1.1) to this path returns JSON
      ``{"token": "...", "expires_in": <seconds>}``; use ``?token=...`` when opening the WebSocket.
      Must differ from ``path`` (the WS upgrade path). If the client runs in the **same process** as
      nanobot and shares the asyncio loop, use a thread or async HTTP client for GET—do not call
      blocking ``urllib`` or synchronous ``httpx`` from inside a coroutine.
    - ``token_issue_secret``: If non-empty, token requests must send ``Authorization: Bearer <secret>`` or
      ``X-Nanobot-Auth: <secret>``.
    - ``websocket_requires_token``: If True, the handshake must include a valid token (static or issued and not expired).
    - Each connection has its own session: a unique ``chat_id`` maps to the agent session internally.
    - ``media`` field in outbound messages contains local filesystem paths; remote clients need a
      shared filesystem or an HTTP file server to access these files.
    """

    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 8765
    path: str = "/"
    token: str = ""
    token_issue_path: str = ""
    token_issue_secret: str = ""
    token_ttl_s: int = Field(default=300, ge=30, le=86_400)
    websocket_requires_token: bool = True
    auth_jwks_url: str = ""
    auth_issuer: str = ""
    auth_audience: str = ""
    auth_allowed_emails: list[str] = Field(default_factory=list)
    auth_authorized_parties: list[str] = Field(default_factory=list)
    allow_from: list[str] = Field(default_factory=lambda: ["*"])
    streaming: bool = True
    # Default 36 MB, upper 40 MB: supports up to 4 images at ~6 MB each after
    # client-side Worker normalization (see webui Composer). 4 × 6 MB × 1.37
    # (base64 overhead) + envelope framing stays under 36 MB; the 40 MB ceiling
    # leaves a small margin for sender slop without opening a DoS avenue.
    max_message_bytes: int = Field(default=37_748_736, ge=1024, le=41_943_040)
    # Set to null when a client-owned heartbeat is responsible for liveness.
    ping_interval_s: float | None = Field(default=20.0, ge=5.0, le=300.0)
    ping_timeout_s: float = Field(default=20.0, ge=5.0, le=300.0)
    ssl_certfile: str = ""
    ssl_keyfile: str = ""

    @field_validator("path")
    @classmethod
    def path_must_start_with_slash(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError('path must start with "/"')
        return _normalize_config_path(value)

    @field_validator("token_issue_path")
    @classmethod
    def token_issue_path_format(cls, value: str) -> str:
        value = value.strip()
        if not value:
            return ""
        if not value.startswith("/"):
            raise ValueError('token_issue_path must start with "/"')
        return _normalize_config_path(value)

    @model_validator(mode="after")
    def token_issue_path_differs_from_ws_path(self) -> Self:
        if not self.token_issue_path:
            return self
        if _normalize_config_path(self.token_issue_path) == _normalize_config_path(self.path):
            raise ValueError("token_issue_path must differ from path (the WebSocket upgrade path)")
        return self


def _http_json_response(data: dict[str, Any], *, status: int = 200) -> Response:
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    headers = Headers(
        [
            ("Date", email.utils.formatdate(usegmt=True)),
            ("Connection", "close"),
            ("Content-Length", str(len(body))),
            ("Content-Type", "application/json; charset=utf-8"),
        ]
    )
    reason = http.HTTPStatus(status).phrase
    return Response(status, reason, headers, body)


def _read_webui_model_name() -> str | None:
    """Return the configured default model for readonly webui display."""
    from nanobot.model_runtime import read_status

    active = read_status().get("active_model")
    if isinstance(active, str) and active.strip():
        return active.strip()
    try:
        from nanobot.config.loader import load_config

        model = load_config().agents.defaults.model.strip()
        return model or None
    except Exception as e:
        logger.debug("webui bootstrap could not load model name: {}", e)
        return None


def _parse_request_path(path_with_query: str) -> tuple[str, dict[str, list[str]]]:
    """Parse normalized path and query parameters in one pass."""
    parsed = urlparse("ws://x" + path_with_query)
    path = _strip_trailing_slash(parsed.path or "/")
    return path, parse_qs(parsed.query)


def _normalize_http_path(path_with_query: str) -> str:
    """Return the path component (no query string), with trailing slash normalized (root stays ``/``)."""
    return _parse_request_path(path_with_query)[0]


def _parse_query(path_with_query: str) -> dict[str, list[str]]:
    return _parse_request_path(path_with_query)[1]


def _query_first(query: dict[str, list[str]], key: str) -> str | None:
    """Return the first value for *key*, or None."""
    values = query.get(key)
    return values[0] if values else None


def _parse_inbound_payload(raw: str) -> str | None:
    """Parse a client frame into text; return None for empty or unrecognized content."""
    text = raw.strip()
    if not text:
        return None
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return text
        if isinstance(data, dict):
            for key in ("content", "text", "message"):
                value = data.get(key)
                if isinstance(value, str) and value.strip():
                    return value
            return None
        return None
    return text


# Accept UUIDs and short scoped keys like "unified:default". Keeps the capability
# namespace small enough to rule out path traversal / quote injection tricks.
_CHAT_ID_RE = re.compile(r"^[A-Za-z0-9_:-]{1,64}$")


def _is_valid_chat_id(value: Any) -> bool:
    return isinstance(value, str) and _CHAT_ID_RE.match(value) is not None


def _parse_envelope(raw: str) -> dict[str, Any] | None:
    """Return a typed envelope dict if the frame is a new-style JSON envelope, else None.

    A frame qualifies when it parses as a JSON object with a string ``type`` field.
    Legacy frames (plain text, or ``{"content": ...}`` without ``type``) return None;
    callers should fall back to :func:`_parse_inbound_payload` for those.
    """
    text = raw.strip()
    if not text.startswith("{"):
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    t = data.get("type")
    if not isinstance(t, str):
        return None
    return data


# Per-message media limits. The server-side guard is a touch looser than the
# client's ``Worker`` normalization target (6 MB) — tolerate client slop, but
# still cap total ingress at ``_MAX_IMAGES_PER_MESSAGE * _MAX_IMAGE_BYTES``
# which fits comfortably inside ``max_message_bytes``.
_MAX_IMAGES_PER_MESSAGE = 4
_MAX_IMAGE_BYTES = 8 * 1024 * 1024
_MAX_VIDEOS_PER_MESSAGE = 1
_MAX_VIDEO_BYTES = 20 * 1024 * 1024
_MAX_DOCUMENTS_PER_MESSAGE = 3
_MAX_DOCUMENT_BYTES = 10 * 1024 * 1024
_MAX_DOCUMENT_BYTES_PER_MESSAGE = 24 * 1024 * 1024
_MAX_OFFICE_UNCOMPRESSED_BYTES = 32 * 1024 * 1024
_MAX_OFFICE_ENTRIES = 10_000

# Image MIME whitelist — matches the Composer's ``accept`` list. SVG is
# explicitly excluded to avoid the XSS surface inside embedded scripts.
_IMAGE_MIME_ALLOWED: frozenset[str] = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/webp",
        "image/gif",
    }
)

_VIDEO_MIME_ALLOWED: frozenset[str] = frozenset(
    {
        "video/mp4",
        "video/webm",
        "video/quicktime",
    }
)

_DOCUMENT_MIME_EXTENSIONS: dict[str, frozenset[str]] = {
    "application/pdf": frozenset({".pdf"}),
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": frozenset(
        {".docx"}
    ),
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": frozenset(
        {".xlsx"}
    ),
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": frozenset(
        {".pptx"}
    ),
    "text/plain": frozenset({".txt", ".log", ".ini", ".cfg"}),
    "text/markdown": frozenset({".md"}),
    "text/csv": frozenset({".csv"}),
    "application/json": frozenset({".json"}),
    "application/xml": frozenset({".xml"}),
    "text/xml": frozenset({".xml"}),
    "text/html": frozenset({".html", ".htm"}),
    "application/yaml": frozenset({".yaml", ".yml"}),
    "text/yaml": frozenset({".yaml", ".yml"}),
    "application/toml": frozenset({".toml"}),
}
_DOCUMENT_MIME_ALLOWED: frozenset[str] = frozenset(_DOCUMENT_MIME_EXTENSIONS)
_UPLOAD_MIME_ALLOWED: frozenset[str] = (
    _IMAGE_MIME_ALLOWED | _VIDEO_MIME_ALLOWED | _DOCUMENT_MIME_ALLOWED
)

_DATA_URL_MIME_RE = re.compile(r"^data:([^;]+);base64,", re.DOTALL)


def _extract_data_url_mime(url: str) -> str | None:
    """Return the MIME type of a ``data:<mime>;base64,...`` URL, else ``None``."""
    if not isinstance(url, str):
        return None
    m = _DATA_URL_MIME_RE.match(url)
    if not m:
        return None
    return m.group(1).strip().lower() or None


def _document_filename(mime: str, name: Any) -> str | None:
    """Return a safe document name, rejecting MIME/extension mismatches."""
    extensions = _DOCUMENT_MIME_EXTENSIONS[mime]
    if name is None:
        return f"attachment{sorted(extensions)[0]}"
    if not isinstance(name, str) or not name.strip():
        return None
    basename = Path(name).name
    if Path(basename).suffix.lower() not in extensions:
        return None
    return basename


def _document_content_is_valid(path: Path, mime: str) -> bool:
    """Apply cheap container checks before a parser sees untrusted input."""
    if mime == "application/pdf":
        try:
            with path.open("rb") as handle:
                return handle.read(1_024).lstrip().startswith(b"%PDF-")
        except OSError:
            return False

    office_entry = {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
            "word/document.xml",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet":
            "xl/workbook.xml",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation":
            "ppt/presentation.xml",
    }.get(mime)
    if office_entry is not None:
        try:
            with zipfile.ZipFile(path) as archive:
                infos = archive.infolist()
                if len(infos) > _MAX_OFFICE_ENTRIES:
                    return False
                if any(info.flag_bits & 0x1 for info in infos):
                    return False
                if sum(info.file_size for info in infos) > _MAX_OFFICE_UNCOMPRESSED_BYTES:
                    return False
                names = {info.filename for info in infos}
                return "[Content_Types].xml" in names and office_entry in names
        except (OSError, zipfile.BadZipFile):
            return False

    try:
        with path.open("rb") as handle:
            return b"\x00" not in handle.read(8_192)
    except OSError:
        return False


def _attachment_rejection_message(reason: str) -> str:
    return {
        "malformed": "The attachment request was malformed.",
        "too_many_images": "You can attach up to 4 images to one message.",
        "too_many_videos": "You can attach one video to a message.",
        "too_many_documents": "You can attach up to 3 documents to one message.",
        "mime": "That attachment type is not supported.",
        "extension": "The document extension does not match its content type.",
        "size": "An attachment exceeds the per-file size limit.",
        "total_size": "Documents can total up to 24 MB per message.",
        "content": "The document is malformed or has an unsafe container.",
        "decode": "An attachment could not be decoded.",
    }.get(reason, "An attachment was rejected.")


_LOCALHOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

# Matches the legacy chat-id pattern but allows file-system-safe stems too,
# so the API can address sessions whose keys came from non-WebSocket channels.
_API_KEY_RE = re.compile(r"^[A-Za-z0-9_:.-]{1,128}$")
_WORK_ID_RE = re.compile(r"^work_[0-9a-f]{32}$")
_CLIENT_MESSAGE_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
_MAX_ACCEPTED_CLIENT_MESSAGES = 4_096
_COMMAND_ID_RE = re.compile(r"^cmd_[0-9a-f]{32}$")
_ARTIFACT_ID_RE = re.compile(r"^artifact_[0-9a-f]{32}$")
_REASONING_EFFORTS = frozenset(
    {"none", "minimal", "minimum", "low", "medium", "high", "max", "adaptive"}
)
_REASONING_PROFILES = frozenset(profile.value for profile in ReasoningProfile)
_ROOM_ID_RE = re.compile(r"^room_[0-9a-f]{32}$")
_PARTICIPANT_ID_RE = re.compile(r"^participant_[0-9a-f]{32}$")


@dataclass(frozen=True)
class _RoomCredential:
    expires_at: float
    room_id: str
    chat_id: str
    participant_id: str
    display_name: str
    role: str


def _decode_api_key(raw_key: str) -> str | None:
    """Decode a percent-encoded API path segment, then validate the result."""
    key = unquote(raw_key)
    if _API_KEY_RE.match(key) is None:
        return None
    return key


def _decode_id(raw_value: str, pattern: re.Pattern[str]) -> str | None:
    value = unquote(raw_value)
    return value if pattern.fullmatch(value) is not None else None


def _request_json(request: Any) -> dict[str, Any] | None:
    body = getattr(request, "body", b"")
    if not body:
        return {}
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _is_localhost(connection: Any) -> bool:
    """Return True if *connection* originated from the loopback interface."""
    addr = getattr(connection, "remote_address", None)
    if not addr:
        return False
    host = addr[0] if isinstance(addr, tuple) else addr
    if not isinstance(host, str):
        return False
    # ``::ffff:127.0.0.1`` is loopback in IPv6-mapped form.
    if host.startswith("::ffff:"):
        host = host[7:]
    return host in _LOCALHOSTS


def _http_response(
    body: bytes,
    *,
    status: int = 200,
    content_type: str = "text/plain; charset=utf-8",
    extra_headers: list[tuple[str, str]] | None = None,
) -> Response:
    headers = [
        ("Date", email.utils.formatdate(usegmt=True)),
        ("Connection", "close"),
        ("Content-Length", str(len(body))),
        ("Content-Type", content_type),
    ]
    if extra_headers:
        headers.extend(extra_headers)
    reason = http.HTTPStatus(status).phrase
    return Response(status, reason, Headers(headers), body)


def _http_error(status: int, message: str | None = None) -> Response:
    body = (message or http.HTTPStatus(status).phrase).encode("utf-8")
    return _http_response(body, status=status)


def _bearer_token(headers: Any) -> str | None:
    """Pull a Bearer token out of standard or query-style headers."""
    auth = headers.get("Authorization") or headers.get("authorization")
    if auth and auth.lower().startswith("bearer "):
        return auth[7:].strip() or None
    return None


def _is_websocket_upgrade(request: WsRequest) -> bool:
    """Detect an actual WS upgrade; plain HTTP GETs to the same path should fall through."""
    upgrade = request.headers.get("Upgrade") or request.headers.get("upgrade")
    connection = request.headers.get("Connection") or request.headers.get("connection")
    if not upgrade or "websocket" not in upgrade.lower():
        return False
    if not connection or "upgrade" not in connection.lower():
        return False
    return True


def _b64url_encode(data: bytes) -> str:
    """URL-safe base64 without padding — compact + friendly in URL paths."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    """Reverse of :func:`_b64url_encode`; caller handles ``ValueError``."""
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


# Allowed MIME types we actually serve from the media endpoint. Anything
# outside this set is degraded to ``application/octet-stream`` so an
# attacker who somehow gets a signed URL for an unexpected file type can't
# trick the browser into sniffing executable content.
_MEDIA_ALLOWED_MIMES: frozenset[str] = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/webp",
        "image/gif",
        "video/mp4",
        "video/webm",
        "video/quicktime",
    }
)


def _issue_route_secret_matches(headers: Any, configured_secret: str) -> bool:
    """Return True if the token-issue HTTP request carries credentials matching ``token_issue_secret``."""
    if not configured_secret:
        return True
    authorization = headers.get("Authorization") or headers.get("authorization")
    if authorization and authorization.lower().startswith("bearer "):
        supplied = authorization[7:].strip()
        return hmac.compare_digest(supplied, configured_secret)
    header_token = headers.get("X-Nanobot-Auth") or headers.get("x-nanobot-auth")
    if not header_token:
        return False
    return hmac.compare_digest(header_token.strip(), configured_secret)


class WebSocketChannel(BaseChannel):
    """Run a local WebSocket server; forward text/JSON messages to the message bus."""

    name = "websocket"
    display_name = "WebSocket"

    def __init__(
        self,
        config: Any,
        bus: MessageBus,
        *,
        session_manager: "SessionManager | None" = None,
        static_dist_path: Path | None = None,
        active_session_keys: Callable[[], set[str]] | None = None,
    ):
        if isinstance(config, dict):
            config = WebSocketConfig.model_validate(config)
        super().__init__(config, bus)
        self.config: WebSocketConfig = config
        # chat_id -> connections subscribed to it (fan-out target).
        self._subs: dict[str, set[Any]] = {}
        # connection -> chat_ids it is subscribed to (O(1) cleanup on disconnect).
        self._conn_chats: dict[Any, set[str]] = {}
        # connection -> default chat_id for legacy frames that omit routing.
        self._conn_default: dict[Any, str] = {}
        # task_id -> WebSocket subscribers for durable Work events.
        self._work_subs: dict[str, set[Any]] = {}
        self._conn_work: dict[Any, set[str]] = {}
        # Stable client IDs suppress transport retries while this runtime is
        # alive. Persisted session IDs provide the second dedupe boundary after
        # a restart.
        self._accepted_client_messages: dict[tuple[str, str], None] = {}
        # Single-use tokens consumed at WebSocket handshake.
        self._issued_tokens: dict[str, float] = {}
        # Multi-use tokens for the embedded webui's REST surface; checked but not consumed.
        self._api_tokens: dict[str, float] = {}
        # Room tokens are separate from tenant-wide tokens. The WebSocket copy
        # is consumed at handshake while the REST copy remains valid for the
        # exact room history until expiry.
        self._room_ws_tokens: dict[str, _RoomCredential] = {}
        self._room_api_tokens: dict[str, _RoomCredential] = {}
        self._conn_room: dict[Any, _RoomCredential] = {}
        self._clerk_verifier = ClerkTokenVerifier(
            issuer=config.auth_issuer,
            jwks_url=config.auth_jwks_url,
            audience=config.auth_audience,
            allowed_emails=config.auth_allowed_emails,
            authorized_parties=config.auth_authorized_parties,
            secret_key=os.environ.get("CLERK_SECRET_KEY", ""),
        )
        self._stop_event: asyncio.Event | None = None
        self._server_task: asyncio.Task[None] | None = None
        self._session_manager = session_manager
        self._active_session_keys = active_session_keys
        self._work_store = (
            WorkStore(session_manager.workspace) if session_manager is not None else None
        )
        self._chat_inbox = (
            ChatInboxStore(session_manager.workspace) if session_manager is not None else None
        )
        self._static_dist_path: Path | None = (
            static_dist_path.resolve() if static_dist_path is not None else None
        )
        # Process-local secret used to HMAC-sign media URLs. The signed URL is
        # the capability — anyone who holds a valid URL can fetch that one
        # file, nothing else. The secret regenerates on restart so links
        # become self-expiring (callers just refresh the session list).
        self._media_secret: bytes = secrets.token_bytes(32)

    # -- Subscription bookkeeping -------------------------------------------

    def _attach(self, connection: Any, chat_id: str) -> None:
        """Idempotently subscribe *connection* to *chat_id*."""
        self._subs.setdefault(chat_id, set()).add(connection)
        self._conn_chats.setdefault(connection, set()).add(chat_id)

    def _cleanup_connection(self, connection: Any) -> None:
        """Remove *connection* from every subscription set; safe to call multiple times."""
        chat_ids = self._conn_chats.pop(connection, set())
        for cid in chat_ids:
            subs = self._subs.get(cid)
            if subs is None:
                continue
            subs.discard(connection)
            if not subs:
                self._subs.pop(cid, None)
        self._conn_default.pop(connection, None)
        self._conn_room.pop(connection, None)
        task_ids = self._conn_work.pop(connection, set())
        for task_id in task_ids:
            subscribers = self._work_subs.get(task_id)
            if subscribers is None:
                continue
            subscribers.discard(connection)
            if not subscribers:
                self._work_subs.pop(task_id, None)

    async def _send_event(self, connection: Any, event: str, **fields: Any) -> None:
        """Send a control event (attached, error, ...) to a single connection."""
        payload: dict[str, Any] = {"event": event}
        payload.update(fields)
        raw = json.dumps(payload, ensure_ascii=False)
        try:
            await connection.send(raw)
        except ConnectionClosed:
            self._cleanup_connection(connection)
        except Exception as e:
            logger.warning("websocket: failed to send {} event: {}", event, e)

    async def _broadcast_event(self, chat_id: str, event: str, **fields: Any) -> None:
        """Fan a room event out to a stable snapshot of current subscribers."""
        subscribers = list(self._subs.get(chat_id, set()))
        if not subscribers:
            return
        await asyncio.gather(
            *(
                self._send_event(connection, event, chat_id=chat_id, **fields)
                for connection in subscribers
            )
        )

    async def _send_message_ack(
        self,
        connection: Any,
        *,
        chat_id: str,
        client_message_id: str,
        status: str,
        detail: str | None = None,
    ) -> None:
        fields: dict[str, Any] = {
            "chat_id": chat_id,
            "client_message_id": client_message_id,
            "status": status,
        }
        if detail:
            fields["detail"] = detail
        await self._send_event(connection, "message.ack", **fields)

    def _remember_client_message(self, chat_id: str, client_message_id: str) -> None:
        key = (chat_id, client_message_id)
        self._accepted_client_messages.pop(key, None)
        self._accepted_client_messages[key] = None
        while len(self._accepted_client_messages) > _MAX_ACCEPTED_CLIENT_MESSAGES:
            oldest = next(iter(self._accepted_client_messages))
            self._accepted_client_messages.pop(oldest, None)

    async def _recover_chat_inbox(self) -> None:
        if self._chat_inbox is None:
            return
        records = await self._chat_inbox.recoverable()
        recovered = 0
        for record in records:
            if record.state in {"stored", "retry_wait"}:
                claim = (
                    self._chat_inbox.claim_for_enqueue
                    if record.state == "stored"
                    else self._chat_inbox.claim_retry_for_enqueue
                )
                claimed = await claim(record.message.chat_id, record.client_message_id)
                if not claimed:
                    continue
            try:
                await self.bus.publish_inbound(record.message)
            except Exception:
                await self._chat_inbox.release_enqueue_claim(
                    record.message.chat_id,
                    record.client_message_id,
                )
                raise
            recovered += 1
        if recovered:
            logger.info("Recovered {} durable WebSocket message(s)", recovered)

    def _attach_work(self, connection: Any, task_id: str) -> None:
        self._work_subs.setdefault(task_id, set()).add(connection)
        self._conn_work.setdefault(connection, set()).add(task_id)

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        return WebSocketConfig().model_dump(by_alias=True)

    def _expected_path(self) -> str:
        return _normalize_config_path(self.config.path)

    def _build_ssl_context(self) -> ssl.SSLContext | None:
        cert = self.config.ssl_certfile.strip()
        key = self.config.ssl_keyfile.strip()
        if not cert and not key:
            return None
        if not cert or not key:
            raise ValueError(
                "websocket: ssl_certfile and ssl_keyfile must both be set for WSS, or both left empty"
            )
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(certfile=cert, keyfile=key)
        return ctx

    _MAX_ISSUED_TOKENS = 10_000

    def _purge_expired_issued_tokens(self) -> None:
        now = time.monotonic()
        for token_key, expiry in list(self._issued_tokens.items()):
            if now > expiry:
                self._issued_tokens.pop(token_key, None)
        for pool in (self._room_ws_tokens, self._room_api_tokens):
            for token_key, credential in list(pool.items()):
                if now > credential.expires_at:
                    pool.pop(token_key, None)

    def _take_issued_token_if_valid(self, token_value: str | None) -> bool:
        """Validate and consume one issued token (single use per connection attempt).

        Uses single-step pop to minimize the window between lookup and removal;
        safe under asyncio's single-threaded cooperative model.
        """
        if not token_value:
            return False
        self._purge_expired_issued_tokens()
        expiry = self._issued_tokens.pop(token_value, None)
        if expiry is None:
            return False
        if time.monotonic() > expiry:
            return False
        return True

    def _take_room_token_if_valid(
        self,
        connection: Any,
        token_value: str | None,
    ) -> bool:
        if not token_value:
            return False
        self._purge_expired_issued_tokens()
        credential = self._room_ws_tokens.pop(token_value, None)
        if credential is None or time.monotonic() > credential.expires_at:
            return False
        self._conn_room[connection] = credential
        return True

    def _room_api_credential(self, request: WsRequest) -> _RoomCredential | None:
        self._purge_expired_issued_tokens()
        token = _bearer_token(request.headers)
        if not token:
            return None
        credential = self._room_api_tokens.get(token)
        if credential is None or time.monotonic() > credential.expires_at:
            self._room_api_tokens.pop(token, None)
            return None
        return credential

    def _mint_room_transport_token(
        self,
        *,
        room_id: str,
        chat_id: str,
        participant_id: str,
        display_name: str,
        role: str,
    ) -> Response:
        self._purge_expired_issued_tokens()
        if (
            len(self._room_ws_tokens) >= self._MAX_ISSUED_TOKENS
            or len(self._room_api_tokens) >= self._MAX_ISSUED_TOKENS
        ):
            return _http_json_response(
                {"error": "too many outstanding room tokens"},
                status=429,
            )
        token = f"nbrt_{secrets.token_urlsafe(32)}"
        credential = _RoomCredential(
            expires_at=time.monotonic() + float(self.config.token_ttl_s),
            room_id=room_id,
            chat_id=chat_id,
            participant_id=participant_id,
            display_name=display_name,
            role=role,
        )
        self._room_ws_tokens[token] = credential
        self._room_api_tokens[token] = credential
        return _http_json_response(
            {
                "token": token,
                "ws_path": self._expected_path(),
                "expires_in": self.config.token_ttl_s,
                "model_name": _read_webui_model_name(),
            }
        )

    def _handle_token_issue_http(self, connection: Any, request: Any) -> Any:
        secret = self.config.token_issue_secret.strip()
        if secret:
            if not _issue_route_secret_matches(request.headers, secret):
                return connection.respond(401, "Unauthorized")
        else:
            logger.warning(
                "websocket: token_issue_path is set but token_issue_secret is empty; "
                "any client can obtain connection tokens — set token_issue_secret for production."
            )
        self._purge_expired_issued_tokens()
        self._purge_expired_api_tokens()
        if (
            len(self._issued_tokens) >= self._MAX_ISSUED_TOKENS
            or len(self._api_tokens) >= self._MAX_ISSUED_TOKENS
        ):
            logger.error(
                "websocket: too many outstanding tokens (issued={}, api={}), rejecting issuance",
                len(self._issued_tokens),
                len(self._api_tokens),
            )
            return _http_json_response({"error": "too many outstanding tokens"}, status=429)
        token_value = f"nbwt_{secrets.token_urlsafe(32)}"
        expiry = time.monotonic() + float(self.config.token_ttl_s)
        # Keep one token in each pool: WS consumes its copy, while REST keeps
        # accepting the same token until the shared TTL expires.
        self._issued_tokens[token_value] = expiry
        self._api_tokens[token_value] = expiry

        return _http_json_response(
            {
                "token": token_value,
                "expires_in": self.config.token_ttl_s,
                "ws_path": self._expected_path(),
            }
        )

    # -- HTTP dispatch ------------------------------------------------------

    async def _dispatch_http(self, connection: Any, request: WsRequest) -> Any:
        """Route an inbound HTTP request to a handler or to the WS upgrade path."""
        got, query = _parse_request_path(request.path)
        method = getattr(request, "method", "GET").upper()

        # 1. Token issue endpoint (legacy, optional, gated by configured secret).
        if self.config.token_issue_path:
            issue_expected = _normalize_config_path(self.config.token_issue_path)
            if got == issue_expected:
                if method != "GET":
                    return _http_error(405, "Method Not Allowed")
                return self._handle_token_issue_http(connection, request)

        if got == "/auth/bootstrap":
            if method != "GET":
                return _http_error(405, "Method Not Allowed")
            return await self._handle_auth_bootstrap(request)

        if got == "/auth/shared-rooms":
            if method != "POST":
                return _http_error(405, "Method Not Allowed")
            return await self._handle_shared_room_create(request)

        if got == "/auth/shared-room-token":
            if method != "POST":
                return _http_error(405, "Method Not Allowed")
            return self._handle_shared_room_token(request)

        if got == "/auth/shared-room-revoke":
            if method != "POST":
                return _http_error(405, "Method Not Allowed")
            return await self._handle_shared_room_revoke(request)

        # 2. WebUI bootstrap: localhost-only, mints tokens for the embedded UI.
        if got == "/webui/bootstrap":
            if method != "GET":
                return _http_error(405, "Method Not Allowed")
            return self._handle_webui_bootstrap(connection)

        # 3. REST surface for the embedded UI.
        if got == "/api/sessions":
            if method != "GET":
                return _http_error(405, "Method Not Allowed")
            return await self._handle_sessions_list(request)

        if got == "/api/activity":
            if method != "GET":
                return _http_error(405, "Method Not Allowed")
            return self._handle_activity(request)

        if got == "/api/work":
            if method == "GET":
                return await self._handle_work_list(request)
            if method == "POST":
                return await self._handle_work_create(request)
            return _http_error(405, "Method Not Allowed")

        match = re.match(r"^/api/work/artifacts/([^/]+)$", got)
        if match:
            if method != "GET":
                return _http_error(405, "Method Not Allowed")
            return await self._handle_work_artifact(request, match.group(1))

        match = re.match(r"^/api/work/([^/]+)/events$", got)
        if match:
            if method != "GET":
                return _http_error(405, "Method Not Allowed")
            return await self._handle_work_events(request, match.group(1))

        match = re.match(r"^/api/work/([^/]+)/cancel$", got)
        if match:
            if method != "POST":
                return _http_error(405, "Method Not Allowed")
            return await self._handle_work_cancel(request, match.group(1))

        match = re.match(r"^/api/work/([^/]+)/message$", got)
        if match:
            if method != "POST":
                return _http_error(405, "Method Not Allowed")
            return await self._handle_work_message(request, match.group(1))

        match = re.match(r"^/api/work/([^/]+)$", got)
        if match:
            if method != "GET":
                return _http_error(405, "Method Not Allowed")
            return await self._handle_work_detail(request, match.group(1))

        if got == "/api/model/status":
            if method != "GET":
                return _http_error(405, "Method Not Allowed")
            return self._handle_model_status(request)

        if got == "/api/model/switch":
            if method != "POST":
                return _http_error(405, "Method Not Allowed")
            return self._handle_model_switch(request)

        if got == "/api/settings":
            if method != "GET":
                return _http_error(405, "Method Not Allowed")
            return self._handle_settings(request)

        if got == "/api/settings/update":
            if method not in {"GET", "POST"}:
                return _http_error(405, "Method Not Allowed")
            return self._handle_settings_update(request)

        m = re.match(r"^/api/sessions/([^/]+)/messages$", got)
        if m:
            if method != "GET":
                return _http_error(405, "Method Not Allowed")
            return self._handle_session_messages(request, m.group(1))

        # NOTE: websockets' HTTP parser only accepts GET, so we cannot expose a
        # true ``DELETE`` verb. The action is folded into the path instead.
        m = re.match(r"^/api/sessions/([^/]+)/delete$", got)
        if m:
            if method not in {"GET", "POST"}:
                return _http_error(405, "Method Not Allowed")
            return self._handle_session_delete(request, m.group(1))

        # Signed media fetch: ``<sig>`` is an HMAC over ``<payload>``; the
        # payload decodes to a path inside :func:`get_media_dir`. See
        # :meth:`_sign_media_path` for the inverse direction used to build
        # these URLs when replaying a session.
        m = re.match(r"^/api/media/([A-Za-z0-9_-]+)/([A-Za-z0-9_-]+)$", got)
        if m:
            if method != "GET":
                return _http_error(405, "Method Not Allowed")
            return self._handle_media_fetch(m.group(1), m.group(2))

        # 4. WebSocket upgrade (the channel's primary purpose). Only run the
        # handshake gate on requests that actually ask to upgrade; otherwise
        # a bare ``GET /`` from the browser would be rejected as an
        # unauthorized WS handshake instead of serving the SPA's index.html.
        expected_ws = self._expected_path()
        if method == "GET" and got == expected_ws and _is_websocket_upgrade(request):
            client_id = _query_first(query, "client_id") or ""
            if len(client_id) > 128:
                client_id = client_id[:128]
            if not self.is_allowed(client_id):
                return connection.respond(403, "Forbidden")
            return self._authorize_websocket_handshake(connection, query)

        # 5. Static SPA serving (only if a build directory was wired in).
        if method == "GET" and self._static_dist_path is not None:
            response = self._serve_static(got)
            if response is not None:
                return response

        return connection.respond(404, "Not Found")

    # -- HTTP route handlers ------------------------------------------------

    def _check_api_token(self, request: WsRequest) -> bool:
        """Validate a request against the API token pool (multi-use, TTL-bound)."""
        self._purge_expired_api_tokens()
        token = _bearer_token(request.headers)
        if not token:
            return False
        expiry = self._api_tokens.get(token)
        if expiry is None or time.monotonic() > expiry:
            self._api_tokens.pop(token, None)
            return False
        return True

    def _purge_expired_api_tokens(self) -> None:
        now = time.monotonic()
        for token_key, expiry in list(self._api_tokens.items()):
            if now > expiry:
                self._api_tokens.pop(token_key, None)

    def _handle_webui_bootstrap(self, connection: Any) -> Response:
        if self._clerk_verifier.enabled:
            return _http_error(404, "Not Found")
        if not _is_localhost(connection):
            return _http_error(403, "webui bootstrap is localhost-only")
        return self._mint_transport_token()

    async def _handle_auth_bootstrap(self, request: WsRequest) -> Response:
        token = _bearer_token(request.headers)
        if not token:
            return _http_json_response({"error": "authentication required"}, status=401)
        try:
            await self._clerk_verifier.verify(token)
        except ClerkAuthenticationError:
            return _http_json_response({"error": "authentication required"}, status=401)
        except ClerkAuthorizationError:
            return _http_json_response({"error": "forbidden"}, status=403)
        except ClerkUnavailableError:
            logger.warning("websocket: Clerk bootstrap verification unavailable")
            return _http_json_response({"error": "identity verification unavailable"}, status=503)
        return self._mint_transport_token()

    async def _handle_shared_room_create(self, request: WsRequest) -> Response:
        if not _issue_route_secret_matches(
            request.headers,
            self.config.token_issue_secret.strip(),
        ):
            return _http_error(401, "Unauthorized")
        if not self.config.token_issue_secret.strip():
            return _http_error(503, "room issuance is unavailable")
        if self._session_manager is None:
            return _http_error(503, "session manager unavailable")
        body = _request_json(request)
        if body is None:
            return _http_error(400, "invalid JSON body")
        source_key = body.get("source_session_key")
        chat_id = body.get("chat_id")
        room_id = body.get("room_id")
        if (
            not isinstance(source_key, str)
            or _API_KEY_RE.fullmatch(source_key) is None
            or not source_key.startswith("websocket:")
        ):
            return _http_error(400, "invalid source_session_key")
        if not _is_valid_chat_id(chat_id):
            return _http_error(400, "invalid chat_id")
        if not isinstance(room_id, str) or _ROOM_ID_RE.fullmatch(room_id) is None:
            return _http_error(400, "invalid room_id")
        title = str(body.get("title") or "Shared conversation").strip()
        owner_display_name = str(body.get("owner_display_name") or "Owner").strip()
        if not 1 <= len(title) <= 120 or not 1 <= len(owner_display_name) <= 64:
            return _http_error(400, "invalid room metadata")
        destination_key = f"websocket:{chat_id}"
        try:
            await asyncio.to_thread(
                self._session_manager.clone_session,
                source_key,
                destination_key,
                metadata={
                    "shared_room": True,
                    "room_id": room_id,
                    "title": title,
                    "owner_display_name": owner_display_name,
                },
            )
        except FileNotFoundError:
            return _http_error(404, "source session not found")
        except FileExistsError:
            return _http_error(409, "room session already exists")
        except Exception:
            logger.exception("failed to create shared room session {}", room_id)
            return _http_error(500, "failed to create room")
        return _http_json_response(
            {
                "room_id": room_id,
                "session_key": destination_key,
                "chat_id": chat_id,
                "title": title,
            },
            status=201,
        )

    def _handle_shared_room_token(self, request: WsRequest) -> Response:
        if not _issue_route_secret_matches(
            request.headers,
            self.config.token_issue_secret.strip(),
        ):
            return _http_error(401, "Unauthorized")
        if not self.config.token_issue_secret.strip():
            return _http_error(503, "room issuance is unavailable")
        if self._session_manager is None:
            return _http_error(503, "session manager unavailable")
        body = _request_json(request)
        if body is None:
            return _http_error(400, "invalid JSON body")
        room_id = body.get("room_id")
        chat_id = body.get("chat_id")
        participant_id = body.get("participant_id")
        display_name = str(body.get("display_name") or "").strip()
        role = str(body.get("role") or "contributor").strip().lower()
        if not isinstance(room_id, str) or _ROOM_ID_RE.fullmatch(room_id) is None:
            return _http_error(400, "invalid room_id")
        if not _is_valid_chat_id(chat_id):
            return _http_error(400, "invalid chat_id")
        if (
            not isinstance(participant_id, str)
            or _PARTICIPANT_ID_RE.fullmatch(participant_id) is None
        ):
            return _http_error(400, "invalid participant_id")
        if not 1 <= len(display_name) <= 64 or role != "contributor":
            return _http_error(400, "invalid participant")
        payload = self._session_manager.read_session_file(f"websocket:{chat_id}")
        metadata = payload.get("metadata", {}) if isinstance(payload, dict) else {}
        if (
            not isinstance(metadata, dict)
            or metadata.get("shared_room") is not True
            or metadata.get("room_id") != room_id
        ):
            return _http_error(404, "room not found")
        return self._mint_room_transport_token(
            room_id=room_id,
            chat_id=chat_id,
            participant_id=participant_id,
            display_name=display_name,
            role=role,
        )

    async def _handle_shared_room_revoke(self, request: WsRequest) -> Response:
        if not _issue_route_secret_matches(
            request.headers,
            self.config.token_issue_secret.strip(),
        ):
            return _http_error(401, "Unauthorized")
        if not self.config.token_issue_secret.strip():
            return _http_error(503, "room revocation is unavailable")
        if self._session_manager is None:
            return _http_error(503, "session manager unavailable")
        body = _request_json(request)
        if body is None:
            return _http_error(400, "invalid JSON body")
        room_id = body.get("room_id")
        chat_id = body.get("chat_id")
        if not isinstance(room_id, str) or _ROOM_ID_RE.fullmatch(room_id) is None:
            return _http_error(400, "invalid room_id")
        if not _is_valid_chat_id(chat_id):
            return _http_error(400, "invalid chat_id")
        payload = self._session_manager.read_session_file(f"websocket:{chat_id}")
        metadata = payload.get("metadata", {}) if isinstance(payload, dict) else {}
        if (
            not isinstance(metadata, dict)
            or metadata.get("shared_room") is not True
            or metadata.get("room_id") != room_id
        ):
            return _http_error(404, "room not found")

        invalidated_tokens = 0
        for pool in (self._room_ws_tokens, self._room_api_tokens):
            for token, credential in list(pool.items()):
                if credential.room_id == room_id and credential.chat_id == chat_id:
                    pool.pop(token, None)
                    invalidated_tokens += 1

        connections = [
            connection
            for connection, credential in list(self._conn_room.items())
            if credential.room_id == room_id and credential.chat_id == chat_id
        ]
        for connection in connections:
            self._cleanup_connection(connection)
            asyncio.create_task(
                connection.close(code=1008, reason="shared room revoked")
            )
        return _http_json_response(
            {
                "room_id": room_id,
                "invalidated_tokens": invalidated_tokens,
                "closed_connections": len(connections),
            }
        )

    def _shared_room_owner_credential(
        self,
        chat_id: str,
    ) -> _RoomCredential | None:
        if self._session_manager is None:
            return None
        payload = self._session_manager.read_session_file(f"websocket:{chat_id}")
        metadata = payload.get("metadata", {}) if isinstance(payload, dict) else {}
        if not isinstance(metadata, dict) or metadata.get("shared_room") is not True:
            return None
        room_id = metadata.get("room_id")
        if not isinstance(room_id, str) or _ROOM_ID_RE.fullmatch(room_id) is None:
            return None
        display_name = str(metadata.get("owner_display_name") or "Owner").strip()
        return _RoomCredential(
            expires_at=float("inf"),
            room_id=room_id,
            chat_id=chat_id,
            participant_id="owner",
            display_name=display_name[:64] or "Owner",
            role="owner",
        )

    def _mint_transport_token(self) -> Response:
        # Cap outstanding tokens to avoid runaway growth from a misbehaving client.
        self._purge_expired_issued_tokens()
        self._purge_expired_api_tokens()
        if (
            len(self._issued_tokens) >= self._MAX_ISSUED_TOKENS
            or len(self._api_tokens) >= self._MAX_ISSUED_TOKENS
        ):
            return _http_response(
                json.dumps({"error": "too many outstanding tokens"}).encode("utf-8"),
                status=429,
                content_type="application/json; charset=utf-8",
            )
        token = f"nbwt_{secrets.token_urlsafe(32)}"
        expiry = time.monotonic() + float(self.config.token_ttl_s)
        # Same string registered in both pools: the WS handshake consumes one copy
        # while the REST surface keeps validating the other until TTL expiry.
        self._issued_tokens[token] = expiry
        self._api_tokens[token] = expiry
        return _http_json_response(
            {
                "token": token,
                "ws_path": self._expected_path(),
                "expires_in": self.config.token_ttl_s,
                "model_name": _read_webui_model_name(),
            }
        )

    async def _handle_sessions_list(self, request: WsRequest) -> Response:
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        if self._session_manager is None:
            return _http_error(503, "session manager unavailable")
        cleaned = await asyncio.to_thread(self._webui_session_summaries)
        return _http_json_response({"sessions": cleaned})

    def _webui_session_summaries(self) -> list[dict[str, Any]]:
        assert self._session_manager is not None
        sessions = self._session_manager.list_sessions()
        # The webui is only meaningful for websocket-channel chats — CLI /
        # Slack / Lark / Discord sessions can't be resumed from the browser,
        # so leaking them into the sidebar is just noise. Filter to the
        # ``websocket:`` prefix and strip absolute paths on the way out.
        cleaned: list[dict[str, Any]] = []
        for session in sessions:
            key = session.get("key")
            if not isinstance(key, str) or not key.startswith("websocket:"):
                continue
            summary = {k: v for k, v in session.items() if k != "path"}
            metadata = summary.get("metadata")
            if isinstance(metadata, dict):
                title = metadata.get("title")
                if isinstance(title, str) and title.strip():
                    summary["title"] = title.strip()
                if metadata.get("shared_room") is True:
                    summary["shared_room"] = True
            preview = self._session_manager.read_session_preview(key)
            if preview:
                summary["preview"] = preview
            cleaned.append(summary)
        return cleaned

    @staticmethod
    def _message_text(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return " ".join(
                item["text"]
                for item in content
                if isinstance(item, dict) and isinstance(item.get("text"), str)
            )
        return ""

    @classmethod
    def _session_preview(cls, messages: list[Any]) -> str:
        for message in messages:
            if not isinstance(message, dict) or message.get("role") != "user":
                continue
            text = " ".join(cls._message_text(message.get("content", "")).split())
            if text:
                return text[:160]
            media = message.get("media")
            if isinstance(media, list) and media:
                return "Media attachment"
        return ""

    @staticmethod
    def _activity_status(messages: list[Any], live: bool) -> str:
        if live:
            return "active"
        for message in reversed(messages):
            if not isinstance(message, dict):
                continue
            if message.get("role") == "assistant" and message.get("buttons"):
                return "waiting"
            if message.get("role") in {"user", "assistant"}:
                break
        return "idle"

    def _handle_activity(self, request: WsRequest) -> Response:
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        if self._session_manager is None:
            return _http_error(503, "session manager unavailable")
        active_keys = self._active_session_keys() if self._active_session_keys else set()
        items: list[dict[str, Any]] = []
        for session in self._session_manager.list_sessions():
            key = session.get("key")
            if not isinstance(key, str) or not self._is_webui_session_key(key):
                continue
            payload = self._session_manager.read_session_file(key)
            messages = payload.get("messages", []) if isinstance(payload, dict) else []
            if not isinstance(messages, list):
                messages = []
            chat_id = key.split(":", 1)[1]
            live = key in active_keys
            last = next(
                (message for message in reversed(messages) if isinstance(message, dict)),
                {},
            )
            last_role = last.get("role")
            last_text = self._message_text(last.get("content", ""))
            items.append(
                {
                    "key": key,
                    "chat_id": chat_id,
                    "created_at": session.get("created_at"),
                    "updated_at": session.get("updated_at"),
                    "preview": self._session_preview(messages),
                    "status": self._activity_status(messages, live),
                    "live": live,
                    "message_count": len(messages),
                    "last_role": last_role if isinstance(last_role, str) else None,
                    "last_text": " ".join(last_text.split())[:240],
                }
            )
        return _http_json_response({"activity": items})

    async def _handle_work_list(self, request: WsRequest) -> Response:
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        if self._work_store is None:
            return _http_error(503, "work store unavailable")
        query = _parse_query(request.path)
        status = _query_first(query, "status")
        try:
            limit = int(_query_first(query, "limit") or "50")
        except ValueError:
            limit = 50
        limit = max(1, min(limit, 200))
        try:
            offset = max(0, int(_query_first(query, "offset") or "0"))
        except ValueError:
            return _http_error(400, "offset must be an integer")
        order_by_task_id = _query_first(query, "order") == "task_id"
        after_task_id = _query_first(query, "after_task_id")
        if after_task_id is not None and _WORK_ID_RE.fullmatch(after_task_id) is None:
            return _http_error(400, "after_task_id must be a Work task id")
        tasks = await self._work_store.run_io(
            self._work_store.list_tasks,
            status=status,
            limit=limit,
            offset=offset,
            after_task_id=after_task_id,
            order_by_task_id=order_by_task_id,
        )
        return _http_json_response(
            {
                "tasks": tasks,
                "has_more": len(tasks) == limit,
                "next_offset": offset + len(tasks),
                "next_task_id": tasks[-1]["task_id"] if tasks else after_task_id,
            }
        )

    async def _handle_work_create(self, request: WsRequest) -> Response:
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        if self._work_store is None:
            return _http_error(503, "work store unavailable")
        body = _request_json(request)
        if body is None:
            return _http_error(400, "invalid JSON body")
        chat_id = body.get("chat_id")
        content = body.get("content")
        if not _is_valid_chat_id(chat_id):
            return _http_error(400, "invalid chat_id")
        if not isinstance(content, str) or not content.strip():
            return _http_error(400, "content is required")
        reasoning_profile = parse_reasoning_profile(
            body.get("reasoning_profile", ReasoningProfile.AUTO.value)
        )
        if reasoning_profile is None:
            return _http_error(400, "invalid reasoning_profile")
        media_paths, media_error = self._work_media(body.get("media"))
        if media_error is not None:
            return _http_error(400, f"media rejected: {media_error}")
        task = await self._work_store.run_io(
            self._work_store.create_task,
            chat_id=chat_id,
            content=content,
            mode="background",
            title=body.get("title") if isinstance(body.get("title"), str) else None,
            model=_read_webui_model_name() or "",
            reasoning_profile=reasoning_profile.value,
        )
        task_id = str(task["task_id"])
        try:
            await self._publish_work_inbound(
                task,
                sender_id="rest",
                content=content,
                media=media_paths,
            )
        except Exception:
            logger.exception("failed to enqueue REST Work task {}", task_id)
            await self._fail_work_enqueue(task_id)
            return _http_error(503, "failed to enqueue work")
        return _http_json_response({"task": task}, status=201)

    async def _handle_work_detail(self, request: WsRequest, raw_task_id: str) -> Response:
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        if self._work_store is None:
            return _http_error(503, "work store unavailable")
        task_id = _decode_id(raw_task_id, _WORK_ID_RE)
        if task_id is None:
            return _http_error(400, "invalid task id")
        task = await self._work_store.run_io(self._work_store.task_snapshot, task_id)
        if task is None:
            return _http_error(404, "task not found")
        self._augment_work_artifact_urls(task)
        return _http_json_response({"task": task})

    async def _handle_work_events(self, request: WsRequest, raw_task_id: str) -> Response:
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        if self._work_store is None:
            return _http_error(503, "work store unavailable")
        task_id = _decode_id(raw_task_id, _WORK_ID_RE)
        if task_id is None:
            return _http_error(400, "invalid task id")
        if await self._work_store.run_io(self._work_store.get_task, task_id) is None:
            return _http_error(404, "task not found")
        query = _parse_query(request.path)
        after_raw = _query_first(query, "after") or _query_first(query, "after_seq") or "0"
        try:
            after_seq = max(0, int(after_raw))
        except ValueError:
            return _http_error(400, "after must be an integer")
        try:
            limit = max(
                1,
                min(MAX_EVENT_PAGE, int(_query_first(query, "limit") or MAX_EVENT_PAGE)),
            )
        except ValueError:
            return _http_error(400, "limit must be an integer")
        events = await self._work_store.run_io(
            self._work_store.list_events,
            task_id,
            after_seq=after_seq,
            limit=limit,
        )
        next_after_seq = events[-1]["seq"] if events else after_seq
        return _http_json_response(
            {
                "events": events,
                "has_more": len(events) == limit,
                "next_after_seq": next_after_seq,
            }
        )

    async def _handle_work_cancel(self, request: WsRequest, raw_task_id: str) -> Response:
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        if self._work_store is None:
            return _http_error(503, "work store unavailable")
        task_id = _decode_id(raw_task_id, _WORK_ID_RE)
        if task_id is None:
            return _http_error(400, "invalid task id")
        task = await self._work_store.run_io(self._work_store.get_task, task_id)
        if task is None:
            return _http_error(404, "task not found")
        error = await self._cancel_work_task(task, sender_id="rest")
        if error == "terminal":
            return _http_error(409, "task is already complete")
        if error is not None:
            return _http_error(503, "failed to signal cancellation")
        current = await self._work_store.run_io(self._work_store.get_task, task_id)
        return _http_json_response({"task": current})

    async def _handle_work_message(self, request: WsRequest, raw_task_id: str) -> Response:
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        if self._work_store is None:
            return _http_error(503, "work store unavailable")
        task_id = _decode_id(raw_task_id, _WORK_ID_RE)
        if task_id is None:
            return _http_error(400, "invalid task id")
        task = await self._work_store.run_io(self._work_store.get_task, task_id)
        if task is None:
            return _http_error(404, "task not found")
        if task.get("status") not in ACTIVE_STATUSES:
            return _http_error(409, "task does not accept messages")
        body = _request_json(request)
        content = body.get("content") if body is not None else None
        if not isinstance(content, str) or not content.strip():
            return _http_error(400, "content is required")
        chat_id = str(task.get("chat_id") or "")
        if not _is_valid_chat_id(chat_id):
            return _http_error(500, "task chat is invalid")
        try:
            await self._publish_work_inbound(
                task,
                sender_id="rest",
                content=content,
            )
        except Exception:
            logger.exception("failed to enqueue message for Work task {}", task_id)
            return _http_error(503, "failed to enqueue work message")
        await self._record_work_message(task_id, content)
        return _http_json_response({"accepted": True, "task_id": task_id}, status=202)

    async def _publish_work_inbound(
        self,
        task: dict[str, Any],
        *,
        sender_id: str,
        content: str,
        media: list[str] | None = None,
        remote: Any = None,
    ) -> None:
        task_id = str(task["task_id"])
        session_key = str(task.get("session_key") or "")
        chat_id = str(task.get("chat_id") or "")
        if not session_key or not _is_valid_chat_id(chat_id):
            raise ValueError("Work task routing is invalid")
        metadata: dict[str, Any] = {
            "_wants_stream": True,
            "work_task_id": task_id,
            "work_mode": "background",
            "reasoning_profile": str(
                task.get("reasoning_profile") or ReasoningProfile.AUTO.value
            ),
        }
        if remote is not None:
            metadata["remote"] = remote
        await self.bus.publish_inbound(
            InboundMessage(
                channel=self.name,
                sender_id=sender_id,
                chat_id=chat_id,
                content=content,
                media=media or [],
                metadata=metadata,
                session_key_override=session_key,
            )
        )

    async def _fail_work_enqueue(self, task_id: str) -> None:
        assert self._work_store is not None
        event = await self._work_store.run_io(
            self._work_store.update_status,
            task_id,
            "failed",
            error="Failed to enqueue Work task.",
        )
        if event is not None:
            await self._broadcast_work_event(event)

    async def _record_work_message(self, task_id: str, content: str) -> None:
        assert self._work_store is not None
        event = await self._work_store.run_io(
            self._work_store.append_event,
            task_id,
            "message.received",
            {"content": content},
            actor="user",
        )
        if event is not None:
            await self._broadcast_work_event(event)

    async def _cancel_work_task(self, task: dict[str, Any], *, sender_id: str) -> str | None:
        assert self._work_store is not None
        if task.get("status") not in ACTIVE_STATUSES:
            return "terminal"
        try:
            await self._publish_work_inbound(
                task,
                sender_id=sender_id,
                content="/stop",
            )
        except Exception:
            logger.exception("failed to signal cancellation for Work task {}", task["task_id"])
            return "publish_failed"
        task_id = str(task["task_id"])
        event = await self._work_store.run_io(self._work_store.update_status, task_id, "cancelled")
        if event is None:
            current = await self._work_store.run_io(self._work_store.get_task, task_id)
            if current is not None and current.get("status") == "cancelled":
                return None
            return "terminal"
        await self._broadcast_work_event(event)
        return None

    async def _handle_work_artifact(self, request: WsRequest, raw_artifact_id: str) -> Any:
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        if self._work_store is None:
            return _http_error(503, "work store unavailable")
        artifact_id = _decode_id(raw_artifact_id, _ARTIFACT_ID_RE)
        if artifact_id is None:
            return _http_error(400, "invalid artifact id")
        item = await self._work_store.run_io(self._work_store.artifact_path, artifact_id)
        if item is None:
            return _http_error(404, "artifact not found")
        path, metadata = item
        mime = metadata.get("mime")
        if not isinstance(mime, str) or not mime:
            mime = "application/octet-stream"
        from nanobot.channels.websocket_server import TransportFileResponse

        return TransportFileResponse(
            path=path,
            content_type=mime,
            headers={
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
                "Content-Disposition": (
                    f'attachment; filename="{metadata.get("name", "artifact")}"'
                ),
            },
        )

    @staticmethod
    def _work_media(raw_media: Any) -> tuple[list[str], str | None]:
        if raw_media is None:
            return [], None
        if not isinstance(raw_media, list):
            return [], "malformed"
        return WebSocketChannel._save_envelope_media(raw_media)

    def _settings_payload(self, *, requires_restart: bool = False) -> dict[str, Any]:
        from nanobot.config.loader import get_config_path, load_config
        from nanobot.providers.registry import PROVIDERS, find_by_name

        config = load_config()
        defaults = config.agents.defaults
        provider_name = config.get_provider_name(defaults.model) or defaults.provider
        provider = config.get_provider(defaults.model)
        selected_provider = provider_name
        if defaults.provider != "auto":
            spec = find_by_name(defaults.provider)
            selected_provider = spec.name if spec else provider_name
        return {
            "agent": {
                "model": defaults.model,
                "provider": selected_provider,
                "resolved_provider": provider_name,
                "has_api_key": bool(provider and provider.api_key),
            },
            "providers": [{"name": "auto", "label": "Auto"}]
            + [{"name": spec.name, "label": spec.label} for spec in PROVIDERS],
            "runtime": {
                "config_path": str(get_config_path().expanduser()),
            },
            "model_runtime": self._model_status_payload(),
            "requires_restart": requires_restart,
        }

    def _handle_settings(self, request: WsRequest) -> Response:
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        return _http_json_response(self._settings_payload())

    def _handle_settings_update(self, request: WsRequest) -> Response:
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        from nanobot.config.loader import load_config, save_config
        from nanobot.providers.registry import find_by_name

        query = _parse_query(request.path)
        config = load_config()
        defaults = config.agents.defaults
        changed = False

        model = _query_first(query, "model")
        if model is not None:
            model = model.strip()
            if not model:
                return _http_error(400, "model is required")
            if defaults.model != model:
                defaults.model = model
                changed = True

        provider = _query_first(query, "provider")
        if provider is not None:
            provider = provider.strip() or "auto"
            if provider != "auto" and find_by_name(provider) is None:
                return _http_error(400, "unknown provider")
            if defaults.provider != provider:
                defaults.provider = provider
                changed = True

        if changed:
            save_config(config)
        return _http_json_response(self._settings_payload(requires_restart=changed))

    @staticmethod
    def _model_status_payload() -> dict[str, Any]:
        from nanobot.model_runtime import read_status

        return read_status()

    def _handle_model_status(self, request: WsRequest) -> Response:
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        return _http_json_response({"model_runtime": self._model_status_payload()})

    def _handle_model_switch(self, request: WsRequest) -> Response:
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        from nanobot.model_runtime import (
            ModelSwitchInProgressError,
            ModelSwitchUnavailableError,
            request_switch,
        )

        query = _parse_query(request.path)
        body = _request_json(request)
        if body is None:
            return _http_error(400, "invalid JSON body")
        raw_target = body.get("target") or _query_first(query, "target") or ""
        target = str(raw_target).strip().lower()
        if target not in {"qwen", "minimax"}:
            return _http_error(400, "target must be qwen or minimax")
        raw_force = body.get("force", _query_first(query, "force"))
        force = raw_force is True or str(raw_force or "").strip().lower() in {
            "1",
            "true",
            "yes",
        }
        try:
            status = request_switch(target, force=force)
        except ModelSwitchInProgressError as exc:
            return _http_error(409, str(exc))
        except ModelSwitchUnavailableError as exc:
            return _http_error(503, str(exc))
        except Exception:
            logger.exception("failed to start model switch")
            return _http_error(500, "failed to start model switch")
        return _http_json_response({"model_runtime": status}, status=202)

    @staticmethod
    def _is_webui_session_key(key: str) -> bool:
        """Return True when *key* belongs to the webui's websocket-only surface."""
        return key.startswith("websocket:")

    def _handle_session_messages(self, request: WsRequest, key: str) -> Response:
        if self._session_manager is None:
            return _http_error(503, "session manager unavailable")
        decoded_key = _decode_api_key(key)
        if decoded_key is None:
            return _http_error(400, "invalid session key")
        room_credential = self._room_api_credential(request)
        tenant_authorized = self._check_api_token(request)
        if not tenant_authorized and (
            room_credential is None
            or decoded_key != f"websocket:{room_credential.chat_id}"
        ):
            return _http_error(401, "Unauthorized")
        # The embedded webui only understands websocket-channel sessions. Keep
        # its read surface aligned with ``/api/sessions`` instead of letting a
        # caller probe arbitrary CLI / Slack / Lark history by handcrafted URL.
        if not self._is_webui_session_key(decoded_key):
            return _http_error(404, "session not found")
        data = self._session_manager.read_session_file(decoded_key)
        if data is None:
            return _http_error(404, "session not found")
        # Decorate persisted user messages with signed media URLs so the
        # client can render previews. The raw on-disk ``media`` paths are
        # stripped on the way out — they leak server filesystem layout and
        # the client never needs them once it has the signed fetch URL.
        self._augment_media_urls(data)
        return _http_json_response(data)

    def _augment_media_urls(self, payload: dict[str, Any]) -> None:
        """Mutate *payload* in place: each message's ``media`` path list is
        replaced by a parallel ``media_urls`` list of signed fetch URLs.

        Messages without media or with non-string path entries are left
        untouched. Paths that no longer live inside ``media_dir`` (e.g. the
        file was deleted, or the dir was relocated) are silently skipped;
        the client falls back to the historical-replay placeholder tile.
        """
        messages = payload.get("messages")
        if not isinstance(messages, list):
            return
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            media = msg.get("media")
            urls: list[dict[str, str]] = []
            if isinstance(media, list):
                for entry in media:
                    if not isinstance(entry, str) or not entry:
                        continue
                    signed = self._sign_media_path(Path(entry))
                    if signed is None:
                        continue
                    urls.append({"url": signed, "name": Path(entry).name})
                # Always drop raw filesystem paths from the wire payload.
                msg.pop("media", None)
            urls.extend(self._work_artifact_media_urls(msg.get("work_artifacts")))
            if urls:
                msg["media_urls"] = [*(msg.get("media_urls") or []), *urls]

    @staticmethod
    def _work_artifact_url(artifact_id: str) -> str:
        return f"/api/work/artifacts/{artifact_id}"

    def _work_artifact_media_urls(self, artifacts: Any) -> list[dict[str, str]]:
        if not isinstance(artifacts, list):
            return []
        urls: list[dict[str, str]] = []
        for artifact in artifacts:
            if not isinstance(artifact, dict):
                continue
            artifact_id = artifact.get("artifact_id")
            if not isinstance(artifact_id, str) or _ARTIFACT_ID_RE.fullmatch(artifact_id) is None:
                continue
            item = {"url": self._work_artifact_url(artifact_id)}
            name = artifact.get("name")
            if isinstance(name, str) and name:
                item["name"] = name
            urls.append(item)
        return urls

    def _augment_work_artifact_urls(self, task: dict[str, Any]) -> None:
        artifacts = task.get("artifacts")
        if not isinstance(artifacts, list):
            return
        for artifact in artifacts:
            if not isinstance(artifact, dict):
                continue
            artifact_id = artifact.get("artifact_id")
            if isinstance(artifact_id, str) and _ARTIFACT_ID_RE.fullmatch(artifact_id):
                artifact["url"] = self._work_artifact_url(artifact_id)

    def _sign_media_path(self, abs_path: Path) -> str | None:
        """Return a ``/api/media/<sig>/<payload>`` URL for *abs_path*, or
        ``None`` when the path does not resolve inside the media root.

        The URL is self-authenticating: the signature binds the payload to
        this process's ``_media_secret``, so only paths we chose to sign can
        be fetched. The returned path is relative to the server origin; the
        client joins it against the existing webui base.
        """
        try:
            if _contains_symlink_component(abs_path):
                return None
            media_root = get_media_dir().resolve()
            rel = abs_path.resolve().relative_to(media_root)
        except (OSError, ValueError):
            return None
        payload = _b64url_encode(rel.as_posix().encode("utf-8"))
        mac = hmac.new(self._media_secret, payload.encode("ascii"), hashlib.sha256).digest()[:16]
        return f"/api/media/{_b64url_encode(mac)}/{payload}"

    def _allowed_outbound_media(self, path: Path) -> Path | None:
        """Resolve an outbound source under this tenant's allowed roots."""
        if _contains_symlink_component(path):
            return None
        workspace = (
            self._session_manager.workspace
            if self._session_manager is not None
            else get_workspace_path()
        )
        try:
            resolved = path.resolve(strict=True)
            roots = (Path(workspace).resolve(strict=False), get_media_dir().resolve(strict=False))
            if not any(resolved == root or root in resolved.parents for root in roots):
                return None
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(resolved, flags)
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    return None
            finally:
                os.close(fd)
        except (OSError, ValueError):
            return None
        return resolved

    def _sign_or_stage_media_path(self, path: Path) -> dict[str, str] | None:
        """Return a signed media URL payload for *path*.

        Persisted inbound media already lives under ``get_media_dir`` and can
        be signed directly. Workspace files are copied into the websocket
        media bucket first so the browser can fetch them through the existing
        signed media route without exposing arbitrary filesystem paths.
        """
        resolved = self._allowed_outbound_media(path)
        if resolved is None:
            return None
        signed = self._sign_media_path(resolved)
        if signed is not None:
            return {"url": signed, "name": resolved.name}
        try:
            media_dir = get_media_dir("websocket")
            safe_name = safe_filename(resolved.name) or "attachment"
            staged = media_dir / f"{uuid.uuid4().hex[:12]}-{safe_name}"
            source_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            source_fd = os.open(resolved, source_flags)
            try:
                staged_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
                staged_fd = os.open(staged, staged_flags, 0o600)
                try:
                    with (
                        os.fdopen(source_fd, "rb", closefd=False) as source,
                        os.fdopen(staged_fd, "wb", closefd=False) as destination,
                    ):
                        shutil.copyfileobj(source, destination)
                finally:
                    os.close(staged_fd)
            finally:
                os.close(source_fd)
        except OSError as exc:
            logger.warning("websocket: failed to stage outbound media {}: {}", resolved, exc)
            return None
        signed = self._sign_media_path(staged)
        if signed is None:
            return None
        return {"url": signed, "name": resolved.name}

    def _handle_media_fetch(self, sig: str, payload: str) -> Response:
        """Serve a single media file previously signed via
        :meth:`_sign_media_path`. Validates the signature, decodes the
        payload to a relative path, and streams the file bytes with a
        long-lived immutable cache header (the URL already encodes the
        file identity, so caches can be aggressive)."""
        try:
            provided_mac = _b64url_decode(sig)
        except (ValueError, binascii.Error):
            return _http_error(401, "invalid signature")
        expected_mac = hmac.new(
            self._media_secret, payload.encode("ascii"), hashlib.sha256
        ).digest()[:16]
        if not hmac.compare_digest(expected_mac, provided_mac):
            return _http_error(401, "invalid signature")
        try:
            rel_bytes = _b64url_decode(payload)
            rel_str = rel_bytes.decode("utf-8")
        except (ValueError, binascii.Error, UnicodeDecodeError):
            return _http_error(400, "invalid payload")
        # An attacker who somehow bypassed the HMAC check would still need
        # the resolved path to escape the media root; guard defensively.
        try:
            media_root = get_media_dir().resolve()
            if _contains_symlink_component(media_root / rel_str):
                return _http_error(404, "not found")
            candidate = (media_root / rel_str).resolve()
            candidate.relative_to(media_root)
        except (OSError, ValueError):
            return _http_error(404, "not found")
        if not candidate.is_file():
            return _http_error(404, "not found")
        try:
            body = candidate.read_bytes()
        except OSError:
            return _http_error(500, "read error")
        mime, _ = mimetypes.guess_type(candidate.name)
        if mime not in _MEDIA_ALLOWED_MIMES:
            mime = "application/octet-stream"
        return _http_response(
            body,
            content_type=mime,
            extra_headers=[
                ("Cache-Control", "private, max-age=31536000, immutable"),
                # Paired with the MIME whitelist above: prevents browsers from
                # MIME-sniffing an octet-stream fallback into executable HTML.
                ("X-Content-Type-Options", "nosniff"),
            ],
        )

    def _handle_session_delete(self, request: WsRequest, key: str) -> Response:
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        if self._session_manager is None:
            return _http_error(503, "session manager unavailable")
        decoded_key = _decode_api_key(key)
        if decoded_key is None:
            return _http_error(400, "invalid session key")
        # Same boundary as ``_handle_session_messages``: the webui may only
        # mutate websocket sessions, and deletion really does unlink the local
        # JSONL, so keep the blast radius narrow and explicit.
        if not self._is_webui_session_key(decoded_key):
            return _http_error(404, "session not found")
        deleted = self._session_manager.delete_session(decoded_key)
        return _http_json_response({"deleted": bool(deleted)})

    def _serve_static(self, request_path: str) -> Response | None:
        """Resolve *request_path* against the built SPA directory; SPA fallback to index.html."""
        assert self._static_dist_path is not None
        rel = request_path.lstrip("/")
        if not rel:
            rel = "index.html"
        # Reject path-traversal attempts and absolute targets.
        if ".." in rel.split("/") or rel.startswith("/"):
            return _http_error(403, "Forbidden")
        candidate = (self._static_dist_path / rel).resolve()
        try:
            candidate.relative_to(self._static_dist_path)
        except ValueError:
            return _http_error(403, "Forbidden")
        if not candidate.is_file():
            # SPA history-mode fallback: unknown routes serve index.html so the
            # client-side router can render them.
            index = self._static_dist_path / "index.html"
            if index.is_file():
                candidate = index
            else:
                return None
        try:
            body = candidate.read_bytes()
        except OSError as e:
            logger.warning("websocket static: failed to read {}: {}", candidate, e)
            return _http_error(500, "Internal Server Error")
        ctype, _ = mimetypes.guess_type(candidate.name)
        if ctype is None:
            ctype = "application/octet-stream"
        if ctype.startswith("text/") or ctype in {"application/javascript", "application/json"}:
            ctype = f"{ctype}; charset=utf-8"
        # Hash-named build assets are cache-friendly; index.html must stay fresh.
        if candidate.name == "index.html":
            cache = "no-cache"
        else:
            cache = "public, max-age=31536000, immutable"
        return _http_response(
            body,
            status=200,
            content_type=ctype,
            extra_headers=[("Cache-Control", cache)],
        )

    def _authorize_websocket_handshake(self, connection: Any, query: dict[str, list[str]]) -> Any:
        supplied = _query_first(query, "token")
        static_token = self.config.token.strip()

        if static_token:
            if supplied and hmac.compare_digest(supplied, static_token):
                return None
            if supplied and self._take_room_token_if_valid(connection, supplied):
                return None
            if supplied and self._take_issued_token_if_valid(supplied):
                return None
            return connection.respond(401, "Unauthorized")

        if self.config.websocket_requires_token:
            if supplied and self._take_room_token_if_valid(connection, supplied):
                return None
            if supplied and self._take_issued_token_if_valid(supplied):
                return None
            return connection.respond(401, "Unauthorized")

        if supplied:
            if not self._take_room_token_if_valid(connection, supplied):
                self._take_issued_token_if_valid(supplied)
        return None

    async def start(self) -> None:
        from nanobot.channels.websocket_server import run_channel_server

        self._running = True
        self._stop_event = asyncio.Event()
        await self._recover_chat_inbox()

        ssl_context = self._build_ssl_context()
        scheme = "wss" if ssl_context else "ws"

        logger.info(
            "WebSocket server listening on {}://{}:{}{}",
            scheme,
            self.config.host,
            self.config.port,
            self.config.path,
        )
        if self.config.token_issue_path:
            logger.info(
                "WebSocket token issue route: {}://{}:{}{}",
                scheme,
                self.config.host,
                self.config.port,
                _normalize_config_path(self.config.token_issue_path),
            )

        async def runner() -> None:
            assert self._stop_event is not None
            await run_channel_server(
                self,
                host=self.config.host,
                port=self.config.port,
                max_message_bytes=self.config.max_message_bytes,
                ping_interval_s=self.config.ping_interval_s,
                ssl_context=ssl_context,
                stop_event=self._stop_event,
            )

        self._server_task = asyncio.create_task(runner())
        await self._server_task

    async def _connection_loop(self, connection: Any) -> None:
        request = connection.request
        path_part = request.path if request else "/"
        _, query = _parse_request_path(path_part)
        client_id_raw = _query_first(query, "client_id")
        client_id = client_id_raw.strip() if client_id_raw else ""
        if not client_id:
            client_id = f"anon-{uuid.uuid4().hex[:12]}"
        elif len(client_id) > 128:
            logger.warning("websocket: client_id too long ({} chars), truncating", len(client_id))
            client_id = client_id[:128]

        room_credential = self._conn_room.get(connection)
        default_chat_id = (
            room_credential.chat_id
            if room_credential is not None
            else str(uuid.uuid4())
        )

        try:
            await connection.send(
                json.dumps(
                    {
                        "event": "ready",
                        "chat_id": default_chat_id,
                        "client_id": client_id,
                    },
                    ensure_ascii=False,
                )
            )
            # Register only after ready is successfully sent to avoid out-of-order sends
            self._conn_default[connection] = default_chat_id
            self._attach(connection, default_chat_id)

            async for raw in connection:
                if isinstance(raw, bytes):
                    try:
                        raw = raw.decode("utf-8")
                    except UnicodeDecodeError:
                        logger.warning("websocket: ignoring non-utf8 binary frame")
                        continue

                envelope = _parse_envelope(raw)
                if envelope is not None:
                    await self._dispatch_envelope(connection, client_id, envelope)
                    continue

                content = _parse_inbound_payload(raw)
                if content is None:
                    continue
                room = self._conn_room.get(connection)
                metadata: dict[str, Any] = {
                    "remote": getattr(connection, "remote_address", None)
                }
                sender_id = client_id
                if room is not None:
                    sender_id = room.participant_id
                    metadata.update(
                        {
                            "shared_room": True,
                            "room_id": room.room_id,
                            "participant_id": room.participant_id,
                            "participant_display_name": room.display_name,
                        }
                    )
                await self._handle_message(
                    sender_id=sender_id,
                    chat_id=default_chat_id,
                    content=content,
                    metadata=metadata,
                )
        except Exception as e:
            logger.debug("websocket connection ended: {}", e)
        finally:
            self._cleanup_connection(connection)

    @staticmethod
    def _save_envelope_media(
        media: list[Any],
    ) -> tuple[list[str], str | None]:
        """Decode and persist ``media`` items from a ``message`` envelope.

        Returns ``(paths, None)`` on success or ``([], reason)`` on the first
        failure — the caller is expected to surface ``reason`` to the client
        and skip publishing so no half-formed message ever reaches the agent.
        On failure, any files already written to disk earlier in the same
        call are unlinked so partial ingress doesn't leak orphan files.
        ``reason`` is a short, stable token suitable for UI localization.

        Shape: ``list[{"data_url": str, "name"?: str | None}]``.
        """
        image_count = 0
        video_count = 0
        document_count = 0
        for item in media:
            mime = (
                _extract_data_url_mime(item.get("data_url", "")) if isinstance(item, dict) else None
            )
            if mime in _VIDEO_MIME_ALLOWED:
                video_count += 1
            elif mime in _IMAGE_MIME_ALLOWED:
                image_count += 1
            elif mime in _DOCUMENT_MIME_ALLOWED:
                document_count += 1
        if image_count > _MAX_IMAGES_PER_MESSAGE:
            return [], "too_many_images"
        if video_count > _MAX_VIDEOS_PER_MESSAGE:
            return [], "too_many_videos"
        if document_count > _MAX_DOCUMENTS_PER_MESSAGE:
            return [], "too_many_documents"

        media_dir = get_media_dir("websocket")
        paths: list[str] = []
        document_bytes = 0

        def _abort(reason: str) -> tuple[list[str], str]:
            for p in paths:
                try:
                    Path(p).unlink(missing_ok=True)
                except OSError as exc:
                    logger.warning("websocket: failed to unlink partial media {}: {}", p, exc)
            return [], reason

        for item in media:
            if not isinstance(item, dict):
                return _abort("malformed")
            data_url = item.get("data_url")
            if not isinstance(data_url, str) or not data_url:
                return _abort("malformed")
            mime = _extract_data_url_mime(data_url)
            if mime is None:
                return _abort("decode")
            if mime not in _UPLOAD_MIME_ALLOWED:
                return _abort("mime")
            is_video = mime in _VIDEO_MIME_ALLOWED
            is_document = mime in _DOCUMENT_MIME_ALLOWED
            filename = None
            if is_document:
                filename = _document_filename(mime, item.get("name"))
                if filename is None:
                    return _abort("extension")
            if is_video:
                max_bytes = _MAX_VIDEO_BYTES
            elif is_document:
                max_bytes = _MAX_DOCUMENT_BYTES
            else:
                max_bytes = _MAX_IMAGE_BYTES
            try:
                saved = save_base64_data_url(
                    data_url,
                    media_dir,
                    max_bytes=max_bytes,
                    filename=filename,
                )
            except FileSizeExceeded:
                return _abort("size")
            except Exception as exc:
                logger.warning("websocket: media decode failed: {}", exc)
                return _abort("decode")
            if saved is None:
                return _abort("decode")
            paths.append(saved)
            if is_document:
                saved_path = Path(saved)
                if not _document_content_is_valid(saved_path, mime):
                    return _abort("content")
                document_bytes += saved_path.stat().st_size
                if document_bytes > _MAX_DOCUMENT_BYTES_PER_MESSAGE:
                    return _abort("total_size")
        return paths, None

    async def _dispatch_envelope(
        self,
        connection: Any,
        client_id: str,
        envelope: dict[str, Any],
    ) -> None:
        """Route one typed inbound envelope (``new_chat`` / ``attach`` / ``message``)."""
        t = envelope.get("type")
        scoped_room = self._conn_room.get(connection)
        if t == "new_chat":
            if scoped_room is not None:
                await self._send_event(connection, "error", detail="room scope violation")
                return
            new_id = str(uuid.uuid4())
            self._attach(connection, new_id)
            await self._send_event(connection, "attached", chat_id=new_id)
            return
        if t == "attach":
            cid = envelope.get("chat_id")
            if not _is_valid_chat_id(cid):
                await self._send_event(connection, "error", detail="invalid chat_id")
                return
            if scoped_room is not None and cid != scoped_room.chat_id:
                await self._send_event(connection, "error", detail="room scope violation")
                return
            self._attach(connection, cid)
            await self._send_event(connection, "attached", chat_id=cid)
            return
        if t == "message":
            cid = envelope.get("chat_id")
            content = envelope.get("content")
            raw_client_message_id = envelope.get("client_message_id")
            if raw_client_message_id is not None and (
                not isinstance(raw_client_message_id, str)
                or _CLIENT_MESSAGE_ID_RE.fullmatch(raw_client_message_id.lower()) is None
            ):
                await self._send_event(
                    connection,
                    "error",
                    detail="invalid client_message_id",
                )
                return
            client_message_id = (
                raw_client_message_id.lower()
                if isinstance(raw_client_message_id, str)
                else None
            )

            async def reject(detail: str, **fields: Any) -> None:
                if client_message_id is None:
                    await self._send_event(connection, "error", detail=detail, **fields)
                    return
                await self._send_message_ack(
                    connection,
                    chat_id=cid if isinstance(cid, str) else "",
                    client_message_id=client_message_id,
                    status="rejected",
                    detail=fields.get("message") or detail,
                )

            if not _is_valid_chat_id(cid):
                await reject("invalid chat_id")
                return
            if scoped_room is not None and cid != scoped_room.chat_id:
                await reject("room scope violation")
                return
            if not isinstance(content, str):
                await reject("missing content")
                return
            if (
                client_message_id is not None
                and self._chat_inbox is None
                and (cid, client_message_id) in self._accepted_client_messages
            ):
                await self._send_message_ack(
                    connection,
                    chat_id=cid,
                    client_message_id=client_message_id,
                    status="duplicate",
                )
                return

            metadata: dict[str, Any] = {
                "remote": getattr(connection, "remote_address", None)
            }
            room = scoped_room or self._shared_room_owner_credential(cid)
            sender_id = client_id
            if room is not None:
                sender_id = room.participant_id
                metadata.update(
                    {
                        "shared_room": True,
                        "room_id": room.room_id,
                        "participant_id": room.participant_id,
                        "participant_display_name": room.display_name,
                    }
                )
            if client_message_id is not None:
                metadata["client_message_id"] = client_message_id
            reasoning_profile = envelope.get("reasoning_profile")
            if reasoning_profile is not None:
                if (
                    not isinstance(reasoning_profile, str)
                    or reasoning_profile.lower() not in _REASONING_PROFILES
                ):
                    await reject("invalid reasoning_profile")
                    return
                metadata["reasoning_profile"] = reasoning_profile.lower()
            reasoning_effort = envelope.get("reasoning_effort")
            if reasoning_effort is not None:
                if reasoning_profile is not None:
                    await reject("reasoning_profile cannot be combined with reasoning_effort")
                    return
                if (
                    not isinstance(reasoning_effort, str)
                    or reasoning_effort.lower() not in _REASONING_EFFORTS
                ):
                    await reject("invalid reasoning_effort")
                    return
                metadata["reasoning_effort"] = reasoning_effort.lower()
            max_tokens = envelope.get("max_tokens")
            if max_tokens is not None:
                if reasoning_profile is not None:
                    await reject("reasoning_profile cannot be combined with max_tokens")
                    return
                if (
                    not isinstance(max_tokens, int)
                    or isinstance(max_tokens, bool)
                    or not 1 <= max_tokens <= 262_144
                ):
                    await reject("invalid max_tokens")
                    return
                metadata["max_tokens"] = max_tokens

            raw_media = envelope.get("media")
            media_paths: list[str] = []
            if raw_media is not None:
                if not isinstance(raw_media, list):
                    await reject(
                        "attachment_rejected",
                        reason="malformed",
                        message=_attachment_rejection_message("malformed"),
                    )
                    return
                if room is not None and raw_media:
                    await reject(
                        "attachment_rejected",
                        message="Attachments are not available in shared rooms yet.",
                    )
                    return
                media_paths, reason = self._save_envelope_media(raw_media)
                if reason is not None:
                    await reject(
                        "attachment_rejected",
                        reason=reason,
                        message=_attachment_rejection_message(reason),
                    )
                    return

            # Allow attachment-only turns (content may be empty when media is attached).
            if not content.strip() and not media_paths:
                await reject("missing content")
                return

            # Auto-attach on first use so clients can one-shot without a separate attach.
            self._attach(connection, cid)
            if client_message_id is None:
                await self._handle_message(
                    sender_id=sender_id,
                    chat_id=cid,
                    content=content,
                    media=media_paths or None,
                    metadata=metadata,
                )
                return

            prepared = await self._prepare_message(
                sender_id=sender_id,
                chat_id=cid,
                content=content,
                media=media_paths or None,
                metadata=metadata,
            )
            if prepared is None:
                await reject("Message was not accepted.")
                return

            if self._chat_inbox is not None:
                disposition, record = await self._chat_inbox.accept(
                    prepared,
                    client_message_id,
                )
                if disposition == "conflict":
                    self._discard_duplicate_media(prepared.media, record.message.media)
                    await reject(
                        "client_message_id was already used for different content"
                    )
                    return
                if disposition == "existing":
                    self._discard_duplicate_media(prepared.media, record.message.media)
                claimed = await self._chat_inbox.claim_for_enqueue(
                    cid,
                    client_message_id,
                )
                if not claimed:
                    await self._send_message_ack(
                        connection,
                        chat_id=cid,
                        client_message_id=client_message_id,
                        status="duplicate",
                    )
                    return
                try:
                    await self.bus.publish_inbound(record.message)
                except Exception:
                    await self._chat_inbox.release_enqueue_claim(
                        cid,
                        client_message_id,
                    )
                    raise
            else:
                self._remember_client_message(cid, client_message_id)
                try:
                    await self.bus.publish_inbound(prepared)
                except Exception:
                    self._accepted_client_messages.pop((cid, client_message_id), None)
                    raise

            await self._send_message_ack(
                connection,
                chat_id=cid,
                client_message_id=client_message_id,
                status="accepted",
            )
            if room is not None:
                await self._broadcast_event(
                    cid,
                    "participant.message",
                    client_message_id=client_message_id,
                    participant_id=room.participant_id,
                    display_name=room.display_name,
                    content=content,
                    timestamp=datetime.now(timezone.utc).isoformat(),
                )
            return
        if scoped_room is not None and t in {
            "work.create",
            "work.subscribe",
            "work.cancel",
            "work.message",
        }:
            await self._send_event(connection, "error", detail="room scope violation")
            return
        if t == "work.create":
            await self._handle_work_create_envelope(connection, client_id, envelope)
            return
        if t == "work.subscribe":
            await self._handle_work_subscribe_envelope(connection, envelope)
            return
        if t == "work.cancel":
            await self._handle_work_cancel_envelope(connection, envelope)
            return
        if t == "work.message":
            await self._handle_work_message_envelope(connection, client_id, envelope)
            return
        await self._send_event(connection, "error", detail=f"unknown type: {t!r}")

    @staticmethod
    def _discard_duplicate_media(candidate: list[str], retained: list[str]) -> None:
        retained_paths = set(retained)
        for item in candidate:
            if item in retained_paths:
                continue
            try:
                Path(item).unlink(missing_ok=True)
            except OSError as exc:
                logger.warning("websocket: failed to remove duplicate media {}: {}", item, exc)

    async def _handle_work_create_envelope(
        self,
        connection: Any,
        client_id: str,
        envelope: dict[str, Any],
    ) -> None:
        if self._work_store is None:
            await self._send_event(connection, "error", detail="work unavailable")
            return
        chat_id = envelope.get("chat_id")
        content = envelope.get("content")
        request_id = envelope.get("idempotency_key")
        if not _is_valid_chat_id(chat_id):
            await self._send_event(connection, "error", detail="invalid chat_id")
            return
        if not isinstance(content, str) or not content.strip():
            await self._send_event(connection, "error", detail="missing content")
            return
        reasoning_profile = parse_reasoning_profile(
            envelope.get("reasoning_profile", ReasoningProfile.AUTO.value)
        )
        if reasoning_profile is None:
            await self._send_event(
                connection, "error", detail="invalid reasoning_profile"
            )
            return
        if request_id is not None and (
            not isinstance(request_id, str) or _WORK_ID_RE.fullmatch(request_id) is None
        ):
            await self._send_event(connection, "error", detail="invalid idempotency key")
            return
        media_paths, media_error = self._work_media(envelope.get("media"))
        if media_error is not None:
            await self._send_event(
                connection,
                "error",
                detail="attachment_rejected",
                reason=media_error,
                message=_attachment_rejection_message(media_error),
            )
            return
        task = await self._work_store.run_io(
            self._work_store.create_task,
            chat_id=chat_id,
            content=content,
            mode="background",
            title=(envelope.get("title") if isinstance(envelope.get("title"), str) else None),
            model=_read_webui_model_name() or "",
            reasoning_profile=reasoning_profile.value,
            request_id=request_id,
        )
        was_created = bool(task.pop("_was_created", True))
        was_dispatched = bool(task.pop("_was_dispatched", False))
        task_id = str(task["task_id"])
        self._attach(connection, chat_id)
        self._attach_work(connection, task_id)
        await self._send_event(
            connection,
            "work.created",
            task_id=task_id,
            task=task,
        )
        if not was_created:
            if was_dispatched:
                await self._replay_work_events(connection, task_id, after_seq=0)
            return
        try:
            await self._publish_work_inbound(
                task,
                sender_id=client_id,
                content=content,
                media=media_paths,
                remote=getattr(connection, "remote_address", None),
            )
            if request_id is not None:
                await self._work_store.run_io(self._work_store.mark_dispatched, task_id, request_id)
        except Exception:
            logger.exception("failed to enqueue WebSocket Work task {}", task_id)
            await self._fail_work_enqueue(task_id)
            await self._send_event(
                connection,
                "error",
                detail="failed to enqueue work",
                task_id=task_id,
            )

    async def _handle_work_subscribe_envelope(
        self, connection: Any, envelope: dict[str, Any]
    ) -> None:
        if self._work_store is None:
            await self._send_event(connection, "error", detail="work unavailable")
            return
        task_id = envelope.get("task_id")
        if not isinstance(task_id, str) or _WORK_ID_RE.fullmatch(task_id) is None:
            await self._send_event(connection, "error", detail="task not found")
            return
        if await self._work_store.run_io(self._work_store.get_task, task_id) is None:
            await self._send_event(connection, "error", detail="task not found")
            return
        try:
            after_seq = max(0, int(envelope.get("after_seq") or 0))
        except (TypeError, ValueError):
            await self._send_event(connection, "error", detail="invalid after_seq")
            return
        self._attach_work(connection, task_id)
        await self._send_event(connection, "work.subscribed", task_id=task_id)
        await self._replay_work_events(connection, task_id, after_seq=after_seq)

    async def _replay_work_events(self, connection: Any, task_id: str, *, after_seq: int) -> None:
        cursor = after_seq
        while True:
            events = await self._work_store.run_io(
                self._work_store.list_events,
                task_id,
                after_seq=cursor,
                limit=MAX_EVENT_PAGE,
            )
            for event in events:
                await self._send_work_event(connection, event)
            if len(events) < MAX_EVENT_PAGE:
                break
            cursor = int(events[-1]["seq"])
            await asyncio.sleep(0)

    async def _handle_work_cancel_envelope(self, connection: Any, envelope: dict[str, Any]) -> None:
        if self._work_store is None:
            await self._send_event(connection, "error", detail="work unavailable")
            return
        task_id = envelope.get("task_id")
        if not isinstance(task_id, str) or _WORK_ID_RE.fullmatch(task_id) is None:
            await self._send_event(connection, "error", detail="task not found")
            return
        task = await self._work_store.run_io(self._work_store.get_task, task_id)
        if task is None:
            await self._send_event(connection, "error", detail="task not found")
            return
        assert task is not None
        self._attach_work(connection, task_id)
        error = await self._cancel_work_task(task, sender_id="websocket")
        if error == "terminal":
            await self._send_event(connection, "error", detail="task already complete")
            return
        if error is not None:
            await self._send_event(connection, "error", detail="failed to signal cancellation")

    async def _handle_work_message_envelope(
        self,
        connection: Any,
        client_id: str,
        envelope: dict[str, Any],
    ) -> None:
        if self._work_store is None:
            await self._send_event(connection, "error", detail="work unavailable")
            return
        task_id = envelope.get("task_id")
        content = envelope.get("content")
        command_id = envelope.get("idempotency_key")
        if not isinstance(task_id, str) or _WORK_ID_RE.fullmatch(task_id) is None:
            await self._send_event(connection, "error", detail="task not found")
            return
        task = await self._work_store.run_io(self._work_store.get_task, task_id)
        if task is None:
            await self._send_event(connection, "error", detail="task not found")
            return
        if task.get("status") not in ACTIVE_STATUSES:
            await self._send_event(connection, "error", detail="task does not accept messages")
            return
        if not isinstance(content, str) or not content.strip():
            await self._send_event(connection, "error", detail="missing content")
            return
        if command_id is not None and (
            not isinstance(command_id, str) or _COMMAND_ID_RE.fullmatch(command_id) is None
        ):
            await self._send_event(connection, "error", detail="invalid idempotency key")
            return
        chat_id = str(task.get("chat_id") or "")
        if not _is_valid_chat_id(chat_id):
            await self._send_event(connection, "error", detail="invalid task chat")
            return
        self._attach_work(connection, task_id)
        if command_id is not None:
            try:
                reserved, _ = await self._work_store.run_io(
                    self._work_store.reserve_command,
                    command_id,
                    task_id,
                    "message",
                )
            except ValueError:
                await self._send_event(connection, "error", detail="invalid idempotency key")
                return
            if not reserved:
                return
        try:
            await self._publish_work_inbound(
                task,
                sender_id=client_id,
                content=content,
                remote=getattr(connection, "remote_address", None),
            )
        except Exception:
            logger.exception("failed to enqueue message for Work task {}", task_id)
            if command_id is not None:
                try:
                    await self._work_store.run_io(
                        self._work_store.release_command,
                        command_id,
                        task_id,
                        "message",
                    )
                except Exception:
                    logger.exception("failed to release Work command {}", command_id)
            await self._send_event(connection, "error", detail="failed to enqueue work message")
            return
        await self._record_work_message(task_id, content)
        if command_id is not None:
            await self._work_store.run_io(self._work_store.mark_command_dispatched, command_id)

    async def _send_work_event(self, connection: Any, event: Any) -> None:
        data = event.to_api() if hasattr(event, "to_api") else event
        await self._send_event(
            connection,
            "work.event",
            task_id=data.get("task_id"),
            seq=data.get("seq"),
            type=data.get("type"),
            payload=data.get("payload") or {},
            actor=data.get("actor"),
            step_id=data.get("step_id"),
            created_at=data.get("created_at"),
        )

    async def _broadcast_work_event(self, event: Any) -> None:
        data = event.to_api() if hasattr(event, "to_api") else event
        task_id = data.get("task_id") if isinstance(data, dict) else None
        if not isinstance(task_id, str):
            return
        raw = json.dumps(
            {
                "event": "work.event",
                "task_id": task_id,
                "seq": data.get("seq"),
                "type": data.get("type"),
                "payload": data.get("payload") or {},
                "actor": data.get("actor"),
                "step_id": data.get("step_id"),
                "created_at": data.get("created_at"),
            },
            ensure_ascii=False,
        )
        for connection in list(self._work_subs.get(task_id, ())):
            await self._safe_send_to(connection, raw, label=" work ")

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        if self._stop_event:
            self._stop_event.set()
        if self._server_task:
            try:
                await self._server_task
            except Exception as e:
                logger.warning("websocket: server task error during shutdown: {}", e)
            self._server_task = None
        self._subs.clear()
        self._conn_chats.clear()
        self._conn_default.clear()
        self._work_subs.clear()
        self._conn_work.clear()
        self._issued_tokens.clear()
        self._api_tokens.clear()

    async def _safe_send_to(self, connection: Any, raw: str, *, label: str = "") -> None:
        """Send a raw frame to one connection, cleaning up on ConnectionClosed."""
        try:
            await connection.send(raw)
        except ConnectionClosed:
            self._cleanup_connection(connection)
            logger.warning("websocket{}connection gone", label)
        except Exception as e:
            logger.error("websocket{}send failed: {}", label, e)
            raise

    async def send(self, msg: OutboundMessage) -> None:
        if msg.metadata.get("_work_event"):
            await self._broadcast_work_event(msg.metadata["_work_event"])
            return
        # Snapshot the subscriber set so ConnectionClosed cleanups mid-iteration are safe.
        conns = list(self._subs.get(msg.chat_id, ()))
        if not conns:
            logger.warning("websocket: no active subscribers for chat_id={}", msg.chat_id)
            return
        text = msg.content
        if msg.buttons:
            text = _append_buttons_as_text(text, msg.buttons)
        payload: dict[str, Any] = {
            "event": "message",
            "chat_id": msg.chat_id,
            "text": text,
        }
        if msg.buttons:
            payload["buttons"] = msg.buttons
            payload["button_prompt"] = msg.content
        if msg.media:
            accepted_media: list[str] = []
            urls: list[dict[str, str]] = []
            for entry in msg.media:
                if isinstance(entry, str) and entry.lower().startswith(("http://", "https://")):
                    accepted_media.append(entry)
                    continue
                signed = self._sign_or_stage_media_path(Path(entry))
                if signed is not None:
                    accepted_media.append(entry)
                    urls.append(signed)
            if accepted_media:
                payload["media"] = accepted_media
            if urls:
                payload["media_urls"] = urls
        artifact_urls = self._work_artifact_media_urls(msg.metadata.get("_work_artifacts"))
        if artifact_urls:
            payload["media_urls"] = [
                *(payload.get("media_urls") or []),
                *artifact_urls,
            ]
        if msg.reply_to:
            payload["reply_to"] = msg.reply_to
        # Mark intermediate agent breadcrumbs (tool-call hints, generic
        # progress strings) so WS clients can render them as subordinate
        # trace rows rather than conversational replies.
        if msg.metadata.get("_tool_hint"):
            payload["kind"] = "tool_hint"
        elif msg.metadata.get("_progress"):
            payload["kind"] = "progress"
        raw = json.dumps(payload, ensure_ascii=False)
        for connection in conns:
            await self._safe_send_to(connection, raw, label=" ")

    async def send_delta(
        self,
        chat_id: str,
        delta: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        conns = list(self._subs.get(chat_id, ()))
        if not conns:
            return
        meta = metadata or {}
        if meta.get("_stream_end"):
            body: dict[str, Any] = {"event": "stream_end", "chat_id": chat_id}
        else:
            body = {
                "event": "delta",
                "chat_id": chat_id,
                "text": delta,
            }
        if meta.get("_stream_id") is not None:
            body["stream_id"] = meta["_stream_id"]
        raw = json.dumps(body, ensure_ascii=False)
        for connection in conns:
            await self._safe_send_to(connection, raw, label=" stream ")
