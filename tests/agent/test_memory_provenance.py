"""Recall hits name their source: conversation, message range, and time (MIT-1440).

A remembered fact is only auditable if it can be traced to where it came from --
"our September 21 conversation, messages 14-16" -- rather than being asserted by
the model with no source. Ziggy's ``MemoryIndex`` already stored per-chunk
``source``/``kind``/``ts``/``seq``; what it did not store was *which messages* of
the source a window covered, so the ``recall`` tool had nothing citable to show
the model. These tests pin the provenance through the real write path (a session
save, not a hand-inserted row) and through the real read surface (the tool's
output, not just the index), and pin that an index written under the previous
schema still opens, still searches, and renders its unknown range honestly as
``messages ?`` instead of inventing one.
"""

from __future__ import annotations

import contextlib
import sqlite3

from nanobot.agent.memory_index import (
    KIND_CONVERSATION,
    MemoryHit,
    MemoryIndex,
    SessionRecallIndexer,
    format_citation,
    render_hits,
)
from nanobot.agent.tools.context import RequestContext, request_context
from nanobot.agent.tools.recall import MemoryToolConfig, RecallTool
from nanobot.session.manager import SessionManager

# A phrase that occurs only in messages 14-16 of the fixture transcript. It is a
# single rare token so FTS ranks the window that literally contains it above any
# window that merely shares the fixture's generic filler vocabulary; the token
# spelling here is deliberately unique and must match the fixture exactly.
NEEDLE = "kestrelwatch"
# A second, unrelated rare token planted outside the 14-16 band. Its presence is
# the negative control: provenance must be computed per hit, not hard-coded to the
# bracket the feature was designed around.
OTHER_NEEDLE = "numbatwatch"
SESSION_KEY = "telegram:provenance-room"
TITLE = "The Estuary Survey"
DAY = "2026-09-21"


def _manager(tmp_path, name: str = "owner"):
    workspace = tmp_path / name / "workspace"
    workspace.mkdir(parents=True)
    manager = SessionManager(workspace, sessions_root=tmp_path / name / "sessions")
    indexer = SessionRecallIndexer(MemoryIndex(manager.sessions_dir))
    manager.set_indexer(indexer)
    return manager, indexer


def _body(index: int) -> str:
    """Filler for a non-needle message: generic prose, none of these words may
    ever become searchable, or the needle's window would stop being unique."""
    return (
        f"routine logistics note {index} covering staffing and the weather "
        f"and the usual scheduling of the depot for the week"
    )


def _fixture_messages() -> list[dict]:
    """Thirty messages; the needle phrase lives in exactly messages 14, 15 and 16,
    and the control token lives only in message 3."""
    messages: list[dict] = []
    for index in range(30):
        if index in (14, 15, 16):
            content = (
                f"we recorded the {NEEDLE} observation log entry number {index} "
                f"from the estuary survey that season"
            )
        elif index == 3:
            content = f"separately we began the {OTHER_NEEDLE} count near the old rail yard"
        else:
            content = _body(index)
        role = "user" if index % 2 == 0 else "assistant"
        messages.append(
            {
                "role": role,
                "content": content,
                # Every message carries a timestamp; the citation renders the day of
                # the window's *first* message, so this must be a real per-message
                # value, not a single constant stamped on the whole batch.
                "timestamp": f"{DAY}T{9 + index // 6:02d}:{index % 6:02d}:00",
            }
        )
    return messages


def _index_fixture(tmp_path):
    """Index the fixture through the real save path (SessionManager.save fires the
    indexer), so provenance is tested where memory is actually written."""
    manager, indexer = _manager(tmp_path)
    session = manager.get_or_create(SESSION_KEY)
    for message in _fixture_messages():
        session.messages.append(dict(message))
    session.metadata["title"] = TITLE
    manager.save(session)
    return manager, indexer


def _short_fixture_messages() -> list[dict]:
    """Thirty short messages; the needle phrase lives in exactly messages 14, 15
    and 16. Every message renders to the same 30 characters, which makes the
    windower cut the first chunk at exactly message 24 -- the review's
    "one chunk spans messages 0-24" signature case, reproduced deterministically.
    The dots are tokenizer-neutral separators, so no filler word can ever match
    a query term and the needle's window stays unique."""
    messages: list[dict] = []
    for index in range(30):
        if index in (14, 15, 16):
            base = f"{NEEDLE} log {index}"
        else:
            base = f"note {index} of the depot"
        content = base + "." * (24 - len(base))  # uniform rendered length
        messages.append(
            {
                "role": "user",
                "content": content,
                "timestamp": f"{DAY}T09:{index % 60:02d}:00",
            }
        )
    return messages


