"""work.finished push for hand-off tasks (MIT-1857 / OA-15).

Covers the design rules (docs/design/onboarding-and-approval-tuning.md §4,
"The push"): a flagged task reaching succeeded/failed/interrupted sends
exactly one authenticated POST to the connectors runtime route; unflagged and
cancelled tasks send nothing; one retry after 30 s on network errors or 5xx;
a second failure returns False without raising; the restart sweep's notices
go out with status ``interrupted`` once the connectors credential is ready.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.agent.tools.mcp_client_credentials import OAuthClientCredentialsError
from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.config.schema import (
    ChannelsConfig,
    MCPServerConfig,
    OAuthClientCredentialsConfig,
    ToolsConfig,
)
from nanobot.work import finish_notify
from nanobot.work.finish_notify import notify_finished
from nanobot.work.store import WorkStore

_SERVER_URL = "https://connectors.invalid/mcp"
_FINISHED_URL = "https://connectors.invalid/runtime/work/finished"
_TOKEN_URL = "https://connectors.invalid/oauth/token"


class RecordedPost:
    def __init__(self, request: httpx.Request) -> None:
        self.method = request.method
        self.url = str(request.url)
        self.authorization = request.headers.get("Authorization")
        self.body = json.loads(request.content.decode("utf-8"))


class PostRecorder:
    """Mock transport for the finished-push POST; scripted responses optional."""

    def __init__(self, responses: list[int] | None = None, error: Exception | None = None) -> None:
        self.posts: list[RecordedPost] = []
        self._responses = list(responses) if responses is not None else []
        self._error = error
        self.token_hits = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        if "/oauth/token" in str(request.url):
            self.token_hits += 1
            return httpx.Response(
                200, json={"access_token": "minted-token", "expires_in": 300}
            )
        self.posts.append(RecordedPost(request))
        if self._error is not None:
            raise self._error
        if self._responses:
            return httpx.Response(self._responses.pop(0), json={})
        return httpx.Response(202, json={"result": "queued"})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


def _oauth_cfg(tmp_path: Path) -> OAuthClientCredentialsConfig:
    secret_file = tmp_path / "client_secret"
    secret_file.write_text("s3cret\n", encoding="utf-8")
    return OAuthClientCredentialsConfig(
        token_url=_TOKEN_URL,
        client_id="runtime-client",
        client_secret_file=str(secret_file),
    )


def _cfg(tmp_path: Path, *, servers: dict[str, MCPServerConfig] | None = None) -> Any:
    if servers is None:
        servers = {
            "ziggy-connectors": MCPServerConfig(
                url=_SERVER_URL, oauth_client_credentials=_oauth_cfg(tmp_path)
            )
        }
    return SimpleNamespace(
        tools=ToolsConfig(mcp_servers=servers),
        channels=ChannelsConfig.model_validate(
            {"websocket": {"sharedRoomConnectorServer": "ziggy-connectors"}}
        ),
    )


class _FixedBearerAuth(httpx.Auth):
    def auth_flow(self, request: httpx.Request):  # type: ignore[no-untyped-def]
        request.headers["Authorization"] = "Bearer test-runtime-bearer"
        yield request


def _install_mock_push(
    monkeypatch: pytest.MonkeyPatch, recorder: PostRecorder
) -> None:
    """Route notify_finished's POST through *recorder* with a fixed bearer."""
    monkeypatch.setattr(
        finish_notify,
        "_auth_for",
        lambda server, token_transport: _FixedBearerAuth(),
    )
    monkeypatch.setattr(
        finish_notify,
        "_push_client",
        lambda server, auth, transport: httpx.AsyncClient(
            auth=auth, transport=recorder.transport(), timeout=httpx.Timeout(5.0)
        ),
    )


def _provider() -> Any:
    from unittest.mock import MagicMock

    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = SimpleNamespace(max_tokens=4096)
    return provider


def _make_loop(tmp_path: Path, cfg: Any) -> AgentLoop:
    loop = AgentLoop(
        bus=MessageBus(),
        provider=_provider(),
        workspace=tmp_path,
        model="test-model",
        memory_index_enabled=False,
        tools_config=cfg.tools,
        channels_config=cfg.channels,
    )
    return loop


def _work_message(task_id: str) -> InboundMessage:
    return InboundMessage(
        channel="websocket",
        sender_id="client-1",
        chat_id="chat-1",
        content="keep going",
        metadata={"work_task_id": task_id},
    )


