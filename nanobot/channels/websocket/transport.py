"""aiohttp listener adapter for the WebSocket channel (Ziggy-local, MIT-1010).

Why this exists
---------------
Upstream serves the gateway with ``websockets.asyncio.server``.  Its HTTP parser
reads a request line and headers only -- there is no request **body**.  That is
why 0.3.0 moved every WebUI mutation onto the authenticated ``webui_request``
WebSocket frame (``GatewayHTTPHandler.dispatch_webui_mutation``) and returns 405
for a mutation arriving over HTTP.

Ziggy's shared-room control plane is not a WebUI client.  ``ziggy-control``
(``services/ziggy-control/internal/httpapi/shared_rooms.go``) POSTs JSON to
``/auth/shared-rooms``, ``/auth/shared-room-token``, ``/auth/shared-room-revoke``
and ``/auth/shared-rooms/title`` with a ``tokenIssueSecret`` header.  Under the
upstream listener those bodies are unreadable, so every room operation would
fail at cutover.

This adapter is the transport the deployed 0.2.x snapshot already runs
(``nanobot/channels/websocket_server.py``).  Carrying it forward keeps the Go
and Swift wire contract byte-identical; it is opt-in
(``websocket.transport = "aiohttp"``, selected automatically when shared rooms
are configured) so tenants that do not need POST bodies keep upstream's
listener, including its slow-client isolation and degraded-listener recovery.

Tradeoff, recorded deliberately: those two listener-health behaviours are
``websockets``-specific and do **not** apply under this transport.  aiohttp's
own ``max_msg_size`` and heartbeat cover the message-size and liveness half.
"""

from __future__ import annotations

import asyncio
import email.utils
import http
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator

from aiohttp import WSMsgType, web
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosedError
from websockets.http11 import Response


@dataclass
class TransportRequest:
    """The request shape ``GatewayHTTPHandler`` and the handshake path consume.

    Deliberately **not** ``slots=True``: ``GatewayHTTPHandler.dispatch`` stamps
    ``_nanobot_trusted_proxy_authenticated`` (and the WebUI-mutation attributes)
    onto the request object with ``setattr`` (``webui/ws_http.py:470``). A
    slotted dataclass raises ``AttributeError`` there, which would turn every
    HTTP request under this transport into a 500.
    """

    method: str
    path: str
    headers: Any
    body: bytes = b""
    # aiohttp's ``path_qs`` decodes percent escapes. Keep the original request
    # target for routes whose authorization contract is canonical (published
    # files, session keys) so normalization can never broaden a grant.
    raw_path: str | None = None


@dataclass(slots=True)
class TransportFileResponse:
    """Stream a file from disk instead of buffering it into a Response body."""

    path: Path
    content_type: str
    headers: dict[str, str]


class AiohttpConnection:
    """Expose the ``ServerConnection`` subset ``WebSocketChannel`` consumes."""

    def __init__(self, request: web.Request, transport_request: TransportRequest) -> None:
        self.request = transport_request
        transport = request.transport
        self.remote_address = transport.get_extra_info("peername") if transport else None
        self._ws: web.WebSocketResponse | None = None

    def bind(self, socket: web.WebSocketResponse) -> None:
        self._ws = socket

    def respond(self, status: int, text: str) -> Response:
        body = text.encode("utf-8")
        return Response(
            status,
            http.HTTPStatus(status).phrase,
            Headers(
                [
                    ("Date", email.utils.formatdate(usegmt=True)),
                    ("Connection", "close"),
                    ("Content-Length", str(len(body))),
                    ("Content-Type", "text/plain; charset=utf-8"),
                ]
            ),
            body,
        )

    async def send(self, data: str | bytes) -> None:
        if self._ws is None:
            raise RuntimeError("WebSocket is not prepared")
        try:
            if isinstance(data, bytes):
                await self._ws.send_bytes(data)
            else:
                await self._ws.send_str(data)
        except (ConnectionResetError, RuntimeError) as exc:
            # The channel's outbound writer treats a normal disconnect as
            # ``websockets.exceptions.ConnectionClosed`` and everything else as
            # a bug worth a stack trace. aiohttp raises ConnectionResetError (or
            # RuntimeError once the response is closed) instead, so translate --
            # otherwise every routine client disconnect logs an exception and
            # triggers a spurious 1011 retirement.
            raise ConnectionClosedError(None, None) from exc

    async def close(self, *, code: int = 1000, reason: str = "") -> None:
        if self._ws is not None:
            await self._ws.close(code=code, message=reason.encode("utf-8"))

    def __aiter__(self) -> AsyncIterator[str | bytes]:
        return self._messages()

    async def _messages(self) -> AsyncIterator[str | bytes]:
        if self._ws is None:
            return
        async for message in self._ws:
            if message.type in {WSMsgType.TEXT, WSMsgType.BINARY}:
                yield message.data
            elif message.type in {
                WSMsgType.CLOSE,
                WSMsgType.CLOSED,
                WSMsgType.CLOSING,
                WSMsgType.ERROR,
            }:
                return


