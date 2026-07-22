"""aiohttp listener adapter for the WebSocket channel.

The upstream ``websockets`` HTTP parser accepts GET only. This adapter keeps
the channel's existing response and connection contracts while allowing the
embedded REST surface to expose real POST endpoints on the same port.
"""

from __future__ import annotations

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
    method: str
    path: str
    headers: Any
    body: bytes = b""


@dataclass(slots=True)
class TransportFileResponse:
    path: Path
    content_type: str
    headers: dict[str, str]


class AiohttpConnection:
    """Expose the connection subset consumed by ``WebSocketChannel``."""

    def __init__(self, request: web.Request, transport_request: TransportRequest) -> None:
        self.request = transport_request
        transport = request.transport
        self.remote_address = transport.get_extra_info("peername") if transport else None
        self._websocket: web.WebSocketResponse | None = None

    def bind(self, websocket: web.WebSocketResponse) -> None:
        self._websocket = websocket

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
        if self._websocket is None:
            raise RuntimeError("WebSocket is not prepared")
        if isinstance(data, bytes):
            await self._websocket.send_bytes(data)
        else:
            await self._websocket.send_str(data)

    def __aiter__(self) -> AsyncIterator[str | bytes]:
        return self._messages()

    async def _messages(self) -> AsyncIterator[str | bytes]:
        if self._websocket is None:
            return
        async for message in self._websocket:
            if message.type == WSMsgType.TEXT:
                yield message.data
            elif message.type == WSMsgType.BINARY:
                yield message.data
            elif message.type in {
                WSMsgType.CLOSE,
                WSMsgType.CLOSED,
                WSMsgType.CLOSING,
                WSMsgType.ERROR,
            }:
                return


def _to_aiohttp_response(response: Response) -> web.Response:
    headers = [
        (name, value)
        for name, value in response.headers.raw_items()
        if name.lower() not in {"connection", "content-length", "date"}
    ]
    return web.Response(status=response.status_code, body=response.body, headers=headers)


def create_websocket_response(
    *, max_message_bytes: int, ping_interval_s: float | None
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
    stop_event: Any,
) -> None:
    async def handle(request: web.Request) -> web.StreamResponse:
        transport_request = TransportRequest(
            method=request.method,
            path=request.path_qs,
            headers=request.headers,
            body=await request.read(),
        )
        connection = AiohttpConnection(request, transport_request)
        response = await channel._dispatch_http(connection, transport_request)
        if isinstance(response, TransportFileResponse):
            file_response = web.FileResponse(response.path, headers=response.headers)
            file_response.content_type = response.content_type
            return file_response
        if response is not None:
            return _to_aiohttp_response(response)

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
    site = web.TCPSite(runner, host, port, ssl_context=ssl_context)
    try:
        await site.start()
        await stop_event.wait()
    finally:
        await runner.cleanup()
