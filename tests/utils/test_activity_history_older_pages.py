"""Recovered Activity rows on paginated (``before=``) thread pages (MIT-1060).

MIT-1027 recovered never-journaled tool activity only on the latest page, so
once newer turns pushed a crashed turn off that page its interrupted Activity
row disappeared on scrollback. These tests pin the follow-up: an older page
reads the journal when its own time range covers a record's ``started_at``
and still bails before the read when it cannot host the row.
"""

from datetime import datetime, timezone
from typing import Any

import pytest

from nanobot.utils.activity_history import KEY, project_activity_history

T_Q1 = 1_000
T_A1 = 1_100
T_END1 = 1_200
T_CRASHED_USER = 2_000
T_CRASHED_TOOL = 2_500
T_Q3 = 3_000
T_A3 = 3_100
T_END3 = 4_000


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path, monkeypatch) -> None:
    """No session in these tests has a transcript journal unless it writes one."""
    monkeypatch.setattr("nanobot.config.paths.get_data_dir", lambda: tmp_path)


@pytest.fixture
def journal_reads(monkeypatch) -> list[int]:
    """Records each whole-journal read the projection performs."""
    from nanobot.utils import activity_history

    calls: list[int] = []
    real = activity_history._journaled_activity_state

    def spy(session_key: str) -> Any:
        calls.append(1)
        return real(session_key)

    monkeypatch.setattr(activity_history, "_journaled_activity_state", spy)
    return calls


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat()


def _append_closed_turn(key: str, chat_id: str, idx: int, ms: int) -> None:
    from nanobot.webui.transcript import append_transcript_object

    append_transcript_object(
        key, {"event": "user", "chat_id": chat_id, "text": f"q{idx}", "created_at_ms": ms}
    )
    append_transcript_object(
        key,
        {
            "event": "message",
            "chat_id": chat_id,
            "text": f"a{idx}",
            "created_at_ms": ms + 100,
        },
    )
    append_transcript_object(
        key, {"event": "turn_end", "chat_id": chat_id, "created_at_ms": ms + 200}
    )


def _seed_journal(key: str, chat_id: str) -> None:
    """Turn 1 completed; turn 2 crashed mid-tool-call (only its user row
    journaled); a later turn completed after the crash."""
    _append_closed_turn(key, chat_id, 1, T_Q1)
    from nanobot.webui.transcript import append_transcript_object

    append_transcript_object(
        key,
        {"event": "user", "chat_id": chat_id, "text": "q2", "created_at_ms": T_CRASHED_USER},
    )
    _append_closed_turn(key, chat_id, 3, T_Q3)


def _crashed_record(ms: int = T_CRASHED_TOOL) -> dict[str, Any]:
    return {
        "call_id": "call-x",
        "name": "exec",
        "summary": "Running a command",
        "status": "running",  # the recorder last saw it start; the turn then died
        "started_at": _iso(ms),
        "before_message_count": 2,
        "text": '{"arguments": {"command": "ls"}, "error": null}',
    }


def _user_row(ms: int, content: str) -> dict[str, Any]:
    return {"role": "user", "content": content, "createdAt": ms}


def _assistant_row(ms: int, content: str) -> dict[str, Any]:
    return {"role": "assistant", "content": content, "createdAt": ms}


def _older_page_payload(key: str, chat_id: str, page: list[dict[str, Any]], record: dict) -> dict:
    _seed_journal(key, chat_id)
    return {"key": key, "metadata": {KEY: [dict(record)]}, "messages": page}


def _recovered_rows(payload: dict) -> list[dict[str, Any]]:
    return [row for row in payload["messages"] if row.get("id") == "tool-call-x"]


def test_older_page_ending_on_the_crashed_turn_recovers_its_row() -> None:
    """The issue's repro: the crashed turn is the page's last row, so its
    tool started *after* every timestamp on the page. The page's open tail
    must still count as covering the record."""
    key = "websocket:older-tail"
    page = [
        _user_row(T_Q1, "q1"),
        _assistant_row(T_A1, "a1"),
        _user_row(T_CRASHED_USER, "q2"),
    ]
    payload = _older_page_payload(key, "older-tail", page, _crashed_record())

    project_activity_history(payload, active=False, is_latest_page=False)

    ids = [row.get("id") for row in payload["messages"]]
    assert ids == [None, None, None, "tool-call-x"]
    row = payload["messages"][3]
    assert row["role"] == "tool"
    (tool_event,) = row["toolEvents"]
    assert tool_event["call_id"] == "call-x"
    assert tool_event["status"] == "interrupted"
    assert tool_event["error"] == "Interrupted"


