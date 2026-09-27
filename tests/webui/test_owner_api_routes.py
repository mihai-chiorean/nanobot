"""Owner-gated WebUI HTTP routes restored for 0.3.0 parity (MIT-1431).

Covers the ``GET``/``POST`` route the embedded WebUI calls with an **owner API
token** (bearer) that had gone missing on the 0.3.0 cutover line, so the
browser view 405'd (``/api/settings/update`` -- it was only reachable as a
WebSocket *mutation*, never as a plain HTTP call):

* ``GET|POST /api/settings/update`` -- the Settings save / default-model picker.

The dead 0.2.x compatibility routes ``GET /api/activity`` and
``POST /api/model/switch`` (and ``nanobot/model_runtime.py``) were removed in
MIT-1481: ``/api/activity`` had no live client and ``/api/model/switch``
always answered 503 because ``ZIGGY_MODEL_SWITCH_SCRIPT`` is unset.
``GET /api/activity/audit`` (MIT-1450) stays, covered by
``tests/webui/test_activity_audit_route.py``.

The route must (a) refuse a room credential and a bare trusted-proxy mark
(the owner ``tokens.check_api_token``, not the ``check_api_token`` shortcut --
mirrors the PR #76 rename regression this class of bug was found by), and (b)
serve production's response shape, since the browser decodes these objects
directly. The precise wire contract is pinned to the production handlers at
``origin/feat/shared-rooms`` ``nanobot/channels/websocket.py`` (read via ``git
show``, not guessed).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from nanobot.bus.queue import MessageBus
from nanobot.channels.websocket.rooms import SharedRoomStore
from nanobot.channels.websocket.runtime import WebSocketChannel, WebSocketConfig
from nanobot.channels.websocket.transport import TransportRequest
from nanobot.config.loader import load_config, save_config
from nanobot.config.schema import Config
from nanobot.session.manager import SessionManager
from nanobot.webui.gateway_services import build_gateway_services

OWNER_CHAT = "chat_owner"
ROOM_CHAT = "11111111-2222-3333-4444-555555555555"
ROOM_ID = "room_" + "a" * 32
PARTICIPANT_ID = "participant_" + "b" * 32
PROXY_ASSERTION_HEADER = "X-Ziggy-Proxy-Assertion"


class _Headers(dict):
    """Minimal case-insensitive headers view (real HTTP uses ``Headers``)."""

    def get(self, key: str, default: Any = None) -> Any:  # type: ignore[override]
        for name, value in self.items():
            if name.lower() == key.lower():
                return value
        return default


class _Connection:
    remote_address = ("127.0.0.1", 41000)

    def respond(self, status: int, text: str) -> Any:
        return (status, text)


def _config(*, proxy: bool = False) -> WebSocketConfig:
    data: dict[str, Any] = {
        "enabled": True,
        "allowFrom": ["*"],
        "host": "127.0.0.1",
        "port": 18999,
        "path": "/ws",
        "websocketRequiresToken": False,
        "tokenIssueSecret": "tenant-issue-secret",
        "sharedRoomsEnabled": True,
    }
    if proxy:
        data["trustedProxyAuth"] = {
            "trustedPeerCidrs": ["127.0.0.1/32"],
            "assertionHeader": PROXY_ASSERTION_HEADER,
        }
    return WebSocketConfig.model_validate(data)


def _build(tmp_path: Path, sessions: SessionManager, *, proxy: bool = False) -> Any:
    bus = MessageBus()
    gateway = build_gateway_services(
        config=_config(proxy=proxy),
        bus=bus,
        session_manager=sessions,
        static_dist_path=None,
        workspace_path=tmp_path,
        default_restrict_to_workspace=False,
        runtime_model_name=None,
        runtime_surface="browser",
        runtime_capabilities_overrides=None,
    )
    return WebSocketChannel(_config(proxy=proxy), bus, gateway=gateway)


def _request(
    path: str,
    *,
    method: str = "GET",
    body: Any = None,
    token: str | None = None,
) -> TransportRequest:
    headers = _Headers()
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return TransportRequest(
        method=method,
        path=path,
        headers=headers,
        body=json.dumps(body).encode() if body is not None else b"",
        raw_path=path,
    )


async def _dispatch(channel: Any, request: TransportRequest) -> Any:
    return await channel._dispatch_http(_Connection(), request)


def _body(response: Any) -> dict[str, Any]:
    assert response is not None
    return json.loads(bytes(response.body).decode())


def _text(response: Any) -> str:
    assert response is not None
    return bytes(response.body).decode()


# ---------------------------------------------------------------------------
# GET|POST /api/settings/update
# ---------------------------------------------------------------------------


OWNER_OPENAI_KEY = "sk-owner-pool-secret-DO-NOT-LEAK"
OWNER_ANTHROPIC_KEY = "sk-anthropic-secret-DO-NOT-LEAK"


def _seed_config(config_path: Path) -> None:
    """Write a real on-disk config through the production loader.

    ``update_agent_settings`` (shared with the WebSocket
    ``settings.agent.update`` path) loads/saves via ``load_config``/
    ``save_config`` with no explicit path, so the tests pin those seams to
    *config_path* (same pattern as ``tests/webui/test_settings_api.py``).
    The payload's ``providers`` rows carry only metadata (``has_api_key``
    booleans, hints) -- never the key values, which is why the secrets below
    are safe to seed and are asserted absent from every body.
    """
    seed = Config()
    seed.agents.defaults.model = "openai/gpt-4o"
    seed.agents.defaults.provider = "openai"
    seed.providers.openai.api_key = OWNER_OPENAI_KEY
    seed.providers.anthropic.api_key = OWNER_ANTHROPIC_KEY
    save_config(seed, config_path)


@pytest.fixture
def config_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Isolate the default config path to a temp dir; spy on saves.

    The route now runs the shared ``settings_api.update_agent_settings``
    editor, which resolves the default config path when the caller passes no
    explicit path -- exactly what the production HTTP call does, so the tests
    exercise the same path resolution while keeping every write inside
    *tmp_path*. The save spy wraps the real writer (which the seam patch
    would otherwise replace) so ``save`` counts are observed, not stubbed.
    """
    sessions = SessionManager(tmp_path)
    sessions.get_or_create(f"websocket:{OWNER_CHAT}")
    sessions.save(sessions.get_or_create(f"websocket:{OWNER_CHAT}"), fsync=True)
    channel = _build(tmp_path, sessions)
    token = channel.gateway.http.tokens.issue_api_token(60)

    config_path = tmp_path / "config.json"
    _seed_config(config_path)
    monkeypatch.setattr("nanobot.config.loader._current_config_path", config_path)

    from nanobot.webui import settings_api as settings_api_module

    real_save = settings_api_module.save_config
    calls: list[Any] = []

    def spy_save(config: Any, config_path_arg: Any = None) -> None:
        calls.append(config)
        real_save(config, config_path_arg)

    monkeypatch.setattr(settings_api_module, "save_config", spy_save)
    return channel, token, config_path, calls


