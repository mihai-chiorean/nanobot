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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator

from aiohttp import WSMsgType, web
from websockets.datastructures import Headers
from websockets.http11 import Response


@dataclass(slots=True)
class TransportRequest:
    """The request shape ``GatewayHTTPHandler`` and the handshake path consume."""

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
        if isinstance(data, bytes):
            await self._ws.send_bytes(data)
        else:
            await self._ws.send_str(data)

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
            # Every mutation in the embedded /api surface requires the
            # short-lived REST bearer. Reject *before* buffering a body so an
            # unauthenticated client cannot multiply max_message_bytes across
            # all front-door slots. The /auth/* room routes carry their own
            # tokenIssueSecret check inside the handler.
            if request.path.startswith("/api/") and not channel.check_api_token(
                transport_request
            ):
                return to_aiohttp_response(connection.respond(401, "Unauthorized"))
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
        site = web.UnixSite(runner, unix_socket_path, ssl_context=ssl_context)
    else:
        site = web.TCPSite(runner, host, port, ssl_context=ssl_context)
    try:
        await site.start()
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
