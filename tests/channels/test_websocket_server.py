from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import websockets
from websockets.exceptions import ConnectionClosedError

from nanobot.channels.websocket import WebSocketChannel
from nanobot.channels.websocket_server import create_websocket_response


def test_heartbeat_configuration_is_passed_to_aiohttp() -> None:
    with patch("nanobot.channels.websocket_server.web.WebSocketResponse") as response:
        create_websocket_response(max_message_bytes=4096, ping_interval_s=None)
        response.assert_called_once_with(
            max_msg_size=4096,
            heartbeat=None,
            autoclose=True,
            autoping=True,
        )


@pytest.mark.asyncio
async def test_message_size_enforcement_and_clean_shutdown(tmp_path: Path) -> None:
    port = 29937
    bus = MagicMock(publish_inbound=AsyncMock())
    channel = WebSocketChannel(
        {
            "enabled": True,
            "allowFrom": ["*"],
            "host": "127.0.0.1",
            "port": port,
            "path": "/",
            "websocketRequiresToken": False,
            "maxMessageBytes": 1024,
            "pingIntervalS": None,
        },
        bus,
    )
    server = asyncio.create_task(channel.start())
    await asyncio.sleep(0.2)
    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}/") as client:
            assert json.loads(await client.recv())["event"] == "ready"
            await client.send("x" * 2048)
            with pytest.raises(ConnectionClosedError) as closed:
                await client.recv()
            assert closed.value.rcvd is not None
            assert closed.value.rcvd.code == 1009
    finally:
        await channel.stop()
        await server

    rebound = await asyncio.start_server(lambda _r, _w: None, "127.0.0.1", port)
    rebound.close()
    await rebound.wait_closed()
