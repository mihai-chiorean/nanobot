"""Pinned shape of the session-file tool-activity records and their projection."""

from datetime import datetime, timezone

import pytest

from nanobot.utils.activity_history import KEY, project_activity_history, record_tool_activity


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path, monkeypatch) -> None:
    """No session in these tests has a transcript journal unless it writes one."""
    monkeypatch.setattr("nanobot.config.paths.get_data_dir", lambda: tmp_path)


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
    assert active_event["status"] == "running"

    idle = _payload([{"role": "user", "content": "x"}])
    idle["metadata"] = {KEY: [dict(r) for r in _running_session().metadata[KEY]]}
    project_activity_history(idle, active=False)
    ((idle_event,),) = [row["toolEvents"] for row in idle["messages"] if "toolEvents" in row]
    assert idle_event["phase"] == "error"
    assert idle_event["error"] == "Interrupted"
    # ``status`` separates an interrupted call from a genuinely failed one
    # without clients having to string-match the error text.
    assert idle_event["status"] == "interrupted"


def test_projection_keeps_real_failures_distinguishable() -> None:
    session = _FakeSession([{"role": "user", "content": "read the notes"}])
    failed = _start_event("call-f")
    failed["phase"] = "error"
    failed["error"] = "exit code 2"
    record_tool_activity(session, [failed])

    payload = _payload([{"role": "user", "content": "read the notes"}])
    payload["metadata"] = {KEY: [dict(r) for r in session.metadata[KEY]]}
    project_activity_history(payload, active=False)

    ((event,),) = [row["toolEvents"] for row in payload["messages"] if "toolEvents" in row]
    assert event["phase"] == "error"
    assert event["status"] == "failed"
    assert event["error"] == "exit code 2"


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


def _append_journal_turn(
    key: str,
    chat_id: str,
    idx: int,
    *,
    tool_events: list[dict] | None = None,
    close: bool = True,
) -> None:
    from nanobot.webui.transcript import append_transcript_object

    append_transcript_object(
        key, {"event": "user", "chat_id": chat_id, "text": f"question {idx}"}
    )
    record: dict = {"event": "message", "chat_id": chat_id, "text": f"answer {idx}"}
    if tool_events is not None:
        record["kind"] = "tool_hint"
        record["tool_events"] = tool_events
    append_transcript_object(key, record)
    if close:
        append_transcript_object(key, {"event": "turn_end", "chat_id": chat_id})


def _activity_records(*events: dict, messages: list[dict]) -> list[dict]:
    session = _FakeSession(list(messages))
    record_tool_activity(session, list(events))
    return [dict(record) for record in session.metadata[KEY]]


def test_latest_page_does_not_resynthesize_calls_journaled_on_older_pages() -> None:
    """The dedup must see the whole journal, not only the page being rendered."""
    key = "websocket:paged-dedup"
    _append_journal_turn(key, "paged-dedup", 1, tool_events=[_end_event("call-a", "exec")])
    _append_journal_turn(key, "paged-dedup", 2)

    page = [
        {"role": "user", "content": "question 2"},
        {"role": "assistant", "content": "answer 2"},
    ]
    payload = {"key": key, "metadata": {}, "messages": page}
    payload["metadata"] = {
        KEY: _activity_records(
            _start_event("call-a", "exec"),
            _end_event("call-a", "exec"),
            messages=[{"role": "user", "content": "question 1"}],
        )
    }

    project_activity_history(payload, active=False, is_latest_page=True)

    assert [row.get("id") for row in payload["messages"]] == [None, None]


def test_older_pages_never_host_recovered_rows() -> None:
    key = "websocket:older-page"
    _append_journal_turn(key, "older-page", 1)
    _append_journal_turn(key, "older-page", 2, close=False)

    page = [
        {"role": "user", "content": "question 1"},
        {"role": "assistant", "content": "answer 1"},
    ]
    payload = {"key": key, "metadata": {}, "messages": page}
    payload["metadata"] = {
        KEY: _activity_records(
            _start_event("call-b", "exec"),
            messages=[
                {"role": "user", "content": "question 1"},
                {"role": "assistant", "content": "answer 1"},
                {"role": "user", "content": "question 2"},
            ],
        )
    }

    project_activity_history(payload, active=False, is_latest_page=False)

    assert [row.get("id") for row in payload["messages"]] == [None, None]


