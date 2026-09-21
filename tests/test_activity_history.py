"""Pinned shape of the session-file tool-activity records and their projection."""

from datetime import datetime, timezone

from nanobot.utils.activity_history import KEY, project_activity_history, record_tool_activity


class _FakeSession:
    def __init__(self, messages: list[dict]) -> None:
        self.messages = messages
        self.metadata: dict = {}


def _start_event(call_id: str = "call-1", name: str = "read_file") -> dict:
    return {
        "version": 1,
        "phase": "start",
        "call_id": call_id,
        "name": name,
        "arguments": {"path": "notes.md"},
        "result": None,
        "error": None,
        "files": [],
        "embeds": [],
    }


def _end_event(call_id: str = "call-1", name: str = "read_file") -> dict:
    event = _start_event(call_id, name)
    event["phase"] = "end"
    event["result"] = "body"
    return event


def _payload(messages: list[dict]) -> dict:
    return {"key": "websocket:chat1", "metadata": {}, "messages": messages}


def _running_session() -> _FakeSession:
    session = _FakeSession([{"role": "user", "content": "read the notes"}])
    record_tool_activity(session, [_start_event()])
    return session


def test_record_tool_activity_keeps_one_row_per_call_id() -> None:
    session = _FakeSession([{"role": "user", "content": "read the notes"}])
    record_tool_activity(session, [_start_event()])
    record_tool_activity(session, [_end_event()])

    records = session.metadata[KEY]
    assert len(records) == 1
    record = records[0]
    assert record["call_id"] == "call-1"
    assert record["name"] == "read_file"
    assert record["status"] == "completed"
    assert record["before_message_count"] == 1
    assert "completed_at" in record
    assert '"path": "notes.md"' in record["text"]


def test_projection_pins_the_activity_row_shape() -> None:
    session = _running_session()
    record_tool_activity(session, [_end_event()])
    payload = _payload([dict(message) for message in session.messages])
    payload["metadata"] = {KEY: [dict(record) for record in session.metadata[KEY]]}

    project_activity_history(payload, active=True)

    assert KEY not in payload["metadata"]
    assert len(payload["messages"]) == 2
    user, activity = payload["messages"]
    assert user["role"] == "user"
    assert activity["id"] == "tool-call-1"
    assert activity["role"] == "tool"
    assert activity["kind"] == "trace"
    assert activity["chat_id"] == "chat1"
    assert activity["content"] == "Using a tool"
    assert activity["traces"] == ["Using a tool"]
    assert activity["createdAt"] > 0
    (tool_event,) = activity["toolEvents"]
    assert tool_event["call_id"] == "call-1"
    assert tool_event["name"] == "read_file"
    assert tool_event["phase"] == "end"
    assert tool_event["arguments"] == {"path": "notes.md"}


def test_projection_distinguishes_active_from_interrupted() -> None:
    active = _payload([{"role": "user", "content": "x"}])
    active["metadata"] = {KEY: [dict(r) for r in _running_session().metadata[KEY]]}
    project_activity_history(active, active=True)
    ((active_event,),) = [row["toolEvents"] for row in active["messages"] if "toolEvents" in row]
    assert active_event["phase"] == "start"

    idle = _payload([{"role": "user", "content": "x"}])
    idle["metadata"] = {KEY: [dict(r) for r in _running_session().metadata[KEY]]}
    project_activity_history(idle, active=False)
    ((idle_event,),) = [row["toolEvents"] for row in idle["messages"] if "toolEvents" in row]
    assert idle_event["phase"] == "error"
    assert idle_event["error"] == "Interrupted"


def test_projection_skips_calls_the_journal_already_rendered() -> None:
    session = _running_session()
    record_tool_activity(session, [_end_event()])
    journaled = {
        "id": "tr-7",
        "role": "tool",
        "kind": "trace",
        "content": "read_file(notes.md)",
        "traces": ["read_file(notes.md)"],
        "toolEvents": [_end_event()],
        "createdAt": int(datetime.now(timezone.utc).timestamp() * 1000),
    }
    payload = _payload([{"role": "user", "content": "read the notes"}, dict(journaled)])
    payload["metadata"] = {KEY: [dict(record) for record in session.metadata[KEY]]}

    project_activity_history(payload, active=False)

    assert payload["messages"][1] == journaled
    assert not [message for message in payload["messages"] if message.get("id") == "tool-call-1"]
