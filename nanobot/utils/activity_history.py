"""Bounded presentation events, separate from model reasoning/history."""

import json
from collections.abc import Callable
from dataclasses import dataclass
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


def _record_started_ms(record: dict[str, Any]) -> int | None:
    started = _record_timestamp_ms(record.get("started_at"))
    return started if started > 0 else None


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
    """Tail view of what the transcript journal already captured.

    Returns ``(journaled_call_ids, last_turn_end_ms, has_lines, open_trailing_turn)``,
    or ``None`` when the journal cannot be read at all.

    Only the active chunk is consulted on the common path. The caller only
    reads this for a page that can host a recovered row: the latest page, or
    an older page whose time range covers a record's ``started_at``
    (MIT-1060). A call that was journaled on an older page is dropped by the
    whole-journal call-id dedup and the timestamp anchor (its turn's user row
    is not on this page), not by a whole-journal scan. A call whose trace
    reached the journal is always journaled in the same chunk as the turn that
    ran it, so the active chunk
    carries everything still relevant to the latest page. When the active chunk
    is empty (a journal that has rotated every turn, or none at all) we fall
    back to the full read so "no journal" is not confused with "journal
    entirely rotated".
    """
    try:
        from nanobot.webui.transcript import read_active_transcript_lines, read_transcript_lines

        active_lines = cast(list[Any], read_active_transcript_lines(session_key))
        has_turn_end = any(
            isinstance(line, dict) and cast(dict[str, Any], line).get("event") == "turn_end"
            for line in active_lines
        )
        if has_turn_end:
            # The active chunk holds the most recent ``turn_end`` and everything
            # appended after it (the open turn), so it alone answers both the
            # tail-shape and dedup questions for the latest page.
            raw_lines: Any = active_lines
        else:
            # No turn boundary in the active chunk: either there is no journal, it
            # has fully rotated, or an oversized turn pushed the last closed one
            # into a segment. Consult every chunk so "no journal" is not confused
            # with "tail lives in a segment" (which would mis-place recovered rows).
            raw_lines = cast(Any, read_transcript_lines(session_key))
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


def _resolved_status(record: dict[str, Any], *, active: bool, in_live_turn: bool) -> str:
    status = str(record.get("status") or "interrupted")
    # ``running`` is only trustworthy while the turn that made the call is the
    # live one: a call left ``running`` by a turn that has since been replaced
    # (a crash, then a newer turn) never came back, so it is interrupted.
    if status == "running" and not (active and in_live_turn):
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


def _activity_event(
    record: dict[str, Any],
    status: str,
    chat_id: str,
    anchor: dict[str, Any] | None,
) -> dict[str, Any]:
    """The ``?projection=events`` form of :func:`_activity_item`.

    Mirrors what ``_client_projection_event`` emits for a journaled
    ``tool_hint``/``progress`` message, joined to the anchoring user event's
    turn so clients group it under the turn that ran the tool.
    """
    summary = str(record.get("summary") or "Tool activity")
    event: dict[str, Any] = {
        "event": "message",
        "chat_id": chat_id,
        "projection_id": "activity-" + str(record.get("call_id", "")),
        "kind": "progress",
        "text": summary,
        "tool_events": [_record_tool_event(record, status)],
    }
    turn_id = anchor.get("turn_id") if anchor is not None else None
    if isinstance(turn_id, str) and turn_id:
        event["turn_id"] = turn_id
        event["turn_phase"] = "activity"
    started = _record_started_ms(record)
    if started is not None:
        event["created_at_ms"] = started
    return event


@dataclass(frozen=True)
class _RowShape:
    """How one thread projection spells the fields recovery reads and writes."""

    tool_events_key: str
    is_user: Callable[[dict[str, Any]], bool]
    created_ms_key: str
    make_item: Callable[[dict[str, Any], str, str, dict[str, Any] | None], dict[str, Any]]


