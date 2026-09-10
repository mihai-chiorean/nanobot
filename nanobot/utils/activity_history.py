"""Bounded presentation events, separate from model reasoning/history."""

import json
from datetime import datetime, timezone

KEY = "activity_v1"


def record_tool_activity(session, events):
    now = datetime.now(timezone.utc).isoformat()
    records = session.metadata.setdefault(KEY, [])
    if not isinstance(records, list):
        records = []
        session.metadata[KEY] = records
    for event in events:
        call_id = event.get("call_id")
        if not isinstance(call_id, str) or not call_id:
            continue
        record = next((r for r in records if r.get("call_id") == call_id), None)
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


def project_activity_history(payload, *, active: bool):
    metadata = payload.get("metadata", {})
    records = metadata.pop(KEY, []) if isinstance(metadata, dict) else []
    messages = payload.get("messages")
    if not isinstance(records, list) or not isinstance(messages, list):
        return
    groups = {}
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("before_message_count"), int):
            continue
        status = record.get("status", "interrupted")
        if status == "running" and not active:
            status = "interrupted"
        block = {
            "type": "tool",
            "name": record.get("name"),
            "summary": record.get("summary"),
            "text": record.get("text", ""),
            "status": status,
            "started_at": record.get("started_at"),
            "completed_at": record.get("completed_at"),
        }
        item = {
            "version": "1",
            "id": "tool-" + record.get("call_id", ""),
            "chat_id": payload.get("key", "").removeprefix("websocket:"),
            "role": "assistant",
            "content": record.get("summary", "Tool activity"),
            "created_at": record.get("started_at"),
            "blocks": [block],
        }
        groups.setdefault(min(record["before_message_count"], len(messages)), []).append(item)
    combined = []
    for i in range(len(messages) + 1):
        combined.extend(groups.get(i, []))
        if i < len(messages):
            combined.append(messages[i])
    payload["messages"] = combined