def test_recovered_rows_anchor_to_the_open_turn_user_row() -> None:
    """A journaled trace row would otherwise push the recovered row too early."""
    key = "websocket:open-turn"
    from nanobot.webui.transcript import append_transcript_object

    _append_journal_turn(key, "open-turn", 1, tool_events=[_end_event("call-old", "exec")])
    # The crashed turn journaled its user row and nothing after it: the tool
    # call itself never reached the journal.
    append_transcript_object(
        key, {"event": "user", "chat_id": "open-turn", "text": "question 2"}
    )

    journaled_trace = {
        "id": "tr-old",
        "role": "tool",
        "kind": "trace",
        "content": "exec(ls)",
        "traces": ["exec(ls)"],
        "toolEvents": [_end_event("call-old", "exec")],
        "createdAt": int(datetime.now(timezone.utc).timestamp() * 1000),
    }
    page = [
        {"role": "user", "content": "question 1"},
        dict(journaled_trace),
        {"role": "assistant", "content": "answer 1"},
        {"role": "user", "content": "question 2"},
    ]
    records = _activity_records(
        {**_start_event("call-crash", "exec"), "phase": "start"},
        messages=[
            {"role": "user", "content": "question 1"},
            {"role": "assistant", "content": "answer 1"},
            {"role": "user", "content": "question 2"},
        ],
    )
    # The tool started after the last journaled turn_end: it belongs to the
    # open turn, not to the lost history the gate drops.
    started = datetime.fromtimestamp(
        datetime.now(timezone.utc).timestamp() + 120, timezone.utc
    ).isoformat()
    for record in records:
        record["started_at"] = started
    payload = {"key": key, "metadata": {KEY: records}, "messages": page}

    project_activity_history(payload, active=False)

    ids = [row.get("id") for row in payload["messages"]]
    assert ids == [None, "tr-old", None, None, "tool-call-crash"]
    # Rows the journal lost while the turn was running render as interrupted,
    # right after the user row of the turn that ran the tool.
    assert payload["messages"][4]["role"] == "tool"


def test_stale_unjournaled_records_are_not_reanchored_to_surviving_turns() -> None:
    """Records older than the last journaled turn_end belong to a lost turn."""
    key = "websocket:stale-record"
    _append_journal_turn(key, "stale-record", 1)
    _append_journal_turn(key, "stale-record", 2)

    stale = {
        "call_id": "call-stale",
        "started_at": "2020-01-01T00:00:00+00:00",
        "before_message_count": 1,
        "name": "exec",
        "summary": "Running a command",
        "status": "running",
        "text": "{}",
    }
    payload = {
        "key": key,
        "metadata": {KEY: [dict(stale)]},
        "messages": [
            {"role": "user", "content": "question 2"},
            {"role": "assistant", "content": "answer 2"},
        ],
    }

    project_activity_history(payload, active=False)

    assert [row.get("id") for row in payload["messages"]] == [None, None]