def _index_short_fixture(tmp_path, name: str = "wide"):
    """Index the short-message fixture through the real save path, so one chunk
    provably spans messages 0-24 while the needle sits only in 14-16."""
    manager, indexer = _manager(tmp_path, name)
    session = manager.get_or_create(SESSION_KEY)
    for message in _short_fixture_messages():
        session.messages.append(dict(message))
    session.metadata["title"] = TITLE
    manager.save(session)
    return manager, indexer


@contextlib.contextmanager
def _calling_from(session_key: str):
    """Bind the per-request context the agent loop binds around a tool call, so the
    tool resolves the same audience the runtime would."""
    with request_context(
        RequestContext(
            channel=session_key.split(":", 1)[0],
            chat_id="room",
            session_key=session_key,
        )
    ):
        yield


def _tool(manager, scope: str = "session") -> RecallTool:
    class _Ctx:
        config = type("_Cfg", (), {"memory": MemoryToolConfig(scope=scope)})()
        sessions = manager

    _Ctx.workspace = str(manager.workspace)
    return RecallTool.create(_Ctx())


# ---------------------------------------------------------------------------
# 1. A hit brackets the messages it came from, with the right date.
# ---------------------------------------------------------------------------


def test_a_hit_brackets_the_messages_it_came_from(tmp_path):
    _, indexer = _index_fixture(tmp_path)

    needle_hits = [h for h in indexer.index.search(NEEDLE, limit=5) if NEEDLE in h.body]
    assert needle_hits, "the needle window must be retrievable"
    for hit in needle_hits:
        # Every window that actually shows the needle text necessarily overlaps the
        # 14-16 band, so its recorded span must touch it: it cannot start after the
        # band ends nor end before the band begins.
        assert hit.msg_start is not None and hit.msg_end is not None
        assert hit.msg_start <= 16 and hit.msg_end >= 14, (hit.msg_start, hit.msg_end)
        # And the timestamp must be the day the fixture actually used, not empty.
        assert hit.ts.startswith(DAY), hit.ts
    # At least one returned window spans the whole needle passage, so a reader can
    # cite the full "messages 14-16" range rather than a fragment of it.
    assert any(
        h.msg_start <= 14 and h.msg_end >= 16 for h in needle_hits
    ), [(h.msg_start, h.msg_end) for h in needle_hits]


def test_provenance_is_per_hit_not_a_single_constant(tmp_path):
    """The negative control: a different phrase in a different message range must
    get its OWN bracket, which excludes 14-16. If provenance were hard-coded to the
    designed-for band this would falsely pass."""
    _, indexer = _index_fixture(tmp_path)

    needle_hits = [h for h in indexer.index.search(NEEDLE, limit=5) if NEEDLE in h.body]
    control_hits = [
        h for h in indexer.index.search(OTHER_NEEDLE, limit=5) if OTHER_NEEDLE in h.body
    ]
    assert needle_hits and control_hits, "both tokens must be retrievable"
    control = control_hits[0]
    # The control token lives only in message 3, so its window must not extend to
    # the needle band at 14-16; a real per-message computation keeps it below 14.
    assert control.msg_end < 14, (control.msg_start, control.msg_end)
    assert not (control.msg_start <= 14 <= control.msg_end), "control bled into the needle band"


def test_citation_formats_the_source_messages_and_time(tmp_path):
    hit = MemoryHit(
        source=SESSION_KEY,
        kind=KIND_CONVERSATION,
        ts=f"{DAY}T09:14:00",
        body="we recorded the observation log",
        score=-1.5,
        msg_start=14,
        msg_end=16,
    )
    citation = format_citation(hit, TITLE)
    assert citation.startswith("[source: ")
    assert TITLE in citation
    # The stored range is 0-based; the citation shows 1-based numbers for
    # humans (stored 14-16 renders as 15-17). The dash is an en-dash.
    assert "messages 15\u201317" in citation, citation
    assert DAY in citation, citation


def test_citation_falls_back_to_the_key_without_a_title(tmp_path):
    hit = MemoryHit(
        source=SESSION_KEY,
        kind=KIND_CONVERSATION,
        ts=f"{DAY}T09:14:00",
        body="x",
        score=-1.0,
        msg_start=14,
        msg_end=16,
    )
    assert format_citation(hit) == f"[source: {SESSION_KEY}, messages 15\u201317, {DAY}]"


