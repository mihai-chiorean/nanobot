"""Owner-gated WebUI HTTP routes restored for 0.3.0 parity (MIT-1431).

Covers three ``GET``/``POST`` routes the embedded WebUI calls with an **owner
API token** (bearer) that had gone missing on the 0.3.0 cutover line, so the
browser views 404'd (``/api/activity``, ``/api/model/switch``) or 405'd
(``/api/settings/update`` -- it was only reachable as a WebSocket *mutation*,
never as a plain HTTP call):

* ``GET  /api/activity``          -- the Activity view's per-chat feed.
* ``POST /api/model/switch``      -- the web model switch (qwen <-> minimax).
* ``GET|POST /api/settings/update`` -- the Settings save / default-model picker.

Each route must (a) refuse a room credential and a bare trusted-proxy mark
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
from nanobot.session.manager import SessionManager
from nanobot.webui.gateway_services import build_gateway_services

OWNER_CHAT = "chat_owner"
OTHER_CHAT = "chat_other"
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


@pytest.fixture
def env(tmp_path: Path) -> Any:
    """A gateway wired like the real server, with owner + non-webui sessions.

    Two owner webui conversations (one active-pending, one idle) plus a
    non-webui ``cli:`` session that must never leak into the activity feed.
    """
    sessions = SessionManager(tmp_path)
    pending = sessions.get_or_create(f"websocket:{OWNER_CHAT}")
    pending.add_message("user", "need a plan for my trip")
    pending.add_message("assistant", "Sure", buttons=[["Confirm"]])
    sessions.save(pending, fsync=True)
    idle = sessions.get_or_create(f"websocket:{OTHER_CHAT}")
    idle.add_message("user", "just notes")
    idle.add_message("assistant", "noted")
    sessions.save(idle, fsync=True)
    # A non-webui session: must be filtered out of /api/activity.
    outside = sessions.get_or_create("cli:direct")
    outside.add_message("user", "secret CLI exchange")
    sessions.save(outside, fsync=True)

    channel = _build(tmp_path, sessions)
    token = channel.gateway.http.tokens.issue_api_token(60)
    return channel, sessions, token


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
# GET /api/activity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_activity_lists_owner_webui_sessions_in_production_shape(env: Any) -> None:
    """Acceptance (1): owner GET /api/activity -> 200, production keys.

    Pins the row contract from ``_handle_activity``: only the owner's
    ``websocket:`` chats appear (the ``cli:`` session is filtered), each row
    carries the exact key set, ``live`` defaults False, and the assistant
    button row is ``waiting``.
    """
    channel, _sessions, token = env
    response = await _dispatch(channel, _request("/api/activity", token=token))
    assert response.status_code == 200, _text(response)
    rows = {row["key"]: row for row in _body(response)["activity"]}
    assert set(rows) == {f"websocket:{OWNER_CHAT}", f"websocket:{OTHER_CHAT}"}
    # Non-webui session must not leak.
    assert "cli:direct" not in rows
    for row in rows.values():
        assert set(row) >= {
            "key",
            "chat_id",
            "created_at",
            "updated_at",
            "preview",
            "status",
            "live",
            "message_count",
            "last_role",
            "last_text",
        }
        assert row["live"] is False  # no in-flight turn registered
        assert "path" not in row  # on-disk path never leaks
        assert isinstance(row["message_count"], int)
    pending = rows[f"websocket:{OWNER_CHAT}"]
    assert pending["chat_id"] == OWNER_CHAT
    assert pending["status"] == "waiting"  # assistant with pending buttons
    assert pending["preview"] == "need a plan for my trip"  # first user msg
    assert pending["last_role"] == "assistant"
    assert pending["last_text"] == "Sure"
    assert pending["message_count"] == 2
    idle = rows[f"websocket:{OTHER_CHAT}"]
    assert idle["status"] == "idle"


@pytest.mark.asyncio
async def test_activity_marks_live_and_active_when_a_turn_is_in_flight(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance (1b): an in-flight key -> live True, status 'active'.

    Drives the ``active_keys`` wiring independently of the real turn registry
    by seeding a known key; the negative control (a key not in the set) stays
    non-active, proving ``live`` is carried from the provider and not a
    constant.
    """
    channel, _sessions, token = env
    active_key = f"websocket:{OWNER_CHAT}"
    monkeypatch.setattr(
        channel.gateway.http,
        "_active_webui_session_keys",
        lambda: {active_key},
    )
    response = await _dispatch(channel, _request("/api/activity", token=token))
    assert response.status_code == 200, _text(response)
    rows = {row["key"]: row for row in _body(response)["activity"]}
    assert rows[active_key]["live"] is True
    assert rows[active_key]["status"] == "active"
    # Negative control: the other owner chat is not in the injected set.
    other = f"websocket:{OTHER_CHAT}"
    assert rows[other]["live"] is False
    assert rows[other]["status"] != "active"


