"""MIT-1852: ``tools.skills.allow_install`` gates the WebUI skill install routes.

The install route used to trust its own "is this a local request?" judgement
before consulting ``tools.webuiAllowRemotePackageInstall``. Inside a tester
container that judgement is not something to rely on -- the proxied source
address is forgeable and the SkillHub provider installs over plain HTTP with no
``npx`` -- so the workspace kill switch is now checked first, and search /
trending / the skills payload report ``install_supported: false`` when it is off.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from nanobot.bus.queue import MessageBus
from nanobot.channels.websocket.runtime import WebSocketChannel, WebSocketConfig
from nanobot.channels.websocket.transport import TransportRequest
from nanobot.config.loader import save_config
from nanobot.config.schema import Config
from nanobot.session.manager import SessionManager
from nanobot.webui.gateway_services import build_gateway_services

_SKILLHUB_RESULT = {
    "slug": "ima-skills",
    "name": "ima-skills",
    "namespace": {"handle": "tencent-adm"},
    "installs": 11831,
    "downloads": 142525,
    "publisher": {"verified": True},
    "labels": {"requires_api_key": "true"},
}


class _Headers(dict):
    def get(self, key: str, default: Any = None) -> Any:  # type: ignore[override]
        for name, value in self.items():
            if name.lower() == key.lower():
                return value
        return default


class _Conn:
    """Connection stub: configurable peer and (for mutations) request headers."""

    def __init__(self, *, remote: tuple[str, int], request_headers: dict[str, str] | None):
        self.remote_address = remote
        if request_headers is not None:
            self.request = _FakeReq(request_headers)

    def respond(self, status: int, text: str) -> Any:
        return (status, text)


class _FakeReq:
    def __init__(self, headers: dict[str, str]):
        self.headers = headers


_LOCAL = ("127.0.0.1", 41000)


def _ws_config() -> WebSocketConfig:
    return WebSocketConfig.model_validate(
        {
            "enabled": True,
            "allowFrom": ["*"],
            "host": "127.0.0.1",
            "port": 18999,
            "path": "/ws",
            "websocketRequiresToken": False,
            "tokenIssueSecret": "tenant-issue-secret",
            "sharedRoomsEnabled": True,
        }
    )


def _build(tmp_path: Path, *, allow_install: bool | None) -> WebSocketChannel:
    """Build a gateway whose config file pins ``tools.skills.allow_install``.

    ``allow_install=None`` leaves the field at its schema default (True) so the
    default-path test exercises the value real operators get out of the box.
    """
    sessions = SessionManager(tmp_path)
    sessions.get_or_create("websocket:test")
    sessions.save(sessions.get_or_create("websocket:test"), fsync=True)
    config_path = tmp_path / "config.json"
    seed = Config()
    if allow_install is not None:
        seed.tools.skills.allow_install = allow_install
    save_config(seed, config_path)
    bus = MessageBus()
    gateway = build_gateway_services(
        config=_ws_config(),
        bus=bus,
        session_manager=sessions,
        static_dist_path=None,
        workspace_path=tmp_path,
        config_path=config_path,
        default_restrict_to_workspace=False,
        runtime_model_name=None,
        runtime_surface="browser",
        runtime_capabilities_overrides=None,
    )
    return WebSocketChannel(_ws_config(), bus, gateway=gateway)


async def _install(channel: WebSocketChannel) -> Any:
    """Drive the real browser path: a loopback WS ``skill.install`` mutation."""
    connection = _Conn(
        remote=_LOCAL,
        request_headers={"Host": "127.0.0.1:8765"},
    )
    return await channel.gateway.http.dispatch_webui_mutation(
        connection,
        "skill.install",
        {"source": "acme/agent-skills", "skill": "react-testing"},
    )


async def _search(channel: WebSocketChannel) -> Any:
    token = channel.gateway.tokens.issue_api_token(60)
    headers = _Headers({"Authorization": f"Bearer {token}"})
    request = TransportRequest(
        method="GET",
        path="/api/webui/skills/search?q=ima&provider=skillhub",
        headers=headers,
        body=b"",
        raw_path="/api/webui/skills/search",
    )
    return await channel._dispatch_http(_Conn(remote=_LOCAL, request_headers=None), request)


def _patch_skillhub_search(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Response:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict[str, Any]:
            return {"results": [_SKILLHUB_RESULT]}

    class _Client:
        async def __aenter__(self) -> "_Client":
            return self

        async def __aexit__(self, *_args: object) -> None:
            pass

        async def get(self, _url: str, *, params: dict[str, object]) -> _Response:
            return _Response()

    monkeypatch.setattr(
        "nanobot.webui.skills_marketplace.httpx.AsyncClient",
        lambda **_kwargs: _Client(),
    )
    # skills.sh's npx probe must be irrelevant: SkillHub installs over plain HTTP.
    monkeypatch.setattr(
        "nanobot.webui.skills_marketplace.skills_install_supported",
        lambda: True,
    )


def _body(response: Any) -> dict[str, Any]:
    assert response is not None
    return json.loads(bytes(response.body).decode())


def _text(response: Any) -> str:
    assert response is not None
    return bytes(response.body).decode()


@pytest.mark.asyncio
async def test_install_refused_when_disabled_even_for_local_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A local browser peer still gets 403 when installs are disabled.

    This is the failure the issue names: the request is judged "local", which
    previously skipped the remote-install policy entirely. The gate must fire
    first, and the install provider must never be reached.
    """
    install = AsyncMock(side_effect=AssertionError("install must not run when disabled"))
    monkeypatch.setattr("nanobot.webui.ws_http.install_marketplace_skill", install)
    channel = _build(tmp_path, allow_install=False)

    response = await _install(channel)

    assert response is not None
    assert response.status_code == 403
    assert "skill installation is disabled for this workspace" in _text(response)
    install.assert_not_called()


