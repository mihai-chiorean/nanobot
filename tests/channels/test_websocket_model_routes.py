from __future__ import annotations

import asyncio
import functools
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from nanobot.channels.websocket import WebSocketChannel
from nanobot.session.manager import SessionManager


async def _request(method: str, url: str, **kwargs: Any) -> httpx.Response:
    return await asyncio.to_thread(
        functools.partial(httpx.request, method, url, timeout=5.0, **kwargs)
    )


@pytest.mark.asyncio
async def test_authenticated_model_status_and_switch_routes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    port = 29936
    state = tmp_path / "model-state.json"
    state.write_text(json.dumps({"status": "ready", "active_model": "qwen"}))
    script = tmp_path / "ziggy-switch-model"
    script.write_text("#!/bin/sh\n")
    script.chmod(0o700)
    monkeypatch.setenv("ZIGGY_ACTIVE_MODEL_STATE", str(state))
    monkeypatch.setenv("ZIGGY_MODEL_SWITCH_SCRIPT", str(script))
    monkeypatch.setattr("nanobot.model_runtime.Path.home", lambda: tmp_path)
    popen = MagicMock()
    monkeypatch.setattr("nanobot.model_runtime.subprocess.Popen", popen)
    bus = MagicMock(publish_inbound=AsyncMock())
    channel = WebSocketChannel(
        {
            "enabled": True,
            "allowFrom": ["*"],
            "host": "127.0.0.1",
            "port": port,
            "path": "/",
        },
        bus,
        session_manager=SessionManager(tmp_path / "workspace"),
    )
    server = asyncio.create_task(channel.start())
    await asyncio.sleep(0.2)
    try:
        status_url = f"http://127.0.0.1:{port}/api/model/status"
        assert (await _request("GET", status_url)).status_code == 401
        bootstrap = await _request(
            "GET", f"http://127.0.0.1:{port}/webui/bootstrap"
        )
        headers = {"Authorization": f"Bearer {bootstrap.json()['token']}"}

        status = await _request("GET", status_url, headers=headers)
        assert status.status_code == 200
        assert status.json()["model_runtime"]["active_model"] == "qwen"

        invalid = await _request(
            "POST",
            f"http://127.0.0.1:{port}/api/model/switch?target=other",
            headers=headers,
        )
        assert invalid.status_code == 400
        switched = await _request(
            "POST",
            f"http://127.0.0.1:{port}/api/model/switch?target=minimax",
            headers=headers,
            json={"force": True},
        )
        assert switched.status_code == 202
        assert switched.json()["model_runtime"]["requested_target"] == "minimax"
        assert popen.call_args.args[0] == [str(script), "minimax", "--force"]
    finally:
        await channel.stop()
        await server
