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
        phase = "error"
        error = error or "Interrupted"
    return {
        "version": 1,
        "phase": phase,
        "call_id": record.get("call_id"),
        "name": record.get("name"),
        "arguments": arguments,
        "result": None,
        "error": error,
        "files": [],
        "embeds": [],
    }


def project_activity_history(payload: dict[str, Any], *, active: bool) -> None:
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
    covered: set[str] = set()
    for raw_message in messages:
        if not isinstance(raw_message, dict):
            continue
        message = cast(dict[str, Any], raw_message)
        tool_events = message.get("toolEvents")
        if not isinstance(tool_events, list):
            continue
        for raw_event in cast(list[Any], tool_events):
            if not isinstance(raw_event, dict):
                continue
            event = cast(dict[str, Any], raw_event)
            call_id = event.get("call_id")
            if isinstance(call_id, str) and call_id:
                covered.add(call_id)
    groups: dict[int, list[dict[str, Any]]] = {}
    for raw_record in records:
        if not isinstance(raw_record, dict):
            continue
        record = cast(dict[str, Any], raw_record)
        if not isinstance(record.get("before_message_count"), int):
            continue
        if record.get("call_id") in covered:
            continue
        status = record.get("status", "interrupted")
        if status == "running" and not active:
            status = "interrupted"
        summary = record.get("summary") or "Tool activity"
        # The same shape the transcript replay emits for tool activity, so any
        # WebUI or iOS client that renders journaled turns renders these.
        item: dict[str, Any] = {
            "id": "tool-" + str(record.get("call_id", "")),
            "role": "tool",
            "kind": "trace",
            "content": summary,
            "traces": [summary],
            "toolEvents": [_record_tool_event(record, str(status))],
            "createdAt": _record_timestamp_ms(record.get("started_at")),
            "chat_id": webui_chat_id(str(payload.get("key", ""))) or "",
        }
        before = int(record["before_message_count"])
        groups.setdefault(min(before, len(messages)), []).append(item)
    combined: list[Any] = []
    for index in range(len(messages) + 1):
        combined.extend(groups.get(index, []))
        if index < len(messages):
            combined.append(messages[index])
    payload["messages"] = combined