# ---------------------------------------------------------------------------
# 1b. The citation names the messages that answer the query, not the whole
#     indexed chunk (MEM-01b, the review of #92: "the range covers the whole
#     indexed chunk ... the citation reads `messages 0–24` even when only
#     messages 14–16 matched").
# ---------------------------------------------------------------------------


async def test_a_wide_window_cites_the_answering_messages_not_the_whole_chunk(tmp_path):
    manager, indexer = _index_short_fixture(tmp_path)

    hits = [h for h in indexer.index.search(NEEDLE, limit=5) if NEEDLE in h.body]
    assert len(hits) == 1, [(h.msg_start, h.msg_end) for h in hits]
    hit = hits[0]
    # The review's signature, reproduced through the real write path: with
    # 30 short messages one indexed chunk spans messages 0-24, while the
    # needle phrase sits only in messages 14-16.
    assert (hit.msg_start, hit.msg_end) == (0, 24), (hit.msg_start, hit.msg_end)
    # Pre-fix, this rendered the 0-based whole chunk ("messages 0-24"). The
    # full range is still available -- 1-based -- when nothing narrows, but
    # a hit whose body carries the terms must cite the band, not the chunk.
    assert "messages 0\u201324" not in format_citation(hit, TITLE, NEEDLE)
    assert "messages 1\u201325" in format_citation(hit, TITLE)
    assert "messages 15\u201317" in format_citation(hit, TITLE, NEEDLE), format_citation(
        hit, TITLE, NEEDLE
    )
    # Through the tool, the same evidence reaches the model.
    with _calling_from(SESSION_KEY):
        out = str(await _tool(manager).execute(query=NEEDLE))
    assert "messages 15\u201317" in out, out
    assert "messages 1\u201325" not in out, out


def _tiny_fixture_messages(with_tool: bool) -> list[dict]:
    """Thirty user/assistant messages short enough to store as ONE chunk
    (the review's fixture shape: the whole conversation is one window whose
    stored range is wider than the needle band, so narrowing is what is
    being tested). The needle lives in 0-based 14-16. With *with_tool*, an
    assistant tool-call turn (``content=None``) plus a tool result are
    inserted at index 3, as on any tool-using session: they advance the
    transcript numbering but are never indexed, so the window's stored
    range must be able to skip them."""
    messages: list[dict] = []
    for index in range(30):
        if index in (14, 15, 16):
            base = f"{NEEDLE} log {index}"
        else:
            base = f"note {index} of depot"
        content = base + "." * max(0, 16 - len(base))  # keep the whole batch one chunk
        role = "user" if index % 2 == 0 else "assistant"
        messages.append(
            {
                "role": role,
                "content": content,
                "timestamp": f"{DAY}T09:{index % 60:02d}:00",
            }
        )
    if with_tool:
        messages.insert(
            3,
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "call_1", "name": "exec", "arguments": {}}],
                "timestamp": f"{DAY}T09:03:30",
            },
        )
        messages.insert(
            4,
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "content": "tool output that must never be indexed",
                "timestamp": f"{DAY}T09:03:31",
            },
        )
    return messages


def _index_tiny_fixture(tmp_path, name: str, *, with_tool: bool):
    manager, indexer = _manager(tmp_path, name)
    session = manager.get_or_create(SESSION_KEY)
    for message in _tiny_fixture_messages(with_tool):
        session.messages.append(dict(message))
    session.metadata["title"] = TITLE
    manager.save(session)
    return manager, indexer