def test_older_page_covering_the_record_mid_page_recovers_its_row() -> None:
    """The record falls between two user rows on the page; it anchors under
    the turn that ran it, not the page tail."""
    key = "websocket:older-mid"
    page = [
        _user_row(T_Q1, "q1"),
        _assistant_row(T_A1, "a1"),
        _user_row(T_CRASHED_USER, "q2"),
        _user_row(T_Q3, "q3"),
        _assistant_row(T_A3, "a3"),
    ]
    payload = _older_page_payload(key, "older-mid", page, _crashed_record())

    project_activity_history(payload, active=False, is_latest_page=False)

    ids = [row.get("id") for row in payload["messages"]]
    assert ids == [None, None, None, "tool-call-x", None, None]
    assert payload["messages"][3]["role"] == "tool"


def test_older_page_with_a_live_turn_never_shows_recovered_rows_as_running() -> None:
    """The live turn is on the latest page by definition, so a record left
    ``running`` by the page's crashed tail turn reads as interrupted even
    while some newer turn is live."""
    key = "websocket:older-live"
    page = [
        _user_row(T_Q1, "q1"),
        _user_row(T_CRASHED_USER, "q2"),
    ]
    payload = _older_page_payload(key, "older-live", page, _crashed_record())

    project_activity_history(payload, active=True, is_latest_page=False)

    (row,) = _recovered_rows(payload)
    (tool_event,) = row["toolEvents"]
    assert tool_event["status"] == "interrupted"


@pytest.mark.parametrize(
    "started_ms",
    [T_Q1 - 5_000, T_END3 + 5_000],
    ids=["predates-page", "postdates-closed-page"],
)
def test_older_page_outside_the_record_time_range_skips_the_journal_read(
    journal_reads: list[int], started_ms: int
) -> None:
    """Perf property from MIT-1027: a page that cannot host the row must bail
    before reading the journal."""
    key = f"websocket:outside-{started_ms}"
    page = [
        _user_row(T_Q1, "q1"),
        _assistant_row(T_A1, "a1"),
        _user_row(T_Q3, "q3"),
        _assistant_row(T_A3, "a3"),
    ]
    payload = _older_page_payload(key, "outside", page, _crashed_record(started_ms))

    project_activity_history(payload, active=False, is_latest_page=False)

    assert _recovered_rows(payload) == []
    assert journal_reads == []


def test_older_page_without_dated_rows_skips_the_journal_read(
    journal_reads: list[int],
) -> None:
    """A page whose rows carry no usable timestamps cannot prove coverage, so
    it keeps the MIT-1027 bail-out instead of paying for a journal scan."""
    key = "websocket:undated"
    page = [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
    ]
    payload = _older_page_payload(key, "undated", page, _crashed_record())

    project_activity_history(payload, active=False, is_latest_page=False)

    assert _recovered_rows(payload) == []
    assert journal_reads == []


def test_older_page_without_candidates_skips_the_journal_read(
    journal_reads: list[int],
) -> None:
    """The cheap pre-journal short-circuit still covers pages whose replayed
    rows already render every recorded call."""
    key = "websocket:covered"
    covered = {
        "version": 1,
        "phase": "end",
        "status": "completed",
        "call_id": "call-x",
        "name": "exec",
        "arguments": None,
        "result": None,
        "error": None,
        "files": [],
        "embeds": [],
    }
    page = [
        _user_row(T_Q1, "q1"),
        {
            "id": "tr-1",
            "role": "tool",
            "kind": "trace",
            "content": "exec(ls)",
            "traces": ["exec(ls)"],
            "toolEvents": [covered],
            "createdAt": T_A1,
        },
    ]
    payload = _older_page_payload(key, "covered", page, _crashed_record())

    project_activity_history(payload, active=False, is_latest_page=False)

    assert _recovered_rows(payload) == []
    assert journal_reads == []
