"""Work REST routes (Ziggy-local, MIT-1010).

The snapshot served these from ``channels/websocket.py``; 0.3.0 put HTTP in
``webui/ws_http.py``, so they live here and ``WebUIHTTPRouter._dispatch_resolved``
delegates to them.  The route set and response envelopes are the snapshot's,
unchanged, because ``services/ziggy-work``'s reconciler pages
``GET /api/work?limit=200&order=task_id&after_task_id=`` and then reads
``GET /api/work/<id>`` per task (``internal/executor/reconcile.go:77,113``).

Authorization is the owner API token only.  A shared-room ``nbrt_`` bearer is
never accepted here: rooms are a per-session grant, and Work rows span
sessions, so honouring a room credential would turn this into a cross-session
read channel for a guest.
"""

from __future__ import annotations

import re
from typing import Any

from loguru import logger
from websockets.http11 import Request as WsRequest
from websockets.http11 import Response

from nanobot.channels.websocket.transport import TransportFileResponse
from nanobot.channels.websocket.work_stream import (
    ARTIFACT_ID_RE,
    DEFAULT_REASONING_PROFILE,
    WORK_ID_RE,
    WorkStreamHub,
    augment_artifact_urls,
    decode_id,
    parse_reasoning_profile,
)
from nanobot.webui.http_utils import (
    http_error,
    http_json_response,
    parse_query,
    query_first,
)
from nanobot.webui.session_identity import is_valid_webui_chat_id
from nanobot.webui.shared_rooms_http import request_json
from nanobot.work.store import ACTIVE_STATUSES, MAX_EVENT_PAGE

DEFAULT_LIST_LIMIT = 50
MAX_LIST_LIMIT = 200


def _header_safe_filename(name: Any) -> str:
    """A filename safe to interpolate into ``Content-Disposition``.

    ``safe_filename`` strips path and quote characters but not control ones,
    and an artifact name originates with the agent -- which routinely handles
    untrusted text. Quoting alone is therefore not enough: a CR/LF would be a
    response-splitting primitive if the transport ever stopped validating
    header values for us.
    """
    if not isinstance(name, str):
        return "artifact"
    cleaned = "".join(ch for ch in name if ch.isprintable() and ch != '"')
    return cleaned.strip() or "artifact"


