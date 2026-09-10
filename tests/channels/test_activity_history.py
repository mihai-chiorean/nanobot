from copy import deepcopy

from nanobot.session.manager import Session
from nanobot.utils.activity_history import project_activity_history, record_tool_activity


def test_actions_keep_identity_times_and_interrupted_outcome_after_restore():
    session = Session(key="websocket:activity")
    session.add_message("user", "Read the report")
    start = {
        "call_id": "one",
        "phase": "start",
        "name": "read_file",
        "summary": "Reading the report",
        "arguments": {"path": "report.txt"},
    }
    record_tool_activity(session, [start])
    ended = {**start, "phase": "end"}
    record_tool_activity(session, [ended])
    assert start["started_at"] == ended["started_at"]
    assert ended["completed_at"] >= start["started_at"]
    record_tool_activity(
        session,
        [{"call_id": "two", "phase": "start", "name": "web_fetch", "summary": "Reading a source"}],
    )
    payload = {
        "key": session.key,
        "metadata": deepcopy(session.metadata),
        "messages": deepcopy(session.messages),
    }
    project_activity_history(payload, active=False)
    assert len(payload["messages"]) == 3
    assert all(item["kind"] == "tool_hint" for item in payload["messages"][1:])
    assert payload["messages"][1]["blocks"][0]["status"] == "completed"
    assert payload["messages"][2]["blocks"][0]["status"] == "interrupted"
    assert "activity_v1" not in payload["metadata"]