async def test_a_tool_round_trip_inside_the_window_does_not_widen_the_citation(tmp_path):
    """The review's blocker, end to end.

    Tool messages advance the transcript numbering but are never indexed,
    so a window covering them holds fewer body segments than its stored
    range is wide. Position-counting then attributes every segment past the
    first skip to the wrong message, and the segment-count guard makes the
    citation give up and quote the whole window -- which on real sessions is
    most windows, because most windows contain a tool call. The citation
    must instead map each segment through the transcript indices the writer
    recorded, and the un-indexed traffic must not shift the answer."""
    manager, indexer = _index_tiny_fixture(tmp_path, "toolroom", with_tool=True)

    hits = [h for h in indexer.index.search(NEEDLE, limit=5) if NEEDLE in h.body]
    assert len(hits) == 1, [(h.msg_start, h.msg_end) for h in hits]
    hit = hits[0]
    # Preconditions, read from the row rather than assumed: ONE window for
    # the whole conversation, its stored range counts the two un-indexed
    # tool messages (0-based 3 and 4), and the body carries no segment for
    # them -- the state that broke the positional fallback.
    assert (hit.msg_start, hit.msg_end) == (0, 31), (hit.msg_start, hit.msg_end)
    assert "tool output" not in hit.body and "exec" not in hit.body
    # The needle band moved with the insertion (0-based 16-18), so the right
    # citation is 1-based 17-19; the whole-window render (1-32), which is
    # what the un-fixed code produced, must not survive.
    narrowed = format_citation(hit, TITLE, NEEDLE)
    assert "messages 1\u201332" not in narrowed, narrowed
    assert "messages 17\u201319" in narrowed, narrowed
    with _calling_from(SESSION_KEY):
        out = str(await _tool(manager).execute(query=NEEDLE))
    assert "messages 17\u201319" in out, out
    assert "messages 1\u201332" not in out, out

    # Negative control through the same code path: the identical fixture
    # without the round-trip still narrows to its own band (the reviewer's
    # passing case), so a fix that merely disabled narrowing could not pass.
    _, control = _index_tiny_fixture(tmp_path, "toolfree", with_tool=False)
    control_hits = [h for h in control.index.search(NEEDLE, limit=5) if NEEDLE in h.body]
    assert len(control_hits) == 1, [(h.msg_start, h.msg_end) for h in control_hits]
    assert (control_hits[0].msg_start, control_hits[0].msg_end) == (0, 29)
    control_citation = format_citation(control_hits[0], TITLE, NEEDLE)
    assert "messages 15\u201317" in control_citation, control_citation


def _tiny_columns(index: MemoryIndex, source: str) -> tuple[int, int, str, str]:
    row = sqlite3.connect(str(index.path)).execute(
        "SELECT msg_start, msg_end, msg_idx, body FROM chunks WHERE source=?", (source,)
    ).fetchone()
    assert row is not None, "the fixture must leave exactly one window"
    return row


def test_the_written_column_pins_every_segment_and_skips_the_unindexed(tmp_path):
    """Same evidence in the reviewer's own terms, from the raw row rather
    than from the render: the stored index list must line up with the body's
    segments one-for-one, must keep the round-trip's transcript indices out,
    and must still bracket the range."""
    _, indexer = _index_tiny_fixture(tmp_path, "columns", with_tool=True)
    msg_start, msg_end, raw, body = _tiny_columns(indexer.index, SESSION_KEY)
    assert (msg_start, msg_end) == (0, 31), (msg_start, msg_end)
    assert raw is not None, "a row written by the current writer must carry the column"
    indices = [int(part) for part in raw.split(",")]
    # 32 transcript messages, of which the tool round-trip's two are not
    # indexed: exactly the 30 rendered parts, in order, no duplicates.
    assert len(indices) == 30 == body.count("USER: ") + body.count("ASSISTANT: ")
    assert indices == sorted(set(indices))
    assert indices[0] == msg_start and indices[-1] == msg_end
    assert 3 not in indices and 4 not in indices, indices


def _message_hit(
    body: str,
    msg_start: int,
    msg_end: int,
    msg_indices: tuple[int, ...] | None = None,
) -> MemoryHit:
    return MemoryHit(
        source=SESSION_KEY,
        kind=KIND_CONVERSATION,
        ts=f"{DAY}T09:00:00",
        body=body,
        score=-1.0,
        msg_start=msg_start,
        msg_end=msg_end,
        msg_indices=msg_indices,
    )


def test_citation_falls_back_when_the_stored_indices_disagree_with_the_body():
    body = "\n".join(
        f"USER: plain note {i}" + (" kestrelwatch" if i == 6 else "") for i in range(9)
    )
    # Nine segments, but the row's index list claims seven -- the column and
    # the body disagree, so nothing can be attributed and the full range is
    # quoted. A misaligned column must degrade to the honest wide range,
    # never to a confidently wrong band.
    citation = format_citation(_message_hit(body, 40, 48, tuple(range(40, 47))), TITLE, "kestrelwatch")
    assert "messages 41\u201349" in citation, citation
    assert "messages 47\u201347" not in citation, citation