class WorkRouter:
    """Serve ``/api/work*`` against the gateway's :class:`WorkStreamHub`."""

    def __init__(self, *, hub: WorkStreamHub, check_api_token: Any) -> None:
        self.hub = hub
        self._check_api_token = check_api_token

    # -- dispatch -----------------------------------------------------------

    async def dispatch(self, request: WsRequest, path: str) -> Response | Any | None:
        if path != "/api/work" and not path.startswith("/api/work/"):
            return None
        method = getattr(request, "method", "GET")

        if path == "/api/work":
            if method == "GET":
                return await self.list_tasks(request)
            if method == "POST":
                return await self.create_task(request)
            return http_error(405, "Method Not Allowed")

        match = re.fullmatch(r"/api/work/artifacts/([^/]+)", path)
        if match:
            if method != "GET":
                return http_error(405, "Method Not Allowed")
            return await self.artifact(request, match.group(1))

        match = re.fullmatch(r"/api/work/([^/]+)/events", path)
        if match:
            if method != "GET":
                return http_error(405, "Method Not Allowed")
            return await self.events(request, match.group(1))

        match = re.fullmatch(r"/api/work/([^/]+)/cancel", path)
        if match:
            if method != "POST":
                return http_error(405, "Method Not Allowed")
            return await self.cancel(request, match.group(1))

        match = re.fullmatch(r"/api/work/([^/]+)/message", path)
        if match:
            if method != "POST":
                return http_error(405, "Method Not Allowed")
            return await self.message(request, match.group(1))

        match = re.fullmatch(r"/api/work/([^/]+)", path)
        if match:
            if method != "GET":
                return http_error(405, "Method Not Allowed")
            return await self.detail(request, match.group(1))
        return None

    # -- guards -------------------------------------------------------------

    def _guard(self, request: WsRequest) -> Response | None:
        if not self._check_api_token(request):
            return http_error(401, "Unauthorized")
        return None

    async def _task_or_error(
        self,
        raw_task_id: str,
    ) -> tuple[str, dict[str, Any]] | Response:
        task_id = decode_id(raw_task_id, WORK_ID_RE)
        if task_id is None:
            return http_error(400, "invalid task id")
        store = self.hub.store
        task = await store.run_io(store.get_task, task_id)
        if task is None:
            return http_error(404, "task not found")
        return task_id, task

    # -- routes -------------------------------------------------------------

    async def list_tasks(self, request: WsRequest) -> Response:
        guard = self._guard(request)
        if guard is not None:
            return guard
        query = parse_query(request.path)
        status = query_first(query, "status")
        try:
            limit = int(query_first(query, "limit") or DEFAULT_LIST_LIMIT)
        except ValueError:
            limit = DEFAULT_LIST_LIMIT
        limit = max(1, min(limit, MAX_LIST_LIMIT))
        try:
            offset = max(0, int(query_first(query, "offset") or "0"))
        except ValueError:
            return http_error(400, "offset must be an integer")
        order_by_task_id = query_first(query, "order") == "task_id"
        after_task_id = query_first(query, "after_task_id")
        if after_task_id is not None and WORK_ID_RE.fullmatch(after_task_id) is None:
            return http_error(400, "after_task_id must be a Work task id")
        store = self.hub.store
        tasks = await store.run_io(
            store.list_tasks,
            status=status,
            limit=limit,
            offset=offset,
            after_task_id=after_task_id,
            order_by_task_id=order_by_task_id,
        )
        return http_json_response(
            {
                "tasks": tasks,
                "has_more": len(tasks) == limit,
                "next_offset": offset + len(tasks),
                "next_task_id": tasks[-1]["task_id"] if tasks else after_task_id,
            }
        )

    async def create_task(self, request: WsRequest) -> Response:
        guard = self._guard(request)
        if guard is not None:
            return guard
        body = request_json(request)
        if body is None:
            return http_error(400, "invalid JSON body")
        chat_id = body.get("chat_id")
        content = body.get("content")
        if not is_valid_webui_chat_id(chat_id):
            return http_error(400, "invalid chat_id")
        if not isinstance(content, str) or not content.strip():
            return http_error(400, "content is required")
        profile = parse_reasoning_profile(
            body.get("reasoning_profile", DEFAULT_REASONING_PROFILE)
        )
        if profile is None:
            return http_error(400, "invalid reasoning_profile")
        title = body.get("title")
        store = self.hub.store
        task = await store.run_io(
            store.create_task,
            chat_id=str(chat_id),
            content=content,
            mode="background",
            title=title if isinstance(title, str) else None,
            model=self.hub.runtime_model_name,
            reasoning_profile=profile,
        )
        task_id = str(task["task_id"])
        try:
            await self.hub.publish_work_inbound(
                task,
                sender_id="rest",
                content=content,
            )
        except Exception:
            logger.exception("failed to enqueue REST Work task {}", task_id)
            await self.hub.fail_enqueue(task_id)
            return http_error(503, "failed to enqueue work")
        return http_json_response({"task": task}, status=201)

    async def detail(self, request: WsRequest, raw_task_id: str) -> Response:
        guard = self._guard(request)
        if guard is not None:
            return guard
        task_id = decode_id(raw_task_id, WORK_ID_RE)
        if task_id is None:
            return http_error(400, "invalid task id")
        store = self.hub.store
        task = await store.run_io(store.task_snapshot, task_id)
        if task is None:
            return http_error(404, "task not found")
        augment_artifact_urls(task)
        return http_json_response({"task": task})

    async def events(self, request: WsRequest, raw_task_id: str) -> Response:
        guard = self._guard(request)
        if guard is not None:
            return guard
        found = await self._task_or_error(raw_task_id)
        if isinstance(found, Response):
            return found
        task_id, _ = found
        query = parse_query(request.path)
        after_raw = query_first(query, "after") or query_first(query, "after_seq") or "0"
        try:
            after_seq = max(0, int(after_raw))
        except ValueError:
            return http_error(400, "after must be an integer")
        try:
            limit = max(1, min(MAX_EVENT_PAGE, int(query_first(query, "limit") or MAX_EVENT_PAGE)))
        except ValueError:
            return http_error(400, "limit must be an integer")
        store = self.hub.store
        events = await store.run_io(
            store.list_events,
            task_id,
            after_seq=after_seq,
            limit=limit,
        )
        return http_json_response(
            {
                "events": events,
                "has_more": len(events) == limit,
                "next_after_seq": events[-1]["seq"] if events else after_seq,
            }
        )

    async def cancel(self, request: WsRequest, raw_task_id: str) -> Response:
        guard = self._guard(request)
        if guard is not None:
            return guard
        found = await self._task_or_error(raw_task_id)
        if isinstance(found, Response):
            return found
        task_id, task = found
        error = await self.hub.cancel_task(task, sender_id="rest")
        if error == "terminal":
            return http_error(409, "task is already complete")
        if error is not None:
            return http_error(503, "failed to signal cancellation")
        store = self.hub.store
        current = await store.run_io(store.get_task, task_id)
        return http_json_response({"task": current})

    async def message(self, request: WsRequest, raw_task_id: str) -> Response:
        guard = self._guard(request)
        if guard is not None:
            return guard
        found = await self._task_or_error(raw_task_id)
        if isinstance(found, Response):
            return found
        task_id, task = found
        if task.get("status") not in ACTIVE_STATUSES:
            return http_error(409, "task does not accept messages")
        body = request_json(request)
        content = body.get("content") if body is not None else None
        if not isinstance(content, str) or not content.strip():
            return http_error(400, "content is required")
        if not is_valid_webui_chat_id(task.get("chat_id")):
            return http_error(500, "task chat is invalid")
        try:
            await self.hub.publish_work_inbound(task, sender_id="rest", content=content)
        except Exception:
            logger.exception("failed to enqueue message for Work task {}", task_id)
            return http_error(503, "failed to enqueue work message")
        await self.hub.record_message(task_id, content)
        return http_json_response({"accepted": True, "task_id": task_id}, status=202)

    async def artifact(self, request: WsRequest, raw_artifact_id: str) -> Response | Any:
        guard = self._guard(request)
        if guard is not None:
            return guard
        artifact_id = decode_id(raw_artifact_id, ARTIFACT_ID_RE)
        if artifact_id is None:
            return http_error(400, "invalid artifact id")
        store = self.hub.store
        item = await store.run_io(store.artifact_path, artifact_id)
        if item is None:
            return http_error(404, "artifact not found")
        path, metadata = item
        mime = metadata.get("mime")
        if not isinstance(mime, str) or not mime:
            mime = "application/octet-stream"
        name = metadata.get("name")
        filename = _header_safe_filename(name)
        return TransportFileResponse(
            path=path,
            content_type=mime,
            headers={
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
                "Content-Disposition": f'attachment; filename="{filename}"',
            },
        )