_MESSAGES_SHAPE = _RowShape(
    tool_events_key="toolEvents",
    is_user=lambda row: row.get("role") == "user",
    created_ms_key="createdAt",
    make_item=lambda record, status, chat_id, _anchor: _activity_item(record, status, chat_id),
)
# Ziggy-local (MIT-1486): upstream #5819's opt-in ``?projection=events``.
_EVENTS_SHAPE = _RowShape(
    tool_events_key="tool_events",
    is_user=lambda row: row.get("event") == "user_message",
    created_ms_key="created_at_ms",
    make_item=_activity_event,
)


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
        status = _resolved_status(record, active=active, in_live_turn=active)
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
    last_turn_end_ms: int | None,
    shape: _RowShape = _MESSAGES_SHAPE,
) -> None:
    """Anchor recovered rows to the turn that actually ran the tool.

    The rows are journaled, so the replayed list also carries trace,
    reasoning and file-edit rows; a ``before_message_count`` index into
    ``session.messages`` would land early as soon as any earlier turn
    produced an extra row. Anchor by timestamp instead: each recovered call
    belongs to the turn whose user row is the latest one at or before the
    call started, so a call from a crashed turn renders under that turn's
    user row even after a newer turn has been appended. A call that predates
    every user row on this page belongs to a turn the journal lost and is
    dropped rather than re-anchored onto a surviving turn. When the page's
    user rows carry no usable timestamp (a hand-built payload), fall back to
    the open turn and lean on ``last_turn_end_ms`` to drop stale records.
    """
    user_positions: list[int] = [
        index
        for index, raw_message in enumerate(messages)
        if isinstance(raw_message, dict)
        and shape.is_user(cast(dict[str, Any], raw_message))
    ]
    if not user_positions:
        # The page does not host the turn that ran the tool; its rows belong
        # to the page that does.
        return
    last_user = user_positions[-1]
    dated = [
        (index, _valid_created_ms(cast(dict[str, Any], messages[index]).get(shape.created_ms_key)))
        for index in user_positions
    ]
    rows_are_dated = any(created is not None for _, created in dated)
    # A session with no journaled ``turn_end`` at all is mid its first (open)
    # turn: every unjournaled record belongs to that one live turn, so anchor
    # there directly. Only once a turn has closed do we need timestamps to tell
    # a still-recoverable crashed-tail call apart from a stale older record.
    first_turn_open = last_turn_end_ms is None
    groups: dict[int, list[dict[str, Any]]] = {}
    for record in recoverable:
        started = _record_started_ms(record)
        anchor: int | None = last_user
        if not first_turn_open and rows_are_dated and started is not None:
            anchor = None
            for index, created in dated:
                if created is None:
                    continue  # undated row: not a usable boundary, keep scanning
                if created <= started:
                    anchor = index
                else:
                    break  # user rows are chronological; later ones only start later
            if anchor is None:
                # The call predates every dated user row on this page: its turn
                # is not represented here, so do not mis-place it under a newer one.
                continue
        elif started is not None and last_turn_end_ms is not None:
            # Undated page rows: the last journaled turn_end is the only staleness
            # signal. A record at or before it belongs to a closed, journaled turn
            # (already rendered or lost) — not the open turn — so drop it.
            if not started > last_turn_end_ms:
                continue
        in_live_turn = anchor == last_user
        item = shape.make_item(
            record,
            _resolved_status(record, active=active, in_live_turn=in_live_turn),
            chat_id,
            cast(dict[str, Any], messages[anchor]),
        )
        groups.setdefault(anchor, []).append(item)
    for anchor in sorted(groups, reverse=True):
        messages[anchor + 1 : anchor + 1] = groups[anchor]


def project_activity_history(
    payload: dict[str, Any],
    *,
    active: bool,
    is_latest_page: bool = True,
) -> None:
    """Splice unjournaled tool activity into ``payload["messages"]`` and/or
    ``payload["events"]`` (the ``?projection=events`` response, MIT-1486)."""
    stored_metadata: Any = payload.get("metadata", {})
    metadata = cast(dict[str, Any], stored_metadata) if isinstance(stored_metadata, dict) else {}
    raw_records: Any = metadata.pop(KEY, [])
    if not isinstance(raw_records, list):
        return
    records = cast(list[Any], raw_records)
    targets: list[tuple[list[Any], _RowShape]] = []
    for field, shape in (("messages", _MESSAGES_SHAPE), ("events", _EVENTS_SHAPE)):
        rows: Any = payload.get(field)
        if isinstance(rows, list):
            targets.append((cast(list[Any], rows), shape))
    journal: tuple[set[str], int | None, bool, bool] | None = None
    journal_read = False
    for rows, shape in targets:
        recoverable = _page_candidates(rows, records, shape)
        if not recoverable:
            continue
        # The latest page hosts the open turn unconditionally (it lives at the
        # tail). An older ``before=`` page hosts recovered rows only when its
        # own time range covers a record's ``started_at``; bail before reading
        # the journal when it cannot render them (MIT-1060).
        if not is_latest_page and not _page_covers_record_times(rows, recoverable, shape):
            continue
        if not journal_read:
            journal = _journaled_activity_state(str(payload.get("key", "")))
            journal_read = True
        if journal is None:
            # Without a readable journal coverage cannot be proven; leaving the
            # page untouched is safer than risking a double-rendered call.
            return
        _splice_recoverable(
            rows,
            recoverable,
            journal=journal,
            shape=shape,
            chat_id=webui_chat_id(str(payload.get("key", ""))) or "",
            # Only the latest page can hold the live turn; a record left
            # ``running`` by an older page's crashed turn is interrupted.
            active=active and is_latest_page,
        )