async def _drain(loop: AgentLoop) -> None:
    tasks = list(loop._finish_notify_tasks)
    if tasks:
        await asyncio.gather(*tasks)


# ---------------------------------------------------------------------------
# notify_finished unit behaviour (httpx mock transports)
# ---------------------------------------------------------------------------


async def test_posts_bearer_and_exact_body(tmp_path: Path) -> None:
    recorder = PostRecorder()
    token_recorder = PostRecorder()
    cfg = _cfg(tmp_path)

    ok = await notify_finished(
        cfg,
        "work_" + "a" * 32,
        "Fix my calendar",
        "succeeded",
        transport=recorder.transport(),
        token_transport=token_recorder.transport(),
    )

    assert ok is True
    assert len(recorder.posts) == 1
    post = recorder.posts[0]
    assert post.url == _FINISHED_URL
    # The bearer is minted through the client-credentials helper (its token
    # endpoint was hit exactly once) and travels on the POST.
    assert token_recorder.token_hits == 1
    assert post.authorization == "Bearer minted-token"
    assert post.body == {
        "task_id": "work_" + "a" * 32,
        "title": "Fix my calendar",
        "status": "succeeded",
    }


async def test_retries_once_after_30s_on_5xx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = PostRecorder(responses=[503, 202])
    slept: list[float] = []

    async def fake_sleep(delay: float) -> None:
        slept.append(delay)

    monkeypatch.setattr(finish_notify, "_sleep", fake_sleep)

    ok = await notify_finished(
        _cfg(tmp_path),
        "work_" + "b" * 32,
        "Summarise the thread",
        "failed",
        transport=recorder.transport(),
        token_transport=recorder.transport(),
    )

    assert ok is True
    assert len(recorder.posts) == 2
    assert slept == [30.0]


async def test_network_error_twice_returns_false_without_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = PostRecorder(error=httpx.ConnectError("connectors down"))
    slept: list[float] = []

    async def fake_sleep(delay: float) -> None:
        slept.append(delay)

    monkeypatch.setattr(finish_notify, "_sleep", fake_sleep)

    ok = await notify_finished(
        _cfg(tmp_path),
        "work_" + "c" * 32,
        "Book the room",
        "interrupted",
        transport=recorder.transport(),
        token_transport=recorder.transport(),
    )

    assert ok is False
    # Initial attempt plus the single retry, then give up -- no exception.
    assert len(recorder.posts) == 2
    assert slept == [30.0]


async def test_4xx_does_not_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = PostRecorder(responses=[400])
    slept: list[float] = []

    async def fake_sleep(delay: float) -> None:
        slept.append(delay)

    monkeypatch.setattr(finish_notify, "_sleep", fake_sleep)

    ok = await notify_finished(
        _cfg(tmp_path),
        "work_" + "d" * 32,
        "Bad payload",
        "succeeded",
        transport=recorder.transport(),
        token_transport=recorder.transport(),
    )

    assert ok is False
    assert len(recorder.posts) == 1
    assert slept == []


async def test_missing_server_config_returns_false(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, servers={})
    ok = await notify_finished(cfg, "work_" + "e" * 32, "Orphan", "succeeded")
    assert ok is False


async def test_non_push_status_returns_false(tmp_path: Path) -> None:
    recorder = PostRecorder()
    ok = await notify_finished(
        _cfg(tmp_path),
        "work_" + "f" * 32,
        "Cancelled by tester",
        "cancelled",
        transport=recorder.transport(),
        token_transport=recorder.transport(),
    )
    assert ok is False
    assert recorder.posts == []


# ---------------------------------------------------------------------------
# record_work_status wiring through the agent loop
# ---------------------------------------------------------------------------


