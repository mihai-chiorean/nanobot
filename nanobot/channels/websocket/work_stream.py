"""The Work event stream over the gateway socket (Ziggy-local, MIT-1010).

The snapshot served this from the 3267-line ``channels/websocket.py``.  0.3.0
split that file, so the subscription table, the four inbound envelopes and the
fan-out live here and ``webui/inbound_commands.py`` delegates to them.

Why this is not the Automations surface: see ``REBASE-WORK-APP.md``.  In short,
Automations manages cron jobs and triggers, while these frames carry per-task
telemetry -- steps, tool calls, artifacts, a ``seq`` cursor -- for a unit of
work that need not be scheduled at all.  ``services/ziggy-work``'s River
executor *runs* a task over these frames, so the contract is load-bearing for
execution and not only for rendering.

Wire contract (unchanged from the snapshot, pinned by
``tests/channels/test_work_event_stream.py``):

* ``work.create``    -> ``work.created`` ``{task_id, task}``
* ``work.subscribe`` -> ``work.subscribed`` ``{task_id}`` then replayed
  ``work.event`` frames after ``after_seq``
* ``work.event``     ``{task_id, seq, type, payload, actor, step_id, created_at}``
* ``work.cancel`` / ``work.message`` -- idempotency-keyed, ``ACTIVE_STATUSES`` gated
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any
from urllib.parse import unquote

from loguru import logger

from nanobot.bus.events import InboundMessage
from nanobot.webui.session_identity import is_valid_webui_chat_id
from nanobot.work.store import ACTIVE_STATUSES, MAX_EVENT_PAGE, WorkStore

WORK_ID_RE = re.compile(r"^work_[0-9a-f]{32}$")
COMMAND_ID_RE = re.compile(r"^cmd_[0-9a-f]{32}$")
ARTIFACT_ID_RE = re.compile(r"^artifact_[0-9a-f]{32}$")

WORK_ENVELOPE_TYPES = frozenset(
    {"work.create", "work.subscribe", "work.cancel", "work.message"}
)

# ``nanobot/agent/reasoning_policy.py`` is a separate fork carry-forward. The
# wire values are validated here so the contract holds and the column is
# populated; the profile has no behavioural effect until that module lands.
REASONING_PROFILES = frozenset({"auto", "fast", "think", "think-code"})
DEFAULT_REASONING_PROFILE = "auto"


def decode_id(raw_value: str, pattern: re.Pattern[str]) -> str | None:
    """Percent-decode a path segment and validate it against *pattern*."""
    try:
        value = unquote(raw_value)
    except Exception:
        return None
    return value if pattern.fullmatch(value) is not None else None


def parse_reasoning_profile(value: Any) -> str | None:
    """Return a validated profile name, or ``None`` for unsupported input."""
    if not isinstance(value, str):
        return None
    candidate = value.strip().lower()
    return candidate if candidate in REASONING_PROFILES else None


def work_event_fields(event: Any) -> dict[str, Any]:
    """Project a ``WorkEvent`` (or its dict form) onto the wire fields."""
    data = event.to_api() if hasattr(event, "to_api") else event
    if not isinstance(data, dict):
        return {}
    return {
        "task_id": data.get("task_id"),
        "seq": data.get("seq"),
        "type": data.get("type"),
        "payload": data.get("payload") or {},
        "actor": data.get("actor"),
        "step_id": data.get("step_id"),
        "created_at": data.get("created_at"),
    }


def artifact_url(artifact_id: str) -> str:
    return f"/api/work/artifacts/{artifact_id}"


def artifact_media_urls(artifacts: Any) -> list[dict[str, str]]:
    """Wire URLs for the artifact refs stamped on an outbound Work reply."""
    if not isinstance(artifacts, list):
        return []
    urls: list[dict[str, str]] = []
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            continue
        item_id = artifact.get("artifact_id")
        if not isinstance(item_id, str) or ARTIFACT_ID_RE.fullmatch(item_id) is None:
            continue
        item = {"url": artifact_url(item_id)}
        name = artifact.get("name")
        if isinstance(name, str) and name:
            item["name"] = name
        urls.append(item)
    return urls


def augment_artifact_urls(task: dict[str, Any]) -> None:
    """Add a fetchable ``url`` to each artifact on a task snapshot, in place."""
    artifacts = task.get("artifacts")
    if not isinstance(artifacts, list):
        return
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            continue
        item_id = artifact.get("artifact_id")
        if isinstance(item_id, str) and ARTIFACT_ID_RE.fullmatch(item_id):
            artifact["url"] = artifact_url(item_id)


class WorkStreamHub:
    """Own Work subscriptions and the ``work.*`` frames for one channel.

    Held by :class:`~nanobot.channels.websocket.runtime.WebSocketChannel`; the
    transport supplies connection send/attach primitives so this class never
    touches a raw socket.
    """

    def __init__(self, *, transport: Any, store: WorkStore, bus: Any) -> None:
        self._transport = transport
        self._store = store
        self._bus = bus
        # task_id -> connections subscribed to it (fan-out target).
        self._work_subs: dict[str, set[Any]] = {}
        # connection -> task_ids it is subscribed to (O(1) cleanup on close).
        self._conn_work: dict[Any, set[str]] = {}

    @property
    def store(self) -> WorkStore:
        return self._store

    @property
    def runtime_model_name(self) -> str:
        """The model name stamped on new tasks, for the Work app's task card."""
        return str(getattr(self._transport, "runtime_model_name", "") or "")

    # -- subscription bookkeeping ------------------------------------------

    def attach(self, connection: Any, task_id: str) -> None:
        self._work_subs.setdefault(task_id, set()).add(connection)
        self._conn_work.setdefault(connection, set()).add(task_id)

    def detach_connection(self, connection: Any) -> None:
        for task_id in self._conn_work.pop(connection, set()):
            subscribers = self._work_subs.get(task_id)
            if subscribers is None:
                continue
            subscribers.discard(connection)
            if not subscribers:
                self._work_subs.pop(task_id, None)

    def subscribers(self, task_id: str) -> tuple[Any, ...]:
        return tuple(self._work_subs.get(task_id, ()))

    def clear(self) -> None:
        self._work_subs.clear()
        self._conn_work.clear()

    # -- outbound ----------------------------------------------------------

    async def send_event(self, connection: Any, event: Any) -> None:
        await self._transport.webui_send_event(
            connection,
            "work.event",
            **work_event_fields(event),
        )

    async def broadcast_event(self, event: Any) -> None:
        """Fan one event out to every subscriber of its task."""
        fields = work_event_fields(event)
        task_id = fields.get("task_id")
        if not isinstance(task_id, str):
            return
        connections = self.subscribers(task_id)
        if not connections:
            return
        raw = json.dumps({"event": "work.event", **fields}, ensure_ascii=False)
        for connection in connections:
            await self._transport.webui_send_raw(connection, raw, label=" work ")

    async def replay(self, connection: Any, task_id: str, *, after_seq: int) -> None:
        cursor = after_seq
        while True:
            events = await self._store.run_io(
                self._store.list_events,
                task_id,
                after_seq=cursor,
                limit=MAX_EVENT_PAGE,
            )
            for event in events:
                await self.send_event(connection, event)
            if len(events) < MAX_EVENT_PAGE:
                return
            cursor = int(events[-1]["seq"])
            # Yield between pages so a long backlog cannot starve the loop.
            await asyncio.sleep(0)

    async def _error(self, connection: Any, detail: str, **fields: Any) -> None:
        await self._transport.webui_send_event(connection, "error", detail=detail, **fields)

    async def _reject_room_media(
        self,
        connection: Any,
        chat_id: str,
        raw_media: Any,
    ) -> bool:
        """Refuse attachments bound for a shared room (MIT-1399).

        Same rule and wording as ``_dispatch_message``: a file sent into a room
        is served to every guest. ``room_turn_metadata`` is non-empty for any
        room chat, revoked or expired ones included, so this fails closed.
        """
        if not raw_media or not self._transport.room_turn_metadata(connection, chat_id):
            return False
        await self._error(
            connection,
            "attachment_rejected",
            message="Attachments are not available in shared rooms yet.",
            chat_id=chat_id,
        )
        return True

    # -- inbound envelopes -------------------------------------------------

    async def dispatch(
        self,
        connection: Any,
        client_id: str,
        envelope: dict[str, Any],
    ) -> None:
        command_type = envelope.get("type")
        if command_type == "work.create":
            await self.handle_create(connection, client_id, envelope)
        elif command_type == "work.subscribe":
            await self.handle_subscribe(connection, envelope)
        elif command_type == "work.cancel":
            await self.handle_cancel(connection, envelope)
        elif command_type == "work.message":
            await self.handle_message(connection, client_id, envelope)

    async def handle_create(
        self,
        connection: Any,
        client_id: str,
        envelope: dict[str, Any],
    ) -> None:
        chat_id = envelope.get("chat_id")
        content = envelope.get("content")
        request_id = envelope.get("idempotency_key")
        if not is_valid_webui_chat_id(chat_id):
            await self._error(connection, "invalid chat_id")
            return
        if not isinstance(content, str) or not content.strip():
            await self._error(connection, "missing content")
            return
        profile = parse_reasoning_profile(
            envelope.get("reasoning_profile", DEFAULT_REASONING_PROFILE)
        )
        if profile is None:
            await self._error(connection, "invalid reasoning_profile")
            return
        if request_id is not None and (
            not isinstance(request_id, str) or WORK_ID_RE.fullmatch(request_id) is None
        ):
            await self._error(connection, "invalid idempotency key")
            return
        if await self._reject_room_media(connection, str(chat_id), envelope.get("media")):
            return
        media_paths, media_error = self._envelope_media(envelope.get("media"))
        if media_error is not None:
            await self._error(
                connection,
                "attachment_rejected",
                reason=media_error,
            )
            return
        title = envelope.get("title")
        task = await self._store.run_io(
            self._store.create_task,
            chat_id=str(chat_id),
            content=content,
            mode="background",
            title=title if isinstance(title, str) else None,
            model=self.runtime_model_name,
            reasoning_profile=profile,
            request_id=request_id,
        )
        was_created = bool(task.pop("_was_created", True))
        was_dispatched = bool(task.pop("_was_dispatched", False))
        task_id = str(task["task_id"])
        # The creating socket is attached to both the chat and the task so it
        # receives the reply and the event stream without a second round trip.
        self._transport.webui_attach(connection, str(chat_id))
        self.attach(connection, task_id)
        await self._transport.webui_send_event(
            connection,
            "work.created",
            task_id=task_id,
            task=task,
        )
        if not was_created:
            # Idempotent replay: the caller retried with a known key. Only a
            # task confirmed dispatched is replayed -- an unconfirmed one is
            # either still being enqueued by the first attempt or lost its
            # mark_dispatched write, and re-enqueuing on a guess would run the
            # turn twice. The client has the task row from work.created and can
            # subscribe explicitly.
            if was_dispatched:
                await self.replay(connection, task_id, after_seq=0)
            return
        try:
            await self.publish_work_inbound(
                task,
                sender_id=client_id,
                content=content,
                media=media_paths,
                remote=getattr(connection, "remote_address", None),
            )
        except Exception:
            logger.exception("failed to enqueue WebSocket Work task {}", task_id)
            await self.fail_enqueue(task_id)
            await self._error(connection, "failed to enqueue work", task_id=task_id)
            return
        if request_id is not None:
            # Bookkeeping only, and it runs *after* the enqueue succeeded: the
            # task is already running, so failing the task here would be a lie.
            # Letting it raise would instead kill the socket over an UPDATE, so
            # it is logged. The cost of losing it is that an idempotent retry
            # re-sends work.created without replaying the backlog; the turn
            # still runs exactly once, which is the property that matters.
            try:
                await self._store.run_io(self._store.mark_dispatched, task_id, request_id)
            except Exception:
                logger.exception("failed to mark Work task {} dispatched", task_id)

    async def handle_subscribe(self, connection: Any, envelope: dict[str, Any]) -> None:
        task_id = envelope.get("task_id")
        if not isinstance(task_id, str) or WORK_ID_RE.fullmatch(task_id) is None:
            await self._error(connection, "task not found")
            return
        if await self._store.run_io(self._store.get_task, task_id) is None:
            await self._error(connection, "task not found")
            return
        try:
            after_seq = max(0, int(envelope.get("after_seq") or 0))
        except (TypeError, ValueError):
            await self._error(connection, "invalid after_seq")
            return
        self.attach(connection, task_id)
        await self._transport.webui_send_event(connection, "work.subscribed", task_id=task_id)
        await self.replay(connection, task_id, after_seq=after_seq)

    async def handle_cancel(self, connection: Any, envelope: dict[str, Any]) -> None:
        task_id = envelope.get("task_id")
        if not isinstance(task_id, str) or WORK_ID_RE.fullmatch(task_id) is None:
            await self._error(connection, "task not found")
            return
        task = await self._store.run_io(self._store.get_task, task_id)
        if task is None:
            await self._error(connection, "task not found")
            return
        self.attach(connection, task_id)
        error = await self.cancel_task(task, sender_id="websocket")
        if error == "terminal":
            await self._error(connection, "task already complete")
            return
        if error is not None:
            await self._error(connection, "failed to signal cancellation")

    async def handle_message(
        self,
        connection: Any,
        client_id: str,
        envelope: dict[str, Any],
    ) -> None:
        task_id = envelope.get("task_id")
        content = envelope.get("content")
        command_id = envelope.get("idempotency_key")
        if not isinstance(task_id, str) or WORK_ID_RE.fullmatch(task_id) is None:
            await self._error(connection, "task not found")
            return
        task = await self._store.run_io(self._store.get_task, task_id)
        if task is None:
            await self._error(connection, "task not found")
            return
        if task.get("status") not in ACTIVE_STATUSES:
            await self._error(connection, "task does not accept messages")
            return
        if not isinstance(content, str) or not content.strip():
            await self._error(connection, "missing content")
            return
        if command_id is not None and (
            not isinstance(command_id, str) or COMMAND_ID_RE.fullmatch(command_id) is None
        ):
            await self._error(connection, "invalid idempotency key")
            return
        if not is_valid_webui_chat_id(task.get("chat_id")):
            await self._error(connection, "invalid task chat")
            return
        if await self._reject_room_media(
            connection, str(task.get("chat_id")), envelope.get("media"),
        ):
            return
        self.attach(connection, task_id)
        if command_id is not None:
            try:
                reserved, _ = await self._store.run_io(
                    self._store.reserve_command,
                    command_id,
                    task_id,
                    "message",
                )
            except ValueError:
                await self._error(connection, "invalid idempotency key")
                return
            if not reserved:
                # A duplicate retry of a command already in flight: silence is
                # the contract, the first attempt still owns the delivery.
                return
        try:
            await self.publish_work_inbound(
                task,
                sender_id=client_id,
                content=content,
                remote=getattr(connection, "remote_address", None),
            )
        except Exception:
            logger.exception("failed to enqueue message for Work task {}", task_id)
            if command_id is not None:
                try:
                    await self._store.run_io(
                        self._store.release_command,
                        command_id,
                        task_id,
                        "message",
                    )
                except Exception:
                    logger.exception("failed to release Work command {}", command_id)
            await self._error(connection, "failed to enqueue work message")
            return
        await self.record_message(task_id, content)
        if command_id is not None:
            # Same reasoning as mark_dispatched in handle_create: the message is
            # already enqueued, and letting a bookkeeping write raise here tears
            # down the whole connection (``_connection_loop`` has no per-frame
            # handler), losing every chat and Work subscription on it.
            try:
                await self._store.run_io(self._store.mark_command_dispatched, command_id)
            except Exception:
                logger.exception("failed to mark Work command {} dispatched", command_id)

    # -- shared task operations --------------------------------------------

    async def publish_work_inbound(
        self,
        task: dict[str, Any],
        *,
        sender_id: str,
        content: str,
        media: list[str] | None = None,
        remote: Any = None,
    ) -> None:
        """Enqueue a turn bound to *task*'s own durable session."""
        task_id = str(task["task_id"])
        session_key = str(task.get("session_key") or "")
        chat_id = str(task.get("chat_id") or "")
        if not session_key or not is_valid_webui_chat_id(chat_id):
            raise ValueError("Work task routing is invalid")
        metadata: dict[str, Any] = {
            "_wants_stream": True,
            "work_task_id": task_id,
            "work_mode": "background",
            "reasoning_profile": str(
                task.get("reasoning_profile") or DEFAULT_REASONING_PROFILE
            ),
        }
        if remote is not None:
            metadata["remote"] = remote
        await self._bus.publish_inbound(
            InboundMessage(
                channel=self._transport.name,
                sender_id=sender_id,
                chat_id=chat_id,
                content=content,
                media=media or [],
                metadata=metadata,
                session_key_override=session_key,
            )
        )

    async def fail_enqueue(self, task_id: str) -> None:
        event = await self._store.run_io(
            self._store.update_status,
            task_id,
            "failed",
            error="Failed to enqueue Work task.",
        )
        if event is not None:
            await self.broadcast_event(event)

    async def record_message(self, task_id: str, content: str) -> None:
        event = await self._store.run_io(
            self._store.append_event,
            task_id,
            "message.received",
            {"content": content},
            actor="user",
        )
        if event is not None:
            await self.broadcast_event(event)

    async def cancel_task(self, task: dict[str, Any], *, sender_id: str) -> str | None:
        """Signal cancellation. Returns an error token, or ``None`` on success."""
        if task.get("status") not in ACTIVE_STATUSES:
            return "terminal"
        task_id = str(task["task_id"])
        # Write the terminal row before signalling the loop. "/stop" is a
        # command, and the loop's command short-circuit closes the task out as
        # ``succeeded``; if it got there first, the terminal guard would turn
        # this ``cancelled`` write into a no-op and the cancel would be lost.
        event = await self._store.run_io(self._store.update_status, task_id, "cancelled")
        if event is None:
            # update_status returns None when the row did not move. Another
            # caller winning the race is success, not failure.
            current = await self._store.run_io(self._store.get_task, task_id)
            if current is not None and current.get("status") == "cancelled":
                return None
            return "terminal"
        await self.broadcast_event(event)
        try:
            await self.publish_work_inbound(task, sender_id=sender_id, content="/stop")
        except Exception:
            # The row already reads cancelled; the running turn will finish
            # against a terminal row and its outcome write will no-op.
            logger.exception("failed to signal cancellation for Work task {}", task_id)
            return "publish_failed"
        return None

    def _envelope_media(self, raw_media: Any) -> tuple[list[str], str | None]:
        if raw_media is None:
            return [], None
        if not isinstance(raw_media, list):
            return [], "malformed"
        paths, reason = self._transport.store_work_attachments(raw_media)
        return list(paths), reason