@pytest.mark.asyncio
async def test_settings_update_writes_model_and_returns_payload(config_env: Any) -> None:
    """Acceptance (4): owner save -> 200, persisted, client-decodable payload.

    The edit goes through the shared ``update_agent_settings`` editor, so a
    model/provider change alone is ``requires_restart: False`` (the restart
    hint belongs to runtime-section edits) and the payload carries every key
    the web/iOS clients decode: ``agent{model,provider,resolved_provider,
    has_api_key}``, ``providers[{name,label}]``, ``runtime.config_path``.
    Configured API keys stay off the wire -- only ``has_api_key`` booleans are
    serialized.
    """
    channel, token, config_path, calls = config_env
    response = await _dispatch(
        channel,
        _request(
            "/api/settings/update?model=anthropic%2Fclaude-opus-4&provider=anthropic",
            method="POST",
            token=token,
        ),
    )
    assert response.status_code == 200, _text(response)
    payload = _body(response)
    assert payload["agent"]["model"] == "anthropic/claude-opus-4"
    assert payload["agent"]["provider"] == "anthropic"
    assert payload["agent"]["resolved_provider"] == "anthropic"
    assert payload["agent"]["has_api_key"] is True
    assert all({"name" in row and "label" in row for row in payload["providers"]})
    assert payload["runtime"]["config_path"] == str(config_path.expanduser())
    assert "model_runtime" not in payload  # MIT-1481: dead field, never serialized
    assert payload["requires_restart"] is False  # a model change alone needs no restart
    assert len(calls) == 1  # persisted exactly once
    reloaded = load_config(config_path)
    assert reloaded.agents.defaults.model == "anthropic/claude-opus-4"
    assert reloaded.agents.defaults.provider == "anthropic"
    text = _text(response)
    assert OWNER_OPENAI_KEY not in text
    assert OWNER_ANTHROPIC_KEY not in text


@pytest.mark.parametrize("verb", ["GET", "POST"])
@pytest.mark.asyncio
async def test_settings_update_accepts_both_verbs(config_env: Any, verb: str) -> None:
    """Production serves GET and POST alike (delete is folded elsewhere)."""
    channel, token, _config_path, _calls = config_env
    response = await _dispatch(
        channel,
        _request("/api/settings/update?model=openai%2Fgpt-5", method=verb, token=token),
    )
    assert response.status_code == 200, _text(response)
    assert _body(response)["agent"]["model"] == "openai/gpt-5"