def test_citation_ignores_an_endpoint_forged_index_list_and_keeps_the_range_honest():
    body = "\n".join(
        f"USER: plain note {i}" + (" kestrelwatch" if i == 6 else "") for i in range(9)
    )
    # Right length, right start, but the last entry claims a message past
    # the window's stored end: the list no longer describes this row, so it
    # buys nothing and the full stored range is quoted. Accepting it would
    # let a tampered column steer the citation onto messages the window
    # never held.
    forged = (40, 41, 42, 43, 44, 45, 46, 47, 55)
    citation = format_citation(_message_hit(body, 40, 48, forged), TITLE, "kestrelwatch")
    assert "messages 41\u201349" in citation, citation
    assert "messages 47\u201347" not in citation, citation
    assert "messages 56\u201356" not in citation, citation


def test_citation_narrows_through_the_column_when_it_agrees_with_the_body():
    body = "\n".join(
        f"USER: plain note {i}" + (" kestrelwatch" if i == 6 else "") for i in range(9)
    )
    # The positive control for the two guards above: the same body and range,
    # with the index list the writer would have stored (9 transcript messages,
    # numbers starting at 40). The narrow citation is computed through the
    # column, so this pins the happy path, not just the fallbacks.
    aligned = tuple(range(40, 49))
    citation = format_citation(_message_hit(body, 40, 48, aligned), TITLE, "kestrelwatch")
    assert "messages 47\u201347" in citation, citation


def test_citation_narrows_only_to_messages_containing_the_query_terms():
    body = "\n".join(
        f"USER: routine note {i}" + (" kestrelwatch" if i == 6 else "")
        for i in range(9)
    )
    citation = format_citation(_message_hit(body, 20, 28), TITLE, "kestrelwatch")
    assert "messages 27\u201327" in citation, citation  # only message 26 (0-based) matches


def test_citation_spans_the_band_covering_all_matching_messages():
    body = "\n".join(
        (
            "USER: the kestrelwatch arrived"
            if i == 0
            else "USER: the survey closed"
            if i == 2
            else f"USER: quiet stretch {i}"
        )
        for i in range(7)
    )
    citation = format_citation(_message_hit(body, 100, 106), TITLE, "kestrelwatch survey")
    assert "messages 101\u2013103" in citation, citation


def test_citation_falls_back_to_the_full_range_when_no_term_matches_a_message():
    body = "\n".join(f"USER: had to mend fence {i}" for i in range(8))
    # A porter stem match ("mending" matching the indexed "mend") leaves every
    # message term-unmatched, exactly like a future pure-semantic hit: the
    # citation degrades to the full 1-based range, never to a wrong band.
    citation = format_citation(_message_hit(body, 40, 47), TITLE, "mending")
    assert "messages 41\u201348" in citation, citation


def test_citation_keeps_the_full_range_for_narrow_windows():
    # At or under the span threshold the whole window is already a handful of
    # messages: cite it complete, not the one message that happens to match.
    body = "\n".join(
        f"ASSISTANT: quick reply {i}" + (" kestrelwatch" if i == 2 else "")
        for i in range(4)
    )
    citation = format_citation(_message_hit(body, 3, 6), TITLE, "kestrelwatch")
    assert "messages 4\u20137" in citation, citation


def test_citation_narrows_a_tail_window_from_its_own_band():
    # A window at the tail of a long conversation must narrow within its own
    # stored coordinates, never collapse onto the head of the transcript.
    body = "\n".join(
        f"USER: note {i}" + (" kestrelwatch" if i in (6, 7) else "") for i in range(8)
    )
    citation = format_citation(_message_hit(body, 22, 29), TITLE, "kestrelwatch")
    assert "messages 29\u201330" in citation, citation


def test_citation_keeps_a_leading_fragment_with_the_message_it_continues():
    # Windows are cut on a character budget, not on message bounds, so a
    # window can begin mid-message: the unlabelled leading fragment is
    # the tail of the window's first message and must stay attributed
    # to it -- a term matching only that fragment cites the first
    # message, not the second one down.
    body = "kestrelwatch tail of the previous message\n" + "\n".join(
        f"USER: body text {i}" for i in range(6)
    )
    citation = format_citation(_message_hit(body, 0, 6), TITLE, "kestrelwatch")
    assert "messages 1\u20131" in citation, citation


def test_citation_falls_back_when_the_body_disagrees_with_the_stored_range():
    # The segment count must match the stored span, or the attribution is a
    # guess: a body with fewer rendered messages than the range claims (here,
    # a row whose text no longer lines up with msg_start..msg_end) cites the
    # full range rather than a band narrowed by position-wise matching.
    body = "\n".join(
        f"USER: plain note {i}" + (" kestrelwatch" if i == 6 else "") for i in range(7)
    )
    citation = format_citation(_message_hit(body, 0, 9), TITLE, "kestrelwatch")  # 10 claimed, 7 present
    assert "messages 1\u201310" in citation, citation
    assert "messages 7\u20137" not in citation, citation