@pytest.mark.asyncio
async def test_install_allowed_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the gate untouched (schema default), install still works end-to-end."""

    async def _install_provider(
        source: str,
        skill_id: str,
        workspace: Path,
        *,
        provider: str,
        version: str,
    ) -> dict[str, Any]:
        assert source == "acme/agent-skills"
        assert skill_id == "react-testing"
        skill_dir = workspace / "skills" / skill_id
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: react-testing\ndescription: Test React apps.\n---\n",
            encoding="utf-8",
        )
        return {"installed": True, "already_installed": False, "name": skill_id}

    install = AsyncMock(side_effect=_install_provider)
    monkeypatch.setattr("nanobot.webui.ws_http.install_marketplace_skill", install)
    channel = _build(tmp_path, allow_install=None)

    response = await _install(channel)

    assert response is not None
    assert response.status_code == 200
    body = _body(response)
    assert body["last_action"] == {
        "installed": True,
        "already_installed": False,
        "name": "react-testing",
    }
    assert body["install_supported"] is True
    assert any(skill["name"] == "react-testing" for skill in body["skills"])
    install.assert_awaited_once()


@pytest.mark.asyncio
async def test_search_reports_install_unsupported_when_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Search (SkillHub, plain-HTTP provider) reports install_supported=false.

    SkillHub hardcodes ``install_supported: True`` upstream; with the gate off
    both the payload and each row must say false. The negative control (gate on)
    proves the flag tracks the config and is not forced off unconditionally.
    """
    _patch_skillhub_search(monkeypatch)

    disabled = _build(tmp_path, allow_install=False)
    body = _body(await _search(disabled))
    assert body["install_supported"] is False
    assert body["skills"], "expected the stubbed SkillHub result to be returned"
    assert all(skill["install_supported"] is False for skill in body["skills"])

    enabled = _build(tmp_path, allow_install=None)
    control = _body(await _search(enabled))
    assert control["install_supported"] is True
    assert all(skill["install_supported"] is True for skill in control["skills"])