@pytest.mark.asyncio
async def test_activity_requires_owner_token(env: Any) -> None:
    """Acceptance (2a): no token / wrong token -> 401 (GET is the only verb)."""
    channel, _, _ = env
    anonymous = await _dispatch(channel, _request("/api/activity"))
    assert anonymous.status_code == 401
    assert _text(anonymous) == "Unauthorized"
    wrong = await _dispatch(
        channel, _request("/api/activity", token="not-a-real-token")
    )
    assert wrong.status_code == 401


@pytest.mark.asyncio
async def test_activity_refuses_room_credential(env: Any) -> None:
    """Acceptance (2b): a room ``nbrt_`` credential is not an owner token."""
    channel, sessions, _ = env
    store = SharedRoomStore(sessions, token_ttl_s=300)
    room_token, _credential = store.mint(
        room_id=ROOM_ID,
        chat_id=ROOM_CHAT,
        participant_id=PARTICIPANT_ID,
        display_name="Guest",
        role="contributor",
    )
    assert room_token.startswith("nbrt_")
    assert store.api_credential(room_token) is not None  # the credential is real
    # ... yet it is refused on this owner-only route.
    assert channel.gateway.http.tokens.check_api_token(
        _request("/api/activity", token=room_token)
    ) is False
    response = await _dispatch(channel, _request("/api/activity", token=room_token))
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_activity_ignores_the_trusted_proxy_shortcut(tmp_path: Path) -> None:
    """Regression (PR #76 class): a bare trusted-proxy mark is not an owner.

    ``check_api_token`` returns True for any request the proxy vouched for; the
    activity route must use the owner pool directly, so a proxied request with
    no Authorization header is refused -- while the owner bearer still works
    through the same proxy (non-vacuity control).
    """
    sessions = SessionManager(tmp_path)
    owner = sessions.get_or_create(f"websocket:{OWNER_CHAT}")
    owner.add_message("user", "plan my trip")
    sessions.save(owner, fsync=True)
    channel = _build(tmp_path, sessions, proxy=True)
    token = channel.gateway.http.tokens.issue_api_token(60)

    proxied = _request("/api/activity")
    proxied.headers[PROXY_ASSERTION_HEADER] = "room-guest"
    response = await _dispatch(channel, proxied)
    assert getattr(proxied, "_nanobot_trusted_proxy_authenticated", False) is True
    assert response.status_code == 401

    as_owner = _request("/api/activity", token=token)
    as_owner.headers[PROXY_ASSERTION_HEADER] = "owner"
    ok = await _dispatch(channel, as_owner)
    assert ok.status_code == 200


@pytest.mark.asyncio
async def test_activity_post_is_method_not_allowed(env: Any) -> None:
    channel, _, token = env
    # GET is the only verb served; a POST must not silently succeed.
    response = await _dispatch(
        channel, _request("/api/activity", method="POST", token=token)
    )
    assert response.status_code == 405


# ---------------------------------------------------------------------------
# POST /api/model/switch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_model_switch_forwards_target_and_returns_status(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance (3): POST target=minimax -> 202, model_runtime status."""
    channel, _, token = env
    recorded: list[tuple[str, bool]] = []

    def fake_switch(target: str, *, force: bool = False) -> dict[str, Any]:
        recorded.append((target, force))
        return {"active_model": target, "status": "switching", "state_path": "/x/state.json"}

    monkeypatch.setattr("nanobot.model_runtime.request_switch", fake_switch)
    response = await _dispatch(
        channel,
        _request("/api/model/switch?target=minimax&force=1", method="POST", token=token),
    )
    assert response.status_code == 202, _text(response)
    assert recorded == [("minimax", True)]
    assert _body(response)["model_runtime"]["active_model"] == "minimax"