def _iso_ms(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat()


def test_recovered_call_from_a_crashed_turn_survives_a_later_completed_turn() -> None:
    """Round-2 repro: a call recorded in a crashed turn is not lost or misplaced.

    The tail after the last journaled ``turn_end`` holds two turns: the crashed
    one (``q2``, whose tool never journaled) and a later one (``q3``) that has
    since journaled its own ``turn_end``. The recovered call started inside
    ``q2``'s window, so it must anchor under ``q2`` (not the last user row) and
    read ``interrupted`` — the live turn is ``q3``, not the one that ran it.
    Under the round-1 code the ``started_at > last_turn_end`` gate dropped it
    entirely once ``q3`` journaled, and the last-user-row anchor would have put
    it under ``q3`` as ``running``.
    """
    key = "websocket:two-crash"
    from nanobot.webui.transcript import append_transcript_object

    append_transcript_object(
        key, {"event": "user", "chat_id": "two-crash", "text": "q1", "created_at_ms": 1_000}
    )
    append_transcript_object(
        key, {"event": "message", "chat_id": "two-crash", "text": "a1", "created_at_ms": 1_100}
    )
    append_transcript_object(
        key, {"event": "turn_end", "chat_id": "two-crash", "created_at_ms": 1_200}
    )
    # Crashed turn: journaled its user row, the tool call never reached the journal.
    append_transcript_object(
        key, {"event": "user", "chat_id": "two-crash", "text": "q2", "created_at_ms": 2_000}
    )
    # A later turn completed and journaled its own turn_end.
    append_transcript_object(
        key, {"event": "user", "chat_id": "two-crash", "text": "q3", "created_at_ms": 3_000}
    )
    append_transcript_object(
        key, {"event": "message", "chat_id": "two-crash", "text": "a3", "created_at_ms": 3_100}
    )
    append_transcript_object(
        key, {"event": "turn_end", "chat_id": "two-crash", "created_at_ms": 4_000}
    )

    page = [
        {"role": "user", "content": "q1", "createdAt": 1_000},
        {"role": "assistant", "content": "a1", "createdAt": 1_100},
        {"role": "user", "content": "q2", "createdAt": 2_000},
        {"role": "user", "content": "q3", "createdAt": 3_000},
        {"role": "assistant", "content": "a3", "createdAt": 3_100},
    ]
    record = {
        "call_id": "call-x",
        "name": "exec",
        "summary": "Running a command",
        "status": "running",  # the recorder last saw it start; the turn then died
        "started_at": _iso_ms(2_500),  # inside q2's [2000, 3000) window, before q3
        "before_message_count": 2,
        "text": '{"arguments": {"command": "ls"}, "error": null}',
    }
    payload = {"key": key, "metadata": {KEY: [record]}, "messages": page}

    # The session currently has a live turn (q3); the recovered call is not its
    # own, so it must not be shown as running.
    project_activity_history(payload, active=True, is_latest_page=True)

    ids = [row.get("id") for row in payload["messages"]]
    assert ids == [None, None, None, "tool-call-x", None, None]
    row = payload["messages"][3]
    assert row["role"] == "tool"
    ((tool_event,),) = [row["toolEvents"]]
    assert tool_event["call_id"] == "call-x"
    assert tool_event["status"] == "interrupted"


def test_recovered_call_stays_under_its_turn_when_a_newer_turn_is_live() -> None:
    """The live turn's own recovered call renders as running; an older one does not."""
    key = "websocket:two-live"
    from nanobot.webui.transcript import append_transcript_object

    append_transcript_object(
        key, {"event": "user", "chat_id": "two-live", "text": "q1", "created_at_ms": 1_000}
    )
    append_transcript_object(
        key, {"event": "turn_end", "chat_id": "two-live", "created_at_ms": 1_200}
    )
    append_transcript_object(
        key, {"event": "user", "chat_id": "two-live", "text": "q2", "created_at_ms": 2_000}
    )
    append_transcript_object(
        key, {"event": "user", "chat_id": "two-live", "text": "q3", "created_at_ms": 3_000}
    )

    page = [
        {"role": "user", "content": "q1", "createdAt": 1_000},
        {"role": "user", "content": "q2", "createdAt": 2_000},
        {"role": "user", "content": "q3", "createdAt": 3_000},
    ]
    # call-old ran in the crashed q2 window; call-new is the live q3's tool.
    old = {
        "call_id": "call-old",
        "name": "exec",
        "summary": "s",
        "status": "running",
        "started_at": _iso_ms(2_500),
        "before_message_count": 1,
        "text": "{}",
    }
    new = {
        "call_id": "call-new",
        "name": "exec",
        "summary": "s",
        "status": "running",
        "started_at": _iso_ms(3_500),
        "before_message_count": 2,
        "text": "{}",
    }
    payload = {"key": key, "metadata": {KEY: [old, new]}, "messages": page}

    project_activity_history(payload, active=True, is_latest_page=True)

    by_id = {row.get("id"): row for row in payload["messages"] if row.get("id")}
    assert set(by_id) == {"tool-call-old", "tool-call-new"}
    # call-old sits directly after q2 (index 1 -> inserted at 2); call-new after q3.
    ids = [row.get("id") for row in payload["messages"]]
    assert ids == [None, None, "tool-call-old", None, "tool-call-new"]
    old_event = by_id["tool-call-old"]["toolEvents"][0]
    new_event = by_id["tool-call-new"]["toolEvents"][0]
    assert old_event["status"] == "interrupted"  # q2 is not the live turn
    assert new_event["status"] == "running"  # q3 is the live turn
