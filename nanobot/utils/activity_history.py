"""Bounded presentation events, separate from model reasoning/history."""

import json
from datetime import datetime, timezone
from typing import Any, cast

from nanobot.webui.session_identity import webui_chat_id

KEY = "activity_v1"


def record_tool_activity(session: Any, events: list[dict[str, Any]]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    stored_records: Any = session.metadata.setdefault(KEY, [])
    if not isinstance(stored_records, list):
        stored_records = []
        session.metadata[KEY] = stored_records
    records = cast(list[Any], stored_records)
    for event in events:
        call_id = event.get("call_id")
        if not isinstance(call_id, str) or not call_id:
            continue
        record: dict[str, Any] | None = None
        for candidate in records:
            if not isinstance(candidate, dict):
                continue
            candidate_record = cast(dict[str, Any], candidate)
            if candidate_record.get("call_id") == call_id:
                record = candidate_record
                break
        if record is None:
            record = {
                "call_id": call_id,
                "started_at": now,
                "before_message_count": len(session.messages),
            }
            records.append(record)
        phase = event.get("phase")
        record.update(
            name=str(event.get("name") or "")[:256],
            summary=str(event.get("summary") or "Using a tool")[:512],
            status="running" if phase == "start" else "failed" if phase == "error" else "completed",
        )
        record["text"] = json.dumps(
            {"arguments": event.get("arguments"), "error": event.get("error")},
            ensure_ascii=False,
            default=str,
        )[:4096]
        if phase != "start":
            record["completed_at"] = now
        event["started_at"] = record["started_at"]
        if "completed_at" in record:
            event["completed_at"] = record["completed_at"]
    # Bound recovery metadata; ordinary transcript content remains untouched.
    session.metadata[KEY] = records[-500:]


def _record_timestamp_ms(value: object) -> int:
    if not isinstance(value, str) or not value:
        return 0
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return 0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def _valid_created_ms(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value < 0:
        return None
    return int(value)


def _tool_event_call_ids(raw_tool_events: Any) -> set[str]:
    if not isinstance(raw_tool_events, list):
        return set()
    call_ids: set[str] = set()
    for raw_event in cast(list[Any], raw_tool_events):
        if not isinstance(raw_event, dict):
            continue
        call_id = cast(dict[str, Any], raw_event).get("call_id")
        if isinstance(call_id, str) and call_id:
            call_ids.add(call_id)
    return call_ids


def _journaled_activity_state(
    session_key: str,
) -> tuple[set[str], int | None, bool, bool] | None:
    """Whole-journal view of what the transcript already captured.

    Returns ``(journaled_call_ids, last_turn_end_ms, has_lines, open_trailing_turn)``,
    or ``None`` when the journal cannot be read at all. Dedup must span the
    whole journal, not only the page the route happens to render: the
    ``webui-thread`` route paginates, and a call whose trace row lives on an
    older page is otherwise re-synthesized into whichever page is requested.
    """
    try:
        from nanobot.webui.transcript import read_transcript_lines

        raw_lines: Any = read_transcript_lines(session_key)
    except Exception:
        return None
    if not isinstance(raw_lines, list):
        return set(), None, False, False
    lines = cast(list[Any], raw_lines)
    journaled: set[str] = set()
    last_turn_end_ms: int | None = None
    last_turn_end_index = -1
    for index, raw_line in enumerate(lines):
        if not isinstance(raw_line, dict):
            continue
        line = cast(dict[str, Any], raw_line)
        journaled |= _tool_event_call_ids(line.get("tool_events"))
        if line.get("event") == "turn_end":
            created_ms = _valid_created_ms(line.get("created_at_ms"))
            if created_ms is not None and (
                last_turn_end_ms is None or created_ms > last_turn_end_ms
            ):
                last_turn_end_ms = created_ms
            last_turn_end_index = index
    tail = lines[last_turn_end_index + 1 :]
    has_lines = any(isinstance(raw_line, dict) for raw_line in lines)
    open_trailing_turn = any(
        isinstance(raw_line, dict)
        and cast(dict[str, Any], raw_line).get("event") == "user"
        for raw_line in tail
    )
    return journaled, last_turn_end_ms, has_lines, open_trailing_turn


def _record_tool_event(record: dict[str, Any], status: str) -> dict[str, Any]:
    """Rebuild one WebUI tool-progress frame from a persisted activity record."""
    arguments: Any = None
    error: Any = None
    stored: Any = None
    try:
        stored = json.loads(str(record.get("text") or "{}"))
    except ValueError:
        stored = None
    if isinstance(stored, dict):
        stored_data = cast(dict[str, Any], stored)
        arguments = stored_data.get("arguments")
        error = stored_data.get("error")
    if status == "completed":
        phase = "end"
    elif status == "failed":
        phase = "error"
    elif status == "running":
        phase = "start"
    else:  # interrupted: recorded while running, the turn never came back
        status = "interrupted"
        phase = "error"
        error = error or "Interrupted"
    return {
        "version": 1,
        "phase": phase,
        # ``status`` keeps an interrupted call distinguishable from a genuinely
        # failed one without string-matching the error text; clients that do
        # not know the key ignore it.
        "status": status,
        "call_id": record.get("call_id"),
        "name": record.get("name"),
        "arguments": arguments,
        "result": None,
        "error": error,
        "files": [],
        "embeds": [],
    }


def _resolved_status(record: dict[str, Any], *, active: bool) -> str:
    status = str(record.get("status") or "interrupted")
    if status == "running" and not active:
        return "interrupted"
    return status


def _activity_item(record: dict[str, Any], status: str, chat_id: str) -> dict[str, Any]:
    summary = record.get("summary") or "Tool activity"
    # The same shape the transcript replay emits for tool activity, so any
    # WebUI or iOS client that renders journaled turns renders these.
    return {
        "id": "tool-" + str(record.get("call_id", "")),
        "role": "tool",
        "kind": "trace",
        "content": summary,
        "traces": [summary],
        "toolEvents": [_record_tool_event(record, status)],
        "createdAt": _record_timestamp_ms(record.get("started_at")),
        "chat_id": chat_id,
    }


def _splice_by_message_index(
    messages: list[Any],
    recoverable: list[dict[str, Any]],
    *,
    chat_id: str,
    active: bool,
) -> None:
    """Journal-less fallback: position by ``before_message_count``.

    Without a journal the replayed rows are the session messages themselves,
    so the recording-time index still addresses the right slot.
    """
    groups: dict[int, list[dict[str, Any]]] = {}
    for record in recoverable:
        status = _resolved_status(record, active=active)
        item = _activity_item(record, status, chat_id)
        before = int(record["before_message_count"])
        groups.setdefault(min(before, len(messages)), []).append(item)
    combined: list[Any] = []
    for index in range(len(messages) + 1):
        combined.extend(groups.get(index, []))
        if index < len(messages):
            combined.append(messages[index])
    messages[:] = combined


def _splice_into_open_turn(
    messages: list[Any],
    recoverable: list[dict[str, Any]],
    *,
    chat_id: str,
    active: bool,
) -> None:
    """Anchor recovered rows to the turn that ran the tool.

    The rows are journaled, so the replayed list also carries trace,
    reasoning and file-edit rows; a ``before_message_count`` index into
    ``session.messages`` would land early as soon as any earlier turn
    produced an extra row. The only unjournaled turn a latest page can own
    is the open one, and that turn's user row is its anchor.
    """
    anchor: int | None = None
    for index, raw_message in enumerate(messages):
        if (
            isinstance(raw_message, dict)
            and cast(dict[str, Any], raw_message).get("role") == "user"
        ):
            anchor = index
    if anchor is None:
        # The page does not host the turn that ran the tool; its rows belong
        # to the page that does.
        return
    messages[anchor + 1 : anchor + 1] = [
        _activity_item(record, _resolved_status(record, active=active), chat_id)
        for record in recoverable
    ]


def project_activity_history(
    payload: dict[str, Any],
    *,
    active: bool,
    is_latest_page: bool = True,
) -> None:
    stored_metadata: Any = payload.get("metadata", {})
    metadata = cast(dict[str, Any], stored_metadata) if isinstance(stored_metadata, dict) else {}
    raw_records: Any = metadata.pop(KEY, [])
    raw_messages: Any = payload.get("messages")
    if not isinstance(raw_records, list) or not isinstance(raw_messages, list):
        return
    records = cast(list[Any], raw_records)
    messages = cast(list[Any], raw_messages)
    # A turn whose activity already reached the transcript journal renders its
    # own Activity rows; replayed ``toolEvents`` name the calls they cover.
    # Skip those records here so a recovered row never doubles up.
    page_covered: set[str] = set()
    for raw_message in messages:
        if not isinstance(raw_message, dict):
            continue
        page_covered |= _tool_event_call_ids(
            cast(dict[str, Any], raw_message).get("toolEvents")
        )
    candidates: list[dict[str, Any]] = []
    for raw_record in records:
        if not isinstance(raw_record, dict):
            continue
        record = cast(dict[str, Any], raw_record)
        if not isinstance(record.get("before_message_count"), int):
            continue
        call_id = record.get("call_id")
        if isinstance(call_id, str) and call_id in page_covered:
            continue
        candidates.append(record)
    if not candidates:
        return
    journal = _journaled_activity_state(str(payload.get("key", "")))
    if journal is None:
        # Without a readable journal coverage cannot be proven; leaving the
        # page untouched is safer than risking a double-rendered call.
        return
    journaled, last_turn_end_ms, has_journal_lines, open_trailing_turn = journal
    recoverable: list[dict[str, Any]] = []
    for record in candidates:
        call_id = record.get("call_id")
        if isinstance(call_id, str) and call_id in journaled:
            continue
        if last_turn_end_ms is not None:
            # A turn has journaled its end after this record started: the call
            # belongs to history the journal lost. Re-anchoring it under a
            # surviving turn would render it under the wrong turn, so drop it.
            if not _record_timestamp_ms(record.get("started_at")) > last_turn_end_ms:
                continue
        recoverable.append(record)
    if not recoverable:
        return
    chat_id = webui_chat_id(str(payload.get("key", ""))) or ""
    if not has_journal_lines:
        _splice_by_message_index(messages, recoverable, chat_id=chat_id, active=active)
    elif open_trailing_turn and is_latest_page:
        _splice_into_open_turn(messages, recoverable, chat_id=chat_id, active=active)