def _page_covers_record_times(
    rows: list[Any],
    records: list[dict[str, Any]],
    shape: _RowShape,
) -> bool:
    """Whether any record's ``started_at`` could belong to a turn this page hosts.

    The page spans its dated rows' timestamps, open-ended upwards only when
    its last dated row is a user row: that turn may have run tools whose trace
    never reached the journal (the crashed tail turn an older page scrolls
    back to). Pages with no usable timestamps cannot prove coverage, so they
    keep the journal-less bail-out.
    """
    timestamps: list[int] = []
    tail_is_user = False
    for raw_row in rows:
        if not isinstance(raw_row, dict):
            continue
        row = cast(dict[str, Any], raw_row)
        created = _valid_created_ms(row.get(shape.created_ms_key))
        if created is None:
            continue
        timestamps.append(created)
        tail_is_user = shape.is_user(row)
    if not timestamps:
        return False
    lower = min(timestamps)
    upper = max(timestamps)
    for record in records:
        started = _record_started_ms(record)
        if started is None or started < lower:
            continue
        if tail_is_user or started <= upper:
            return True
    return False


def _page_candidates(
    rows: list[Any],
    records: list[Any],
    shape: _RowShape,
) -> list[dict[str, Any]]:
    # A turn whose activity already reached the transcript journal renders its
    # own Activity rows; replayed tool events name the calls they cover.
    # Skip those records here so a recovered row never doubles up.
    page_covered: set[str] = set()
    for raw_row in rows:
        if not isinstance(raw_row, dict):
            continue
        page_covered |= _tool_event_call_ids(
            cast(dict[str, Any], raw_row).get(shape.tool_events_key)
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
    return candidates


def _splice_recoverable(
    rows: list[Any],
    candidates: list[dict[str, Any]],
    *,
    journal: tuple[set[str], int | None, bool, bool],
    shape: _RowShape,
    chat_id: str,
    active: bool,
) -> None:
    journaled, last_turn_end_ms, has_journal_lines, _open_trailing_turn = journal
    # The whole-journal call-id set (not just this page) is what keeps a call
    # journaled on an older page from being re-synthesized here; a genuinely
    # lost call is one its turn never journaled at all, which the timestamp
    # anchor below places under its own turn's user row.
    recoverable = [
        record
        for record in candidates
        if not (isinstance(record.get("call_id"), str) and record.get("call_id") in journaled)
    ]
    if not recoverable:
        return
    if not has_journal_lines and shape is _MESSAGES_SHAPE:
        # Journal-less: the replayed rows are the session messages themselves,
        # so ``before_message_count`` still addresses the right slot. Events
        # never index session messages, so they always anchor by timestamp.
        _splice_by_message_index(rows, recoverable, chat_id=chat_id, active=active)
        return
    # The open turn's tool call (or a crashed turn that never journaled its
    # trace) is recovered by anchoring each record to the user row of the
    # turn that ran it, by timestamp. Runs whether or not the trailing turn
    # is still open: a turn that crashed without a ``turn_end`` still needs
    # its rows, and they must survive later turns completing. A record that
    # predates every user row on this page belongs to a turn the journal
    # lost (it is not on this page) and is dropped rather than re-anchored.
    _splice_into_open_turn(
        rows,
        recoverable,
        chat_id=chat_id,
        active=active,
        last_turn_end_ms=last_turn_end_ms,
        shape=shape,
    )