def test_citation_ignores_role_prefixes_inside_message_text():
    # A turn whose quoted text opens with a role-like prefix is not a window
    # boundary: the extra segment makes the body disagree with the stored
    # span, and the safe answer is the full range, not a band cut on the
    # forged boundary.
    body = "\n".join(f"USER: plain note {i}" for i in range(6))
    body += "\nASSISTANT: quoted USER: fake boundary"
    citation = format_citation(_message_hit(body, 0, 5), TITLE, "fake")  # 6 claimed, 7 segments
    assert "messages 1\u20136" in citation, citation
    assert "messages 7\u20137" not in citation, citation


# ---------------------------------------------------------------------------
# 2. The recall tool's own output carries the citation line the model can repeat.
# ---------------------------------------------------------------------------


async def test_recall_tool_output_shows_the_citation_line(tmp_path):
    manager, indexer = _index_fixture(tmp_path)
    tool = _tool(manager)

    # The window the tool will cite, read from the index (not assumed): in this
    # fixture the top needle hit is the window stored over messages 10-16, a
    # 7-message chunk whose needle text sits only in messages 14-16.
    needle_hits = [h for h in indexer.index.search(NEEDLE, limit=5) if NEEDLE in h.body]
    assert needle_hits, "the needle window must be retrievable"
    top = needle_hits[0]
    assert top.msg_start is not None and top.msg_end is not None
    # Precondition for this test being about narrowing-at-all: the stored
    # window must reach strictly below the needle band, so citing the band
    # is observably different from citing the window.
    assert top.msg_start < 14, (top.msg_start, top.msg_end)
    full_range = f"messages {top.msg_start + 1}\u2013{top.msg_end + 1}"
    stale_range = f"messages {top.msg_start}\u2013{top.msg_end}"

    with _calling_from(SESSION_KEY):
        out = str(await tool.execute(query=NEEDLE))

    assert NEEDLE in out, "the recalled excerpt must still be returned"
    # The model is shown a repeatable citation naming the source, the message
    # range, and the date -- not just the body text. The range is 1-based and
    # narrowed to the messages carrying the query terms (stored 14-16 ->
    # shown 15-17); the whole-chunk render, whether 0-based or 1-based, must
    # not survive for a hit whose messages demonstrably contain the terms.
    assert "[source: " in out
    assert TITLE in out, "the session title is the human label for the source"
    assert DAY in out, "the citation must carry the right date"
    assert "messages " in out and "\u2013" in out, out
    assert "messages 15\u201317" in out, out
    assert stale_range not in out, "0-based numbering must not survive"
    assert full_range not in out, f"un-narrowed window cited: {full_range}"


async def test_recall_tool_uses_the_title_and_falls_back_to_the_key(tmp_path):
    # With a title, the citation names the conversation by its title.
    manager, _ = _index_fixture(tmp_path)
    with _calling_from(SESSION_KEY):
        titled = str(await _tool(manager).execute(query=NEEDLE))
    assert f"[source: {TITLE}," in titled, titled

    # A source with no session title -- here a curated file, whose key is all it
    # has -- is still cited by that key, so a citation is always present rather
    # than silently dropped when a human label is unavailable.
    manager2, indexer2 = _manager(tmp_path, "tenant")
    indexer2.index.index_text(
        "memory/MEMORY.md",
        f"the {NEEDLE} findings were copied verbatim into the long-term notes",
        kind="fact",
    )
    with _calling_from("telegram:some-room"):
        untitled = str(await _tool(manager2, scope="workspace").execute(query=NEEDLE))
    assert "[source: memory/MEMORY.md" in untitled, untitled


def test_the_description_tells_the_model_to_cite():
    """The instruction to cite is part of the tool contract, so it must be present."""
    manager_sentinel = object()
    tool = RecallTool(index=manager_sentinel, default_limit=5, scope="session")
    description = tool.description
    assert "cite" in description.lower() or "source" in description.lower()
    assert "messages" in description.lower()


# ---------------------------------------------------------------------------
# 3. An index written under the previous schema still opens, still searches, and
#    renders its unknown range honestly.
# ---------------------------------------------------------------------------