@pytest.mark.parametrize("target", ["bogus", "qwenx", ""])
@pytest.mark.asyncio
async def test_model_switch_rejects_invalid_target(
    env: Any, monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    """Negative control: a target outside {qwen,minimax} is a 400, not a switch."""
    channel, _, token = env
    called: list[str] = []
    monkeypatch.setattr(
        "nanobot.model_runtime.request_switch",
        lambda target, *, force=False: called.append(target) or {},
    )
    response = await _dispatch(
        channel,
        _request(f"/api/model/switch?target={target}", method="POST", token=token),
    )
    assert response.status_code == 400
    assert called == []  # never reaches the switch


@pytest.mark.asyncio
async def test_model_switch_maps_busy_to_409(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A concurrent switch (ModelSwitchInProgressError) -> 409, prod's code."""
    from nanobot.model_runtime import ModelSwitchInProgressError

    channel, _, token = env

    def boom(target: str, *, force: bool = False) -> dict[str, Any]:
        raise ModelSwitchInProgressError("a model switch is already in progress")

    monkeypatch.setattr("nanobot.model_runtime.request_switch", boom)
    response = await _dispatch(
        channel, _request("/api/model/switch?target=qwen", method="POST", token=token)
    )
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_model_switch_maps_unavailable_to_503(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nanobot.model_runtime import ModelSwitchUnavailableError

    channel, _, token = env

    def boom(target: str, *, force: bool = False) -> dict[str, Any]:
        raise ModelSwitchUnavailableError("model switching is not enabled")

    monkeypatch.setattr("nanobot.model_runtime.request_switch", boom)
    response = await _dispatch(
        channel, _request("/api/model/switch?target=qwen", method="POST", token=token)
    )
    assert response.status_code == 503


@pytest.mark.asyncio
async def test_model_switch_requires_owner_token(env: Any) -> None:
    channel, _, _ = env
    response = await _dispatch(
        channel, _request("/api/model/switch?target=qwen", method="POST")
    )
    assert response.status_code == 401
    assert _text(response) == "Unauthorized"


@pytest.mark.asyncio
async def test_model_switch_get_is_method_not_allowed(env: Any) -> None:
    channel, _, token = env
    response = await _dispatch(
        channel, _request("/api/model/switch?target=qwen", method="GET", token=token)
    )
    assert response.status_code == 405


# ---------------------------------------------------------------------------
# GET|POST /api/settings/update
# ---------------------------------------------------------------------------


class _Provider:
    api_key = "sk-test-secret-DO-NOT-LEAK"


class _Defaults:
    def __init__(self) -> None:
        self.model = "openai/gpt-4o"
        self.provider = "openai"


class _Agents:
    def __init__(self) -> None:
        self.defaults = _Defaults()


class _ProviderConfig:
    def __init__(self) -> None:
        self.api_key = _Provider.api_key


class _FakeConfig:
    def __init__(self) -> None:
        self.agents = _Agents()

    def get_provider_name(self, model: str | None = None) -> str:
        return "openai"

    def get_provider(self, model: str | None = None) -> _ProviderConfig:
        return _ProviderConfig()


@pytest.fixture
def config_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Isolate config to a temp dir with a fake loader (no global mutation).

    The fake's ``agents.defaults`` is a stable object, so the route's
    read-modify-write is observable across the two load_config calls the
    handler makes (its own + ``_settings_payload``'s).
    """
    sessions = SessionManager(tmp_path)
    sessions.get_or_create(f"websocket:{OWNER_CHAT}")
    sessions.save(sessions.get_or_create(f"websocket:{OWNER_CHAT}"), fsync=True)
    channel = _build(tmp_path, sessions)
    token = channel.gateway.http.tokens.issue_api_token(60)

    fake = _FakeConfig()
    saved: list[_FakeConfig] = []

    def fake_load(_path: Any = None) -> _FakeConfig:
        return fake

    def fake_save(cfg: Any, _path: Any = None) -> None:
        saved.append(cfg)

    monkeypatch.setattr("nanobot.config.loader.load_config", fake_load)
    monkeypatch.setattr("nanobot.config.loader.save_config", fake_save)
    monkeypatch.setattr(
        "nanobot.config.loader.get_config_path", lambda: tmp_path / "config.json"
    )
    # Keep the nested model_runtime status read off any real user file.
    monkeypatch.setattr(
        "nanobot.model_runtime.read_status",
        lambda: {"status": "unknown", "active_model": None, "state_path": "/x"},
    )
    return channel, token, fake, saved


@pytest.mark.asyncio
async def test_settings_update_writes_model_and_returns_payload(config_env: Any) -> None:
    """Acceptance (4): owner save -> 200, defaults.model updated, saved once."""
    channel, token, fake, saved = config_env
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
    assert fake.agents.defaults.model == "anthropic/claude-opus-4"
    assert saved == [fake]  # persisted exactly once
    assert payload["requires_restart"] is True  # a change was made
    assert "model_runtime" in payload


@pytest.mark.parametrize("verb", ["GET", "POST"])
@pytest.mark.asyncio
async def test_settings_update_accepts_both_verbs(config_env: Any, verb: str) -> None:
    """Production serves GET and POST alike (delete is folded elsewhere)."""
    channel, token, _fake, _saved = config_env
    response = await _dispatch(
        channel,
        _request("/api/settings/update?model=openai%2Fgpt-4o", method=verb, token=token),
    )
    assert response.status_code == 200, _text(response)


@pytest.mark.asyncio
async def test_settings_update_no_change_is_not_saved(config_env: Any) -> None:
    """Negative control: re-saving the current model does not touch save_config."""
    channel, token, fake, saved = config_env
    same = fake.agents.defaults.model  # 'openai/gpt-4o'
    response = await _dispatch(
        channel,
        _request(f"/api/settings/update?model={same}", method="POST", token=token),
    )
    assert response.status_code == 200
    assert saved == []  # nothing written
    assert _body(response)["requires_restart"] is False


@pytest.mark.asyncio
async def test_settings_update_empty_model_is_400_and_not_saved(config_env: Any) -> None:
    channel, token, _fake, saved = config_env
    response = await _dispatch(
        channel, _request("/api/settings/update?model=", method="POST", token=token)
    )
    assert response.status_code == 400
    assert saved == []


@pytest.mark.asyncio
async def test_settings_update_unknown_provider_is_400(config_env: Any) -> None:
    channel, token, _fake, saved = config_env
    response = await _dispatch(
        channel,
        _request(
            "/api/settings/update?model=openai%2Fgpt-4o&provider=__bogus__",
            method="POST",
            token=token,
        ),
    )
    assert response.status_code == 400
    assert saved == []


@pytest.mark.asyncio
async def test_settings_update_provider_omitted_keeps_explicit(config_env: Any) -> None:
    """Omitting provider must not overwrite an explicit stored provider."""
    channel, token, fake, saved = config_env
    before = fake.agents.defaults.provider
    response = await _dispatch(
        channel,
        _request("/api/settings/update?model=deepseek%2Fdeepseek-chat", method="POST", token=token),
    )
    assert response.status_code == 200
    assert fake.agents.defaults.provider == before  # unchanged
    assert len(saved) == 1  # saved once for the model change


@pytest.mark.asyncio
async def test_settings_update_auto_updates_pinned_provider(config_env: Any) -> None:
    """An explicit non-auto provider reconciles with provider='auto'."""
    channel, token, fake, saved = config_env
    fake.agents.defaults.provider = "openai"
    response = await _dispatch(
        channel,
        _request(
            "/api/settings/update?model=openai%2Fgpt-4o&provider=auto",
            method="POST",
            token=token,
        ),
    )
    assert response.status_code == 200
    assert fake.agents.defaults.provider == "auto"
    assert len(saved) == 1


@pytest.mark.asyncio
async def test_settings_update_refuses_room_credential(config_env: Any) -> None:
    channel, _token, _fake, _saved = config_env
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
    fake = _FakeConfig()
    monkeypatch.setattr("nanobot.config.loader.load_config", lambda _p=None: fake)
    monkeypatch.setattr("nanobot.config.loader.save_config", lambda *_a: None)
    monkeypatch.setattr(
        "nanobot.config.loader.get_config_path", lambda: tmp_path / "config.json"
    )
    monkeypatch.setattr(
        "nanobot.model_runtime.read_status",
        lambda: {"status": "unknown", "active_model": None, "state_path": "/x"},
    )
    channel = _build(tmp_path, sessions, proxy=True)
    proxied = _request("/api/settings/update?model=x%2Fy", method="POST")
    proxied.headers[PROXY_ASSERTION_HEADER] = "room-guest"
    response = await _dispatch(channel, proxied)
    assert getattr(proxied, "_nanobot_trusted_proxy_authenticated", False) is True
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_settings_update_requires_owner_token(config_env: Any) -> None:
    channel, _token, _fake, _saved = config_env
    response = await _dispatch(
        channel, _request("/api/settings/update?model=x%2Fy", method="POST")
    )
    assert response.status_code == 401
    assert _text(response) == "Unauthorized"