def to_aiohttp_response(response: Response) -> web.Response:
    headers = [
        (name, value)
        for name, value in response.headers.raw_items()
        if name.lower() not in {"connection", "content-length", "date"}
    ]
    return web.Response(status=response.status_code, body=response.body, headers=headers)


def create_websocket_response(
    *,
    max_message_bytes: int,
    ping_interval_s: float | None,
) -> web.WebSocketResponse:
    return web.WebSocketResponse(
        max_msg_size=max_message_bytes,
        heartbeat=ping_interval_s,
        autoclose=True,
        autoping=True,
    )


async def run_channel_server(
    channel: Any,
    *,
    host: str,
    port: int,
    max_message_bytes: int,
    ping_interval_s: float | None,
    ssl_context: Any,
    stop_event: asyncio.Event,
    unix_socket_path: str = "",
) -> None:
    """Serve *channel* over aiohttp, routing HTTP and WebSocket on one listener."""

    async def handle(request: web.Request) -> web.StreamResponse:
        transport_request = TransportRequest(
            method=request.method,
            path=request.raw_path,
            headers=request.headers,
            raw_path=request.raw_path,
        )
        connection = AiohttpConnection(request, transport_request)
        if request.can_read_body and request.method in {"POST", "PUT", "PATCH"}:
            # Reject *before* buffering a body so an unauthenticated client
            # cannot multiply max_message_bytes across all front-door slots.
            # Two gates, because two credentials:
            #   /api/*   -> the short-lived REST bearer
            #   /auth/*  -> the tenant tokenIssueSecret (an HMAC compare, no
            #               I/O, so it is safe to run this early; the handlers
            #               still re-check it on their own)
            # Anything else that carries a body is unrouted, so refuse it
            # rather than buffering for a 404.
            if request.path.startswith("/api/"):
                if not channel.check_api_token(transport_request):
                    return to_aiohttp_response(connection.respond(401, "Unauthorized"))
            elif request.path.startswith("/auth/"):
                if not channel.check_issue_route_secret(transport_request):
                    return to_aiohttp_response(connection.respond(401, "Unauthorized"))
            else:
                return to_aiohttp_response(connection.respond(404, "Not Found"))
            try:
                transport_request.body = await asyncio.wait_for(request.read(), timeout=10.0)
            except (TimeoutError, asyncio.TimeoutError):
                return to_aiohttp_response(connection.respond(408, "Request Timeout"))
            except web.HTTPRequestEntityTooLarge:
                return to_aiohttp_response(connection.respond(413, "Payload Too Large"))

        response = await channel._dispatch_http(connection, transport_request)
        if isinstance(response, TransportFileResponse):
            file_response = web.FileResponse(response.path, headers=response.headers)
            file_response.content_type = response.content_type
            return file_response
        if response is not None:
            return to_aiohttp_response(response)

        websocket = create_websocket_response(
            max_message_bytes=max_message_bytes,
            ping_interval_s=ping_interval_s,
        )
        await websocket.prepare(request)
        connection.bind(websocket)
        await channel._connection_loop(connection)
        return websocket

    app = web.Application(client_max_size=max_message_bytes)
    app.router.add_route("*", "/{tail:.*}", handle)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site: web.BaseSite
    if unix_socket_path:
        # Match what the websockets listener does for a unix socket: create the
        # parent directory, clear a stale socket so a restart does not fail with
        # EADDRINUSE, and chmod 0600 so the socket is not world-reachable at the
        # process umask.
        socket_file = Path(unix_socket_path)
        socket_file.parent.mkdir(parents=True, exist_ok=True)
        with suppress(FileNotFoundError):
            socket_file.unlink()
        site = web.UnixSite(runner, unix_socket_path, ssl_context=ssl_context)
    else:
        site = web.TCPSite(runner, host, port, ssl_context=ssl_context)
    try:
        await site.start()
        if unix_socket_path:
            with suppress(OSError):
                Path(unix_socket_path).chmod(0o600)
        await stop_event.wait()
    finally:
        await runner.cleanup()


__all__ = [
    "AiohttpConnection",
    "TransportFileResponse",
    "TransportRequest",
    "create_websocket_response",
    "run_channel_server",
    "to_aiohttp_response",
]