@pytest.mark.asyncio
async def test_settings_update_no_change_is_not_saved(config_env: Any) -> None:
    """Negative control: re-saving the current model does not touch save_config."""
    channel, token, _config_path, calls = config_env
    response = await _dispatch(
        channel,
        _request("/api/settings/update?model=openai%2Fgpt-4o", method="POST", token=token),
    )
    assert response.status_code == 200
    assert calls == []  # nothing written
    assert _body(response)["requires_restart"] is False


@pytest.mark.asyncio
async def test_settings_update_empty_model_is_400_and_not_saved(config_env: Any) -> None:
    """The shared editor rejects a blank model (``model is required``)."""
    channel, token, _config_path, calls = config_env
    response = await _dispatch(
        channel, _request("/api/settings/update?model=", method="POST", token=token)
    )
    assert response.status_code == 400
    assert calls == []


@pytest.mark.asyncio
async def test_settings_update_unknown_provider_is_400(config_env: Any) -> None:
    """The shared editor's provider resolution rejects an unknown name."""
    channel, token, config_path, calls = config_env
    response = await _dispatch(
        channel,
        _request(
            "/api/settings/update?model=openai%2Fgpt-4o&provider=__bogus__",
            method="POST",
            token=token,
        ),
    )
    assert response.status_code == 400
    assert calls == []
    # The aborted request must not have persisted the partially-applied edit.
    assert load_config(config_path).agents.defaults.provider == "openai"


@pytest.mark.asyncio
async def test_settings_update_provider_omitted_keeps_explicit(config_env: Any) -> None:
    """Omitting provider must not overwrite an explicit stored provider."""
    channel, token, config_path, calls = config_env
    response = await _dispatch(
        channel,
        _request("/api/settings/update?model=deepseek%2Fdeepseek-chat", method="POST", token=token),
    )
    assert response.status_code == 200
    reloaded = load_config(config_path)
    assert reloaded.agents.defaults.provider == "openai"  # unchanged
    assert reloaded.agents.defaults.model == "deepseek/deepseek-chat"
    assert len(calls) == 1  # saved once for the model change


@pytest.mark.asyncio
async def test_settings_update_auto_updates_pinned_provider(config_env: Any) -> None:
    """An explicit non-auto provider reconciles with provider='auto'."""
    channel, token, config_path, calls = config_env
    pinned = load_config(config_path)
    pinned.agents.defaults.provider = "anthropic"
    save_config(pinned, config_path)
    calls.clear()  # the reseed above used the real writer, not the route
    response = await _dispatch(
        channel,
        _request(
            "/api/settings/update?model=openai%2Fgpt-4o&provider=auto",
            method="POST",
            token=token,
        ),
    )
    assert response.status_code == 200
    assert load_config(config_path).agents.defaults.provider == "auto"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_settings_update_refuses_room_credential(config_env: Any) -> None:
    channel, _token, _config_path, calls = config_env
    store = SharedRoomStore(channel.gateway.session_manager, token_ttl_s=300)
    room_token, _credential = store.mint(
        room_id=ROOM_ID,
        chat_id=ROOM_CHAT,
        participant_id=PARTICIPANT_ID,
        display_name="Guest",
        role="contributor",
    )
    response = await _dispatch(
        channel,
        _request("/api/settings/update?model=x%2Fy", method="POST", token=room_token),
    )
    assert response.status_code == 401
    assert calls == []  # refused before any settings work


@pytest.mark.asyncio
async def test_settings_update_ignores_the_trusted_proxy_shortcut(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AGENTS-mandated control: trusted-proxy mark + no Authorization -> 401.

    ``check_api_token`` would admit this request via the proxy shortcut; the
    owner route must demand the bearer token (owner pool) instead.
    """
    sessions = SessionManager(tmp_path)
    sessions.save(sessions.get_or_create(f"websocket:{OWNER_CHAT}"), fsync=True)
    config_path = tmp_path / "config.json"
    _seed_config(config_path)
    monkeypatch.setattr("nanobot.config.loader._current_config_path", config_path)
    channel = _build(tmp_path, sessions, proxy=True)
    proxied = _request("/api/settings/update?model=x%2Fy", method="POST")
    proxied.headers[PROXY_ASSERTION_HEADER] = "room-guest"
    response = await _dispatch(channel, proxied)
    assert getattr(proxied, "_nanobot_trusted_proxy_authenticated", False) is True
    assert response.status_code == 401
    # Refused outright: the on-disk config still holds the seeded model.
    assert load_config(config_path).agents.defaults.model == "openai/gpt-4o"


@pytest.mark.asyncio
async def test_settings_update_requires_owner_token(config_env: Any) -> None:
    channel, _token, _config_path, calls = config_env
    response = await _dispatch(
        channel, _request("/api/settings/update?model=x%2Fy", method="POST")
    )
    assert response.status_code == 401
    assert _text(response) == "Unauthorized"
    assert calls == []
