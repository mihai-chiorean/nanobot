"""The Work event stream over the gateway socket (Ziggy-local, MIT-1010).

These pin the wire contract that ``services/ziggy-work``'s River executor runs
a task over (``internal/executor/executor.go:66-250``), the web client
(``web/src/lib/nanobot-client.ts:242-353``) and the iOS Work views
(``ios/Ziggy/Core/Models/WebSocketModels.swift:546-636``) already speak.  Field
names are asserted literally on purpose: a rename here is a silent client break,
because ziggy-work's frame decoder drops unknown fields rather than erroring.

The authorization tests are the point of the file.  Work rows span sessions
while a shared room grants exactly one, so a room guest reaching ``work.*``
would turn the stream into a cross-session read channel.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from nanobot.bus.queue import MessageBus
from nanobot.channels.websocket.runtime import WebSocketChannel, WebSocketConfig
from nanobot.channels.websocket.transport import TransportRequest
from nanobot.channels.websocket.work_stream import (
    WORK_ENVELOPE_TYPES,
    WorkStreamHub,
    work_event_fields,
)
from nanobot.session.manager import SessionManager
from nanobot.webui.gateway_services import build_gateway_services
from nanobot.webui.work_http import WorkRouter
from nanobot.work.store import WorkStore

SECRET = "tenant-issue-secret"
API_TOKEN = "owner-api-token"
OWNER_CHAT = "chat_owner"
ROOM_CHAT = "11111111-2222-3333-4444-555555555555"
ROOM_ID = "room_" + "a" * 32
CHAT_ID = "99999999-8888-7777-6666-555555555555"


# --------------------------------------------------------------------------
# Hub-level harness: a fake transport, the real WorkStore
# --------------------------------------------------------------------------


class _Connection:
    remote_address = ("127.0.0.1", 41000)

    def __init__(self, label: str = "c") -> None:
        self.label = label

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<conn {self.label}>"


class _Transport:
    """The slice of ``WebSocketChannel`` the hub consumes."""

    name = "websocket"
    runtime_model_name = "test-model"

    def __init__(self) -> None:
        self.events: list[tuple[Any, dict[str, Any]]] = []
        self.raw: list[tuple[Any, dict[str, Any]]] = []
        self.attached: list[tuple[Any, str]] = []
        self.attachment_result: tuple[list[str], str | None] = ([], None)

    async def webui_send_event(self, connection: Any, event: str, **fields: Any) -> None:
        self.events.append((connection, {"event": event, **fields}))

    async def webui_send_raw(self, connection: Any, raw: str, *, label: str = "") -> None:
        self.raw.append((connection, json.loads(raw)))

    def webui_attach(self, connection: Any, chat_id: str) -> None:
        self.attached.append((connection, chat_id))

    def store_work_attachments(self, media: list[Any]) -> tuple[list[str], str | None]:
        return self.attachment_result

    def room_turn_metadata(self, connection: Any, chat_id: str) -> dict[str, Any]:
        return {}

    # -- assertions helpers -------------------------------------------------

    def frames(self, connection: Any | None = None) -> list[dict[str, Any]]:
        """Every frame, event- and raw-delivered, in emission order."""
        combined = self.events + self.raw
        return [
            payload
            for conn, payload in combined
            if connection is None or conn is connection
        ]


class _Bus:
    def __init__(self) -> None:
        self.inbound: list[Any] = []

    async def publish_inbound(self, msg: Any) -> None:
        self.inbound.append(msg)


@pytest.fixture
def store(tmp_path: Path) -> WorkStore:
    return WorkStore(tmp_path)


@pytest.fixture
def transport() -> _Transport:
    return _Transport()


@pytest.fixture
def bus() -> _Bus:
    return _Bus()


@pytest.fixture
def hub(transport: _Transport, store: WorkStore, bus: _Bus) -> WorkStreamHub:
    return WorkStreamHub(transport=transport, store=store, bus=bus)


async def _create(hub: WorkStreamHub, connection: Any, **overrides: Any) -> str:
    envelope: dict[str, Any] = {
        "type": "work.create",
        "chat_id": CHAT_ID,
        "content": "summarize the inbox",
    }
    envelope.update(overrides)
    await hub.dispatch(connection, "client-1", envelope)
    transport = hub._transport  # pyright: ignore[reportPrivateUsage]
    created = [f for f in transport.frames() if f["event"] == "work.created"]
    assert created, "work.create produced no work.created"
    return str(created[-1]["task_id"])


# --------------------------------------------------------------------------
# Event round-trip, one test per event type
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_work_create_answers_work_created_with_the_full_task_row(
    hub: WorkStreamHub,
    transport: _Transport,
    bus: _Bus,
) -> None:
    connection = _Connection()
    task_id = await _create(hub, connection, title="Inbox digest")

    frame = transport.frames()[-1]
    assert frame["event"] == "work.created"
    assert frame["task_id"] == task_id
    task = frame["task"]
    # ziggy-work reads task.task_id (executor.go:234); web renders the rest.
    assert task["task_id"] == task_id
    assert task["chat_id"] == CHAT_ID
    assert task["title"] == "Inbox digest"
    assert task["status"] == "queued"
    assert task["mode"] == "background"
    assert task["model"] == "test-model"
    assert task["reasoning_profile"] == "auto"
    assert "session_key" in task and "last_seq" in task
    # Bookkeeping that is not part of the wire payload must not leak.
    assert "_was_created" not in task
    assert "_was_dispatched" not in task

    # The turn is enqueued against the task's own durable session.
    assert len(bus.inbound) == 1
    inbound = bus.inbound[0]
    assert inbound.session_key_override == task["session_key"]
    assert inbound.chat_id == CHAT_ID
    assert inbound.metadata["work_task_id"] == task_id
    assert inbound.metadata["work_mode"] == "background"
    assert inbound.metadata["_wants_stream"] is True


@pytest.mark.asyncio
async def test_work_subscribe_answers_work_subscribed_then_replays_events(
    hub: WorkStreamHub,
    transport: _Transport,
    store: WorkStore,
) -> None:
    creator = _Connection("creator")
    task_id = await _create(hub, creator)
    store.append_event(task_id, "tool.started", {"name": "gmail_search"}, actor="main_agent")

    watcher = _Connection("watcher")
    transport.events.clear()
    transport.raw.clear()
    await hub.dispatch(watcher, "client-2", {"type": "work.subscribe", "task_id": task_id})

    frames = transport.frames(watcher)
    assert frames[0] == {"event": "work.subscribed", "task_id": task_id}
    replayed = [f for f in frames if f["event"] == "work.event"]
    # task.created from create_task, then the tool.started appended above.
    assert [f["type"] for f in replayed] == ["task.created", "tool.started"]
    assert [f["seq"] for f in replayed] == [1, 2]


@pytest.mark.asyncio
async def test_work_event_carries_every_field_the_clients_decode(
    hub: WorkStreamHub,
    transport: _Transport,
    store: WorkStore,
) -> None:
    connection = _Connection()
    task_id = await _create(hub, connection)
    hub.attach(connection, task_id)
    transport.events.clear()
    transport.raw.clear()

    event = store.append_event(
        task_id,
        "status.changed",
        {"status": "running"},
        actor="main_agent",
    )
    await hub.broadcast_event(event)

    frame = transport.frames(connection)[-1]
    assert set(frame) == {
        "event",
        "task_id",
        "seq",
        "type",
        "payload",
        "actor",
        "step_id",
        "created_at",
    }
    assert frame["event"] == "work.event"
    assert frame["task_id"] == task_id
    assert frame["type"] == "status.changed"
    assert frame["payload"] == {"status": "running"}
    assert frame["actor"] == "main_agent"
    assert isinstance(frame["seq"], int)
    assert isinstance(frame["created_at"], str)


@pytest.mark.asyncio
async def test_a_subscribe_after_seq_resumes_instead_of_replaying_everything(
    hub: WorkStreamHub,
    transport: _Transport,
    store: WorkStore,
) -> None:
    connection = _Connection()
    task_id = await _create(hub, connection)
    for name in ("a", "b", "c"):
        store.append_event(task_id, "tool.started", {"name": name}, actor="main_agent")
    transport.events.clear()
    transport.raw.clear()

    await hub.dispatch(
        connection,
        "client-1",
        {"type": "work.subscribe", "task_id": task_id, "after_seq": 2},
    )
    replayed = [f for f in transport.frames() if f["event"] == "work.event"]
    assert [f["seq"] for f in replayed] == [3, 4]


@pytest.mark.asyncio
async def test_an_idempotent_recreate_replays_rather_than_enqueuing_twice(
    hub: WorkStreamHub,
    transport: _Transport,
    bus: _Bus,
    store: WorkStore,
) -> None:
    """ziggy-work retries work.create with its own task id as the key."""
    key = "work_" + "1" * 32
    connection = _Connection()
    task_id = await _create(hub, connection, idempotency_key=key)
    assert len(bus.inbound) == 1
    transport.events.clear()
    transport.raw.clear()

    await hub.dispatch(
        connection,
        "client-1",
        {
            "type": "work.create",
            "chat_id": CHAT_ID,
            "content": "summarize the inbox",
            "idempotency_key": key,
        },
    )
    frames = transport.frames()
    assert frames[0]["event"] == "work.created"
    assert frames[0]["task_id"] == task_id
    # The retry must not start a second turn.
    assert len(bus.inbound) == 1
    # It replays instead, so the client can resume from a cold socket.
    assert [f["type"] for f in frames if f["event"] == "work.event"] == ["task.created"]
    assert store.get_task(task_id) is not None


# --------------------------------------------------------------------------
# Subscription lifecycle
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subscribe_receive_unsubscribe(
    hub: WorkStreamHub,
    transport: _Transport,
    store: WorkStore,
) -> None:
    creator = _Connection("creator")
    task_id = await _create(hub, creator)
    watcher = _Connection("watcher")
    await hub.dispatch(watcher, "client-2", {"type": "work.subscribe", "task_id": task_id})
    assert set(hub.subscribers(task_id)) == {creator, watcher}

    transport.events.clear()
    transport.raw.clear()
    await hub.broadcast_event(store.append_event(task_id, "tool.started", {"name": "x"}))
    assert len(transport.frames(watcher)) == 1

    # Disconnect is the only unsubscribe: the snapshot has no work.unsubscribe
    # frame, so a closing socket must drop out of every task it watched.
    hub.detach_connection(watcher)
    assert hub.subscribers(task_id) == (creator,)

    transport.events.clear()
    transport.raw.clear()
    await hub.broadcast_event(store.append_event(task_id, "tool.finished", {"name": "x"}))
    assert transport.frames(watcher) == []
    assert len(transport.frames(creator)) == 1


@pytest.mark.asyncio
async def test_detaching_the_last_watcher_frees_the_task_entry(
    hub: WorkStreamHub,
) -> None:
    connection = _Connection()
    task_id = await _create(hub, connection)
    hub.detach_connection(connection)
    assert hub.subscribers(task_id) == ()
    # No empty set left behind: a long-lived gateway must not accumulate one
    # per task it ever served.
    assert hub._work_subs == {}  # pyright: ignore[reportPrivateUsage]
    assert hub._conn_work == {}  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_broadcast_reaches_only_the_subscribers_of_that_task(
    hub: WorkStreamHub,
    transport: _Transport,
    store: WorkStore,
) -> None:
    first = _Connection("first")
    second = _Connection("second")
    first_task = await _create(hub, first)
    second_task = await _create(hub, second)
    hub.detach_connection(first)
    hub.attach(first, first_task)
    transport.events.clear()
    transport.raw.clear()

    await hub.broadcast_event(store.append_event(second_task, "tool.started", {"name": "x"}))
    assert transport.frames(first) == []
    assert len(transport.frames(second)) == 1


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("envelope", "detail"),
    [
        ({"type": "work.create", "chat_id": "not a chat", "content": "x"}, "invalid chat_id"),
        ({"type": "work.create", "chat_id": CHAT_ID, "content": "  "}, "missing content"),
        (
            {
                "type": "work.create",
                "chat_id": CHAT_ID,
                "content": "x",
                "reasoning_profile": "turbo",
            },
            "invalid reasoning_profile",
        ),
        (
            {"type": "work.create", "chat_id": CHAT_ID, "content": "x", "idempotency_key": "nope"},
            "invalid idempotency key",
        ),
        ({"type": "work.subscribe", "task_id": "nope"}, "task not found"),
        ({"type": "work.subscribe", "task_id": "work_" + "0" * 32}, "task not found"),
        ({"type": "work.cancel", "task_id": "work_" + "0" * 32}, "task not found"),
        ({"type": "work.message", "task_id": "work_" + "0" * 32, "content": "x"}, "task not found"),
    ],
)
async def test_malformed_frames_are_refused_without_touching_the_store(
    hub: WorkStreamHub,
    transport: _Transport,
    bus: _Bus,
    envelope: dict[str, Any],
    detail: str,
) -> None:
    await hub.dispatch(_Connection(), "client-1", envelope)
    frames = transport.frames()
    assert [f["event"] for f in frames] == ["error"]
    assert frames[0]["detail"] == detail
    assert bus.inbound == []


@pytest.mark.asyncio
async def test_a_terminal_task_refuses_messages_and_cancellation(
    hub: WorkStreamHub,
    transport: _Transport,
    store: WorkStore,
    bus: _Bus,
) -> None:
    connection = _Connection()
    task_id = await _create(hub, connection)
    store.update_status(task_id, "succeeded", result_summary="done")
    transport.events.clear()
    transport.raw.clear()
    bus.inbound.clear()

    await hub.dispatch(
        connection, "client-1", {"type": "work.message", "task_id": task_id, "content": "more"}
    )
    await hub.dispatch(connection, "client-1", {"type": "work.cancel", "task_id": task_id})
    assert [f["detail"] for f in transport.frames()] == [
        "task does not accept messages",
        "task already complete",
    ]
    assert bus.inbound == []


@pytest.mark.asyncio
async def test_cancel_is_recorded_as_cancelled_even_when_the_loop_wins_the_race(
    hub: WorkStreamHub,
    store: WorkStore,
    bus: _Bus,
) -> None:
    """Regression (PR #58 review, P2-2): ``/stop`` closes the task out.

    The loop's command short-circuit writes ``succeeded`` for any command,
    ``/stop`` included.  If ``cancel_task`` signalled first and the loop got
    there before the ``cancelled`` write, the terminal guard dropped the cancel
    and the Work app showed a cancelled task as having succeeded.  The bus here
    plays the loop and closes the row out the moment ``/stop`` is published.
    """
    connection = _Connection()
    task_id = await _create(hub, connection)
    bus.inbound.clear()

    async def _loop_closes_out_on_stop(msg: Any) -> None:
        bus.inbound.append(msg)
        store.update_status(task_id, "succeeded", result_summary="stopped")

    bus.publish_inbound = _loop_closes_out_on_stop  # type: ignore[method-assign]

    task = store.get_task(task_id)
    assert task is not None
    assert await hub.cancel_task(task, sender_id="websocket") is None
    assert [m.content for m in bus.inbound] == ["/stop"]
    current = store.get_task(task_id)
    assert current is not None
    assert current["status"] == "cancelled"


def test_work_event_fields_tolerates_a_plain_dict() -> None:
    """The loop hands the channel an already-projected dict over the bus."""
    projected = work_event_fields(
        {"task_id": "work_" + "2" * 32, "seq": 4, "type": "tool.finished"}
    )
    assert projected["payload"] == {}
    assert projected["actor"] is None
    assert projected["step_id"] is None


# --------------------------------------------------------------------------
# HTTP routes
# --------------------------------------------------------------------------


class _Headers(dict):
    def get(self, key: str, default: Any = None) -> Any:  # type: ignore[override]
        for name, value in self.items():
            if name.lower() == key.lower():
                return value
        return default


def _request(path: str, *, method: str = "GET", token: str | None = API_TOKEN) -> TransportRequest:
    headers = _Headers()
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return TransportRequest(method=method, path=path, headers=headers, body=b"", raw_path=path)


@pytest.fixture
def router(hub: WorkStreamHub) -> WorkRouter:
    def check(request: Any) -> bool:
        return request.headers.get("Authorization") == f"Bearer {API_TOKEN}"

    return WorkRouter(hub=hub, check_api_token=check)


def _body(response: Any) -> Any:
    return json.loads(bytes(response.body).decode())


@pytest.mark.asyncio
async def test_the_list_route_pages_the_way_the_reconciler_walks_it(
    hub: WorkStreamHub,
    router: WorkRouter,
) -> None:
    """``reconcile.go:77`` pages with order=task_id + after_task_id."""
    for _ in range(3):
        await _create(hub, _Connection())
    response = await router.dispatch(_request("/api/work?limit=2&order=task_id"), "/api/work")
    assert response is not None
    body = _body(response)
    assert len(body["tasks"]) == 2
    assert body["has_more"] is True
    assert body["next_offset"] == 2
    assert body["next_task_id"] == body["tasks"][-1]["task_id"]

    nxt = await router.dispatch(
        _request(f"/api/work?limit=2&order=task_id&after_task_id={body['next_task_id']}"),
        "/api/work",
    )
    assert nxt is not None
    rest = _body(nxt)
    assert len(rest["tasks"]) == 1
    assert rest["has_more"] is False


@pytest.mark.asyncio
async def test_the_detail_route_returns_events_and_artifact_urls(
    hub: WorkStreamHub,
    router: WorkRouter,
    store: WorkStore,
    tmp_path: Path,
) -> None:
    connection = _Connection()
    task_id = await _create(hub, connection)
    store.add_artifact(task_id, name="digest.md", kind="markdown", content="# hi")

    response = await router.dispatch(_request(f"/api/work/{task_id}"), f"/api/work/{task_id}")
    assert response is not None
    task = _body(response)["task"]
    assert task["task_id"] == task_id
    artifact = task["artifacts"][0]
    assert artifact["name"] == "digest.md"
    assert artifact["url"] == f"/api/work/artifacts/{artifact['artifact_id']}"


@pytest.mark.asyncio
async def test_the_events_route_pages_on_seq(
    hub: WorkStreamHub,
    router: WorkRouter,
    store: WorkStore,
) -> None:
    task_id = await _create(hub, _Connection())
    for name in ("a", "b"):
        store.append_event(task_id, "tool.started", {"name": name})

    path = f"/api/work/{task_id}/events?after_seq=1&limit=1"
    response = await router.dispatch(_request(path), f"/api/work/{task_id}/events")
    assert response is not None
    body = _body(response)
    assert [e["seq"] for e in body["events"]] == [2]
    assert body["has_more"] is True
    assert body["next_after_seq"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/api/work",
        "/api/work/work_" + "0" * 32,
        "/api/work/work_" + "0" * 32 + "/events",
        "/api/work/artifacts/artifact_" + "0" * 32,
    ],
)
async def test_every_work_route_requires_the_owner_api_token(
    router: WorkRouter,
    path: str,
) -> None:
    response = await router.dispatch(_request(path, token=None), path)
    assert response is not None
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_an_unrelated_path_is_not_claimed_by_the_work_router(
    router: WorkRouter,
) -> None:
    assert await router.dispatch(_request("/api/sessions"), "/api/sessions") is None
    assert await router.dispatch(_request("/api/workspaces"), "/api/workspaces") is None


# --------------------------------------------------------------------------
# Authorization: a shared-room guest must not reach Work
# --------------------------------------------------------------------------


def _config(**kw: Any) -> WebSocketConfig:
    payload: dict[str, Any] = {
        "enabled": True,
        "allowFrom": ["*"],
        "host": "127.0.0.1",
        "port": 18998,
        "path": "/ws",
        "websocketRequiresToken": False,
        "tokenIssueSecret": SECRET,
        "sharedRoomsEnabled": True,
    }
    payload.update(kw)
    return WebSocketConfig.model_validate(payload)


@pytest.fixture
def channel(tmp_path: Path) -> WebSocketChannel:
    sessions = SessionManager(tmp_path)
    session = sessions.get_or_create(f"websocket:{OWNER_CHAT}")
    session.add_message("user", "private question")
    sessions.save(session, fsync=True)
    config = _config()
    bus = MessageBus()
    gateway = build_gateway_services(
        config=config,
        bus=bus,
        session_manager=sessions,
        static_dist_path=None,
        workspace_path=tmp_path,
        default_restrict_to_workspace=False,
        runtime_model_name=None,
        runtime_surface="browser",
        runtime_capabilities_overrides=None,
    )
    return WebSocketChannel(config, bus, gateway=gateway)


class _GuestConnection:
    remote_address = ("127.0.0.1", 41001)

    def respond(self, status: int, text: str) -> Any:
        return (status, text)


async def _room_guest(channel: WebSocketChannel) -> Any:
    assert channel.gateway.http.shared_rooms is not None
    await channel.gateway.http.shared_rooms.create_room(
        TransportRequest(
            method="POST",
            path="/auth/shared-rooms",
            headers=_Headers({"Authorization": f"Bearer {SECRET}"}),
            body=json.dumps(
                {
                    "source_session_key": f"websocket:{OWNER_CHAT}",
                    "chat_id": ROOM_CHAT,
                    "room_id": ROOM_ID,
                    "title": "Shared conversation",
                    "owner_display_name": "Mihai",
                }
            ).encode(),
            raw_path="/auth/shared-rooms",
        )
    )
    assert channel.rooms is not None
    token, _ = channel.rooms.mint(
        room_id=ROOM_ID,
        chat_id=ROOM_CHAT,
        participant_id="participant_" + "e" * 32,
        display_name="Guest",
        role="contributor",
    )
    connection = _GuestConnection()
    assert (
        channel.gateway.endpoint.authorize_websocket_handshake(
            connection, {"token": [token]}, None
        )
        is None
    )
    return connection


@pytest.mark.asyncio
@pytest.mark.parametrize("command_type", sorted(WORK_ENVELOPE_TYPES))
async def test_a_room_guest_cannot_reach_the_work_stream(
    channel: WebSocketChannel,
    command_type: str,
) -> None:
    """The negative case this port exists to avoid.

    A Work task is keyed by task, not by session, so a guest that could
    subscribe would read telemetry -- tool arguments, step titles, artifact
    names -- from the owner's private sessions.  The refusal must come from the
    allow-list in ``WebUICommandRouter.dispatch``, not from a Work-specific
    check that could drift out of step with it.
    """
    connection = await _room_guest(channel)
    sent: list[dict[str, Any]] = []

    async def _capture(conn: Any, event: str, **fields: Any) -> None:
        sent.append({"event": event, **fields})

    channel.webui_send_event = _capture  # type: ignore[assignment]
    assert channel.work is not None
    before = len(channel.work.subscribers("work_" + "0" * 32))

    await channel._commands.dispatch(  # pyright: ignore[reportPrivateUsage]
        connection,
        "guest-1",
        {
            "type": command_type,
            "chat_id": ROOM_CHAT,
            "content": "read the owner's work",
            "task_id": "work_" + "0" * 32,
        },
    )

    assert sent, f"{command_type} produced no rejection"
    assert sent[0]["event"] == "error"
    assert sent[0]["detail"] == "room scope violation"
    assert len(channel.work.subscribers("work_" + "0" * 32)) == before


@pytest.mark.asyncio
async def test_a_room_bearer_is_not_an_api_token_for_the_work_routes(
    channel: WebSocketChannel,
) -> None:
    """The REST half of the same boundary.

    ``/api/sessions/<key>/messages`` deliberately accepts an ``nbrt_`` bearer
    for the one session it names.  ``/api/work*`` must not: the rows are not
    scoped to a session at all.
    """
    await _room_guest(channel)
    assert channel.rooms is not None
    room_token, _ = channel.rooms.mint(
        room_id=ROOM_ID,
        chat_id=ROOM_CHAT,
        participant_id="participant_" + "f" * 32,
        display_name="Guest",
        role="contributor",
    )
    assert room_token.startswith("nbrt_")
    assert channel.gateway.http.work is not None

    request = TransportRequest(
        method="GET",
        path="/api/work",
        headers=_Headers({"Authorization": f"Bearer {room_token}"}),
        body=b"",
        raw_path="/api/work",
    )
    response = await channel.gateway.http.work.dispatch(request, "/api/work")
    assert response is not None
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_a_trusted_proxy_request_is_not_an_api_token_for_the_work_routes(
    channel: WebSocketChannel,
) -> None:
    """Regression (PR #58 review, P2-3).

    ``WebUIHTTPRouter.check_api_token`` returns True for any request a trusted
    proxy vouched for, and ``dispatch`` stamps that flag from the peer address
    and an assertion header alone.  Once ziggy-control fronts the tenant with
    ``trustedProxyAuth``, a room guest arrives exactly that way, so the Work
    routes must demand the owner API token itself.
    """
    work = channel.gateway.http.work
    assert work is not None

    proxied = TransportRequest(
        method="GET",
        path="/api/work",
        headers=_Headers(),
        body=b"",
        raw_path="/api/work",
    )
    setattr(proxied, "_nanobot_trusted_proxy_authenticated", True)
    response = await work.dispatch(proxied, "/api/work")
    assert response is not None
    assert response.status_code == 401

    # Non-vacuity: a real owner API token still reaches the route.
    token = channel.gateway.http.tokens.issue_api_token(60)
    owner = TransportRequest(
        method="GET",
        path="/api/work",
        headers=_Headers({"Authorization": f"Bearer {token}"}),
        body=b"",
        raw_path="/api/work",
    )
    response = await work.dispatch(owner, "/api/work")
    assert response is not None
    assert response.status_code == 200


def _issue_channel(tmp_path: Path) -> WebSocketChannel:
    config = _config(tokenIssuePath="/auth/token")
    bus = MessageBus()
    gateway = build_gateway_services(
        config=config,
        bus=bus,
        session_manager=SessionManager(tmp_path),
        static_dist_path=None,
        workspace_path=tmp_path,
        default_restrict_to_workspace=False,
        runtime_model_name=None,
        runtime_surface="browser",
        runtime_capabilities_overrides=None,
    )
    return WebSocketChannel(config, bus, gateway=gateway)


async def _get(channel: WebSocketChannel, path: str, token: str | None) -> Any:
    headers = _Headers()
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    request = TransportRequest(
        method="GET", path=path, headers=headers, body=b"", raw_path=path
    )
    response = await channel.gateway.http.dispatch(_GuestConnection(), request)
    assert response is not None
    return response


@pytest.mark.asyncio
async def test_the_reconciler_token_from_auth_token_reaches_the_work_routes(
    tmp_path: Path,
) -> None:
    """Regression (PR #58 review, High).

    ziggy-work's reconciler (``executor.go`` ``token`` + ``reconcile.go``) calls
    ``GET /auth/token`` with the tenant issue secret and sends the returned
    ``token`` as the Bearer for ``/api/work``, ``/api/work/<id>/events`` and
    artifact downloads.  The 0.2.x runtime wrote that value into both the
    WebSocket and API pools; 0.3.0 must too, or every reconcile is a 401.
    """
    channel = _issue_channel(tmp_path)
    issued = await _get(channel, "/auth/token", SECRET)
    assert issued.status_code == 200
    token = _body(issued)["token"]

    listed = await _get(channel, "/api/work", token)
    assert listed.status_code == 200
    # Reusable for the whole reconcile pass, not single-use like the WS copy.
    assert (await _get(channel, "/api/work", token)).status_code == 200

    # The WS copy is still there and still single-use.
    tokens = channel.gateway.http.tokens
    assert tokens.take_issued_token_audience(token) == "client"
    assert tokens.take_issued_token_audience(token) is None
    assert (await _get(channel, "/api/work", token)).status_code == 200


@pytest.mark.asyncio
async def test_unknown_or_expired_tokens_are_refused_on_the_work_routes(
    tmp_path: Path,
) -> None:
    channel = _issue_channel(tmp_path)
    assert (await _get(channel, "/api/work", "nbwt_unknown")).status_code == 401

    token = _body(await _get(channel, "/auth/token", SECRET))["token"]
    tokens = channel.gateway.http.tokens
    tokens.api_tokens[token] = 0.0  # monotonic clock is long past zero
    assert (await _get(channel, "/api/work", token)).status_code == 401
    assert token not in tokens.api_tokens


@pytest.mark.asyncio
async def test_auth_token_still_requires_the_issue_secret(tmp_path: Path) -> None:
    channel = _issue_channel(tmp_path)
    response = await _get(channel, "/auth/token", "wrong-secret")
    status = response[0] if isinstance(response, tuple) else response.status_code
    assert status == 401
    assert not channel.gateway.http.tokens.api_tokens


@pytest.mark.asyncio
async def test_auth_token_refuses_when_the_api_pool_is_full(tmp_path: Path) -> None:
    """Same cap as 0.2.x: 429 when either pool holds the maximum."""
    channel = _issue_channel(tmp_path)
    tokens = channel.gateway.http.tokens
    tokens.max_tokens = 2
    tokens.issue_api_token(60)
    tokens.issue_api_token(60)
    response = await _get(channel, "/auth/token", SECRET)
    assert response.status_code == 429
    assert not tokens.issued_tokens


@pytest.mark.asyncio
async def test_a_proxied_request_without_a_token_is_refused_on_the_work_routes(
    tmp_path: Path,
) -> None:
    """d4219f57 stays: the trusted-proxy shortcut does not open /api/work,
    even once /auth/token feeds the API pool."""
    channel = _issue_channel(tmp_path)
    await _get(channel, "/auth/token", SECRET)
    request = TransportRequest(
        method="GET", path="/api/work", headers=_Headers(), body=b"", raw_path="/api/work"
    )
    setattr(request, "_nanobot_trusted_proxy_authenticated", True)
    work = channel.gateway.http.work
    assert work is not None
    response = await work.dispatch(request, "/api/work")
    assert response is not None
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_the_owner_socket_still_reaches_the_work_stream(
    channel: WebSocketChannel,
) -> None:
    """Non-vacuity: the guest refusal above is about the credential, not about
    work.* being unroutable."""
    connection = _GuestConnection()
    sent: list[dict[str, Any]] = []

    async def _capture(conn: Any, event: str, **fields: Any) -> None:
        sent.append({"event": event, **fields})

    channel.webui_send_event = _capture  # type: ignore[assignment]
    await channel._commands.dispatch(  # pyright: ignore[reportPrivateUsage]
        connection,
        "owner-1",
        {"type": "work.create", "chat_id": CHAT_ID, "content": "summarize the inbox"},
    )
    assert [f["event"] for f in sent] == ["work.created"]


@pytest.mark.asyncio
async def test_the_channels_store_does_not_sweep_a_live_task(
    channel: WebSocketChannel,
    tmp_path: Path,
) -> None:
    """Regression: two WorkStores, one restart sweep.

    The schema is brought up lazily, and ``reconcile_interrupted`` rides along
    with it.  The channel opens a second handle on the same database, so if it
    reconciled too, the first ``/api/work`` request after a task started would
    mark that live task ``interrupted`` and the client would see the run die.
    """
    writer = WorkStore(tmp_path)
    task_id = str(writer.create_task(chat_id=CHAT_ID, content="long job")["task_id"])
    writer.update_status(task_id, "running")

    assert channel.work is not None
    # First touch of the channel's handle: this is what brings its schema up.
    assert channel.work.store.get_task(task_id) is not None
    assert writer.get_task(task_id)["status"] == "running"


@pytest.mark.asyncio
async def test_the_work_routes_answer_through_the_real_gateway_dispatch(
    channel: WebSocketChannel,
) -> None:
    """The seam, not just the router.

    ``GatewayHTTPHandler.dispatch`` stamps trusted-proxy state onto the request
    before any route sees it and runs the shared-room router first, so the
    router passing in isolation does not prove ``/api/work`` is reachable --
    or that a room bearer is refused once the real token store is in play.
    """

    class _Peer:
        remote_address = ("8.8.8.8", 5000)

        def respond(self, status: int, text: str) -> Any:
            return (status, text)

    def _get(token: str) -> TransportRequest:
        return TransportRequest(
            method="GET",
            path="/api/work",
            headers=_Headers({"Authorization": f"Bearer {token}"}),
            body=b"",
            raw_path="/api/work",
        )

    denied = await channel.gateway.http.dispatch(_Peer(), _get("nbrt_deadbeef"))
    assert denied is not None
    assert denied.status_code == 401

    api_token = channel.gateway.tokens.issue_api_token(300)
    if isinstance(api_token, tuple):
        api_token = api_token[0]
    allowed = await channel.gateway.http.dispatch(_Peer(), _get(str(api_token)))
    assert allowed is not None
    assert allowed.status_code == 200
    assert _body(allowed) == {
        "tasks": [],
        "has_more": False,
        "next_offset": 0,
        "next_task_id": None,
    }