async def test_flagged_task_succeeded_sends_one_post(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = PostRecorder()
    _install_mock_push(monkeypatch, recorder)
    cfg = _cfg(tmp_path)
    loop = _make_loop(tmp_path, cfg)
    loop.work_store = WorkStore(tmp_path, reconcile_on_open=False)
    task = loop.work_store.create_task(
        chat_id="chat-1", content="keep going", title="Fix my calendar", notify_on_finish=True
    )

    await loop.record_work_status(_work_message(task["task_id"]), "succeeded")
    await _drain(loop)

    assert len(recorder.posts) == 1
    post = recorder.posts[0]
    assert post.url == _FINISHED_URL
    assert post.authorization == "Bearer test-runtime-bearer"
    assert post.body == {
        "task_id": task["task_id"],
        "title": "Fix my calendar",
        "status": "succeeded",
    }


async def test_unflagged_task_sends_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = PostRecorder()
    _install_mock_push(monkeypatch, recorder)
    cfg = _cfg(tmp_path)
    loop = _make_loop(tmp_path, cfg)
    loop.work_store = WorkStore(tmp_path, reconcile_on_open=False)
    task = loop.work_store.create_task(chat_id="chat-1", content="quiet work")

    await loop.record_work_status(_work_message(task["task_id"]), "succeeded")
    await _drain(loop)

    assert recorder.posts == []


async def test_cancelled_sends_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = PostRecorder()
    _install_mock_push(monkeypatch, recorder)
    cfg = _cfg(tmp_path)
    loop = _make_loop(tmp_path, cfg)
    loop.work_store = WorkStore(tmp_path, reconcile_on_open=False)
    task = loop.work_store.create_task(
        chat_id="chat-1", content="stop me", notify_on_finish=True
    )

    await loop.record_work_status(_work_message(task["task_id"]), "cancelled")
    await _drain(loop)

    assert recorder.posts == []


# ---------------------------------------------------------------------------
# Restart sweep -> startup notices
# ---------------------------------------------------------------------------


async def test_restart_sweep_notices_post_interrupted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = PostRecorder()
    _install_mock_push(monkeypatch, recorder)

    # Same sequence the gateway runs: open without the sweep, then call it
    # explicitly before channels accept input (reconcile_work_store).
    store = WorkStore(tmp_path, reconcile_on_open=False)
    flagged = store.create_task(
        chat_id="chat-1", content="long job", title="Tidy the drive", notify_on_finish=True
    )
    store.update_status(flagged["task_id"], "running")
    quiet = store.create_task(chat_id="chat-1", content="quiet job")
    store.update_status(quiet["task_id"], "running")

    # The sweep marks both running tasks interrupted and returns the count,
    # while the flagged one is parked for the startup hook's push.
    assert store.reconcile_interrupted() == 2
    assert store.get_task(quiet["task_id"])["status"] == "interrupted"

    cfg = _cfg(tmp_path)
    loop = _make_loop(tmp_path, cfg)
    loop.work_store = store
    sent = await loop.send_interrupted_finish_notices()
    await _drain(loop)

    # Exactly the flagged task, with status interrupted.
    assert sent == 1
    assert len(recorder.posts) == 1
    assert recorder.posts[0].body == {
        "task_id": flagged["task_id"],
        "title": "Tidy the drive",
        "status": "interrupted",
    }
    # Consumed in one go: a later reconnect must not re-push.
    assert store.pending_finish_notices() == []


def test_sweep_parks_only_flagged_tasks(tmp_path: Path) -> None:
    store = WorkStore(tmp_path, reconcile_on_open=False)
    flagged = store.create_task(
        chat_id="chat-1", content="x", title="One more push", notify_on_finish=True
    )
    store.update_status(flagged["task_id"], "running")
    quiet = store.create_task(chat_id="chat-1", content="y")
    store.update_status(quiet["task_id"], "queued")

    assert store.pending_finish_notices() == []  # nothing before the sweep
    assert store.reconcile_interrupted() == 2
    assert store.pending_finish_notices() == [(flagged["task_id"], "One more push")]


async def test_token_endpoint_failure_is_logged_not_raised(tmp_path: Path) -> None:
    """A connectors outage on the token endpoint returns False, never raises."""
    recorder = PostRecorder()

    def failing_token(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="nope")

    from nanobot.agent.tools.mcp_client_credentials import OAuthClientCredentialsAuth

    auth = OAuthClientCredentialsAuth(
        _oauth_cfg(tmp_path),
        _SERVER_URL,
        token_client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(failing_token), timeout=httpx.Timeout(5.0)
        ),
    )
    with pytest.raises(OAuthClientCredentialsError):
        # Sanity: the helper itself does raise -- notify_finished must not.
        await auth._token()

    ok = await notify_finished(
        _cfg(tmp_path),
        "work_" + "0" * 32,
        "Connectors offline",
        "failed",
        transport=recorder.transport(),
        token_transport=httpx.MockTransport(failing_token),
    )
    assert ok is False
    assert recorder.posts == []