def test_an_index_from_the_old_schema_still_opens_and_searches(tmp_path):
    """Build a v1 database (no msg_start/msg_end columns, schema_version=1) exactly
    as the prior code wrote it, then open it with the current index."""
    directory = tmp_path / "legacy"
    directory.mkdir(parents=True)
    path = directory / ".memory-index.sqlite3"
    db = sqlite3.connect(str(path))
    db.executescript(
        """
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE sources (
            source TEXT PRIMARY KEY, kind TEXT NOT NULL,
            cursor INTEGER NOT NULL DEFAULT 0, digest TEXT, updated_at REAL NOT NULL);
        CREATE TABLE chunks (
            id INTEGER PRIMARY KEY, source TEXT NOT NULL, kind TEXT NOT NULL,
            ts TEXT, seq INTEGER NOT NULL, body TEXT NOT NULL);
        CREATE INDEX chunks_by_source ON chunks(source);
        CREATE VIRTUAL TABLE chunks_fts USING fts5(
            body, content='chunks', content_rowid='id',
            tokenize='porter unicode61 remove_diacritics 2');
        CREATE TRIGGER chunks_ai AFTER INSERT ON chunks BEGIN
            INSERT INTO chunks_fts(rowid, body) VALUES (new.id, new.body); END;
        CREATE TRIGGER chunks_ad AFTER DELETE ON chunks BEGIN
            INSERT INTO chunks_fts(chunks_fts, rowid, body) VALUES ('delete', old.id, old.body); END;
        CREATE TRIGGER chunks_au AFTER UPDATE ON chunks BEGIN
            INSERT INTO chunks_fts(chunks_fts, rowid, body) VALUES ('delete', old.id, old.body);
            INSERT INTO chunks_fts(rowid, body) VALUES (new.id, new.body); END;
        INSERT INTO meta(key, value) VALUES('schema_version', '1');
        INSERT INTO sources(source, kind, cursor, digest, updated_at)
            VALUES('webui:legacy', 'conversation', 0, NULL, 1.0);
        INSERT INTO chunks(source, kind, ts, seq, body)
            VALUES('webui:legacy', 'conversation', '2026-08-30T09:00:00', 0,
                   'the numbat survey was postponed because the rain would not lift');
        """
    )
    db.commit()
    # Precondition: the old file really lacks the columns and is stamped v1.
    cols = {row[1] for row in db.execute("PRAGMA table_info('chunks')")}
    db.close()
    assert "msg_start" not in cols and "msg_end" not in cols

    index = MemoryIndex(directory)
    try:
        hits = index.search("numbat survey postponed rain", limit=5)
        assert hits, "an index written by the old code must still be searchable"
        for hit in hits:
            # Pre-v2 rows have no recoverable range: the range is None, and the
            # citation renders it as the explicit unknown marker, never a fabricated
            # "messages 0-0" and never a crash.
            assert hit.msg_start is None and hit.msg_end is None
            assert "messages ?" in format_citation(hit, "Legacy")
        # The migration is in place: the file is stamped current and the columns exist.
        check = sqlite3.connect(str(path))
        version = check.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()[0]
        now_cols = {row[1] for row in check.execute("PRAGMA table_info('chunks')")}
        check.close()
        assert version == "3", version
        assert {"msg_start", "msg_end", "msg_idx"} <= now_cols
    finally:
        index.close()


def test_an_index_at_v2_gains_the_index_column_and_keeps_its_citations(tmp_path):
    """A v2 index (range columns, no ``msg_idx``) opens, gains the column in
    place, and its existing rows stay citable the way they were: with no
    per-segment map stored, they must still narrow positionally -- the
    reader's pre-v3 behaviour is the documented fallback for a NULL column,
    not a regression into ``messages ?``. Fresh writes after the migration
    carry the column, so the map is what later citations are computed from."""
    directory = tmp_path / "v2"
    directory.mkdir(parents=True)
    path = directory / ".memory-index.sqlite3"
    legacy_body = "\n".join(
        f"USER: plain note {i}" + (" kestrelwatch" if i == 6 else "") for i in range(8)
    )
    db = sqlite3.connect(str(path))
    db.executescript(
        """
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE sources (
            source TEXT PRIMARY KEY, kind TEXT NOT NULL,
            cursor INTEGER NOT NULL DEFAULT 0, digest TEXT, updated_at REAL NOT NULL);
        CREATE TABLE chunks (
            id INTEGER PRIMARY KEY, source TEXT NOT NULL, kind TEXT NOT NULL,
            ts TEXT, seq INTEGER NOT NULL, body TEXT NOT NULL,
            msg_start INTEGER, msg_end INTEGER);
        CREATE INDEX chunks_by_source ON chunks(source);
        CREATE VIRTUAL TABLE chunks_fts USING fts5(
            body, content='chunks', content_rowid='id',
            tokenize='porter unicode61 remove_diacritics 2');
        CREATE TRIGGER chunks_ai AFTER INSERT ON chunks BEGIN
            INSERT INTO chunks_fts(rowid, body) VALUES (new.id, new.body); END;
        CREATE TRIGGER chunks_ad AFTER DELETE ON chunks BEGIN
            INSERT INTO chunks_fts(chunks_fts, rowid, body) VALUES ('delete', old.id, old.body); END;
        CREATE TRIGGER chunks_au AFTER UPDATE ON chunks BEGIN
            INSERT INTO chunks_fts(chunks_fts, rowid, body) VALUES ('delete', old.id, old.body);
            INSERT INTO chunks_fts(rowid, body) VALUES (new.id, new.body); END;
        INSERT INTO meta(key, value) VALUES('schema_version', '2');
        """
    )
    db.execute(
        "INSERT INTO chunks(source, kind, ts, seq, body, msg_start, msg_end) "
        "VALUES(?,?,?,?,?,?,?)",
        ("webui:legacy2", "conversation", "2026-08-30T09:00:00", 0, legacy_body, 0, 7),
    )
    db.commit()
    cols = {row[1] for row in db.execute("PRAGMA table_info('chunks')")}
    db.close()
    assert "msg_idx" not in cols  # precondition: the file really predates v3

    index = MemoryIndex(directory)
    try:
        legacy_hits = [h for h in index.search("kestrelwatch", limit=5) if NEEDLE in h.body]
        assert len(legacy_hits) == 1, [(h.msg_start, h.msg_end) for h in legacy_hits]
        legacy = legacy_hits[0]
        assert (legacy.msg_start, legacy.msg_end) == (0, 7)
        assert legacy.msg_indices is None, "a pre-v3 row has no per-segment map"
        citation = format_citation(legacy, "Legacy", "kestrelwatch")
        assert "messages 7\u20137" in citation, citation  # positional, as before
        # A write through the migrated database carries the column, so new
        # citations are computed from the stored map, not from position.
        index.index_messages(
            "webui:fresh",
            [
                {
                    "role": "user",
                    "content": f"kestrelwatch fresh entry {i}",
                    "timestamp": f"{DAY}T09:{i:02d}:00",
                }
                for i in range(8)
            ],
        )
        fresh_hits = [h for h in index.search("kestrelwatch fresh", limit=5) if "fresh" in h.body]
        assert len(fresh_hits) == 1, [(h.msg_start, h.msg_end) for h in fresh_hits]
        assert fresh_hits[0].msg_indices is not None
        assert fresh_hits[0].msg_indices == tuple(range(8))
        check = sqlite3.connect(str(path))
        version = check.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()[0]
        now_cols = {row[1] for row in check.execute("PRAGMA table_info('chunks')")}
        stamped = check.execute(
            "SELECT msg_idx FROM chunks WHERE source='webui:legacy2'"
        ).fetchone()[0]
        check.close()
        assert version == "3", version
        assert "msg_idx" in now_cols
        assert stamped is None, "migration must not fabricate a map for old rows"
    finally:
        index.close()


def test_render_hits_output_stays_bounded_with_citations():
    """The citation is emitted inside the block whose budget the renderer enforces; a
    citation that grows without bound could push the prompt-injection cap out, so the
    formatted total must still respect the cap once citations are counted."""
    from nanobot.agent.memory_index import MAX_TOTAL_CHARS

    hits = [
        MemoryHit(
            source=f"discord:{i}",
            kind=KIND_CONVERSATION,
            ts=f"{DAY}T09:0{i}:00",
            body="\n".join(["line of recalled text"] * 30),
            score=-1.0,
            msg_start=1000 * i,
            msg_end=1000 * i + 40,
        )
        for i in range(10)
    ]
    rendered = render_hits(hits, "numbat", title_for=lambda source: "Some Long Conversation Title")
    # Every hit now carries an extra citation line; the cap must hold on the
    # formatted bytes, not on the bodies alone.
    assert len(rendered) < MAX_TOTAL_CHARS + 1000, len(rendered)
    assert "messages " in rendered  # citations actually rendered
