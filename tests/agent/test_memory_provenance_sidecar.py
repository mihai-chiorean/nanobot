"""MEMORY.md facts carry a source; ``memory_explain`` traces one back (MIT-1441).

A curated long-term fact in ``memory/MEMORY.md`` had no link to the conversation
it was learned from, so "where did you get that?" had no answer and a wrong fact
could not be checked at its source. The provenance is computed from the Dream
run's real git diff (ground truth already produced by code), keyed by a hash of
each added line, and stored beside the file in ``memory/provenance.jsonl`` — the
model never has to keep an inline comment. These tests pin:

* ``record_dream_provenance`` writes one record per added MEMORY.md fact, from
  the *real* ``dream_content_diff()`` output, attributed to the consuming batch,
  and never pulls in an added line from another file's hunk;
* a multi-session batch records nothing (no honest single source exists) and does
  not raise;
* ``memory_explain`` returns the cited message's text and its *message date*
  (read from the index, not the run date) — the full acceptance flow;
* a line with no sidecar record answers "no recorded source";
* a source outside the caller's bound audience is refused as a normal result,
  never leaking the text and never raising.

The fact text and the message text use deliberately different tokens (the fact
says "kestrel editor", only the message says "kestrelwatch") so that asserting
the needle appears is proof the cited message came back through the index, not
that the MEMORY.md line was echoed.
"""

from __future__ import annotations

import builtins
import os
import threading

from nanobot.agent.memory import MemoryStore, provenance_key
from nanobot.agent.memory_index import MemoryIndex, SessionRecallIndexer
from nanobot.agent.tools.context import RequestContext, request_context
from nanobot.agent.tools.memory_explain import (
    _NO_MATCH,
    _NO_SOURCE,
    _OUT_OF_SCOPE,
    MemoryExplainTool,
)
from nanobot.session.manager import SessionManager

DAY = "2026-09-21"  # fixed past date; differs from the run date (today) so a
# citation that accidentally used the record's date instead of the message's own
# timestamp fails the date assertion.
NEEDLE = "kestrelwatch"  # lives ONLY in the cited message, never in MEMORY.md
FACT = "- User prefers the kestrel editor"  # the curated line (different token)
PHRASE = "prefers the kestrel editor"  # a substring of FACT, what the caller asks about
SESSION_KEY = "telegram:provenance-room"
OTHER_SESSION = "telegram:other-room"
TITLE = "The Estuary Survey"


def _fixture_messages(needle_index: int) -> list[dict]:
    """Twelve messages with the unique needle token planted at *needle_index*.

    Every message carries the shared *DAY* timestamp prefix, so a citation that
    renders the window's stored timestamp shows that date; the only *NEEDLE* token
    in the whole fixture lives in one message, making its presence in the output
    attributable to exactly that message.
    """
    messages: list[dict] = []
    for index in range(12):
        if index == needle_index:
            content = f"I really like the {NEEDLE} approach for the estuary survey log"
        else:
            content = (
                f"routine logistics note {index} covering staffing and the weather "
                f"and the usual scheduling of the depot for the week"
            )
        role = "user" if index % 2 == 0 else "assistant"
        messages.append(
            {
                "role": role,
                "content": content,
                "timestamp": f"{DAY}T{9 + index // 6:02d}:{index % 6:02d}:00",
            }
        )
    return messages


def _index_conversation(tmp_path, name: str, session_key: str, messages: list[dict]):
    """Index *messages* for *session_key* through the real durable save path."""
    workspace = tmp_path / name / "workspace"
    workspace.mkdir(parents=True)
    manager = SessionManager(workspace, sessions_root=tmp_path / name / "sessions")
    indexer = SessionRecallIndexer(MemoryIndex(manager.sessions_dir))
    manager.set_indexer(indexer)
    session = manager.get_or_create(session_key)
    for message in messages:
        session.messages.append(dict(message))
    session.metadata["title"] = TITLE
    manager.save(session)
    return manager, indexer, workspace


def _batch(session_key: str, cursors: list[int]) -> list[dict]:
    return [
        {
            "cursor": cursor,
            "timestamp": f"{DAY} 09:00",
            "content": f"consolidated entry {cursor}",
            "session_key": session_key,
        }
        for cursor in cursors
    ]


def _write_fact_and_record(store: MemoryStore, session_key: str, cursors: list[int]) -> str:
    """Commit an empty MEMORY.md, add FACT via the file (as Dream would), and record
    its provenance from the *real* diff. Returns the diff body."""
    store.git.init()
    store.write_memory(f"# Memory\n\n{FACT}\n")
    diff_body = store.dream_content_diff()
    store.record_dream_provenance(diff_body, _batch(session_key, cursors))
    return diff_body


def _tool(store, indexer, sessions, scope: str) -> MemoryExplainTool:
    return MemoryExplainTool(store=store, index=indexer.index, sessions=sessions, scope=scope)


def _fail_provenance_reads(monkeypatch):
    """Make any *read* open of provenance.jsonl raise PermissionError, as a
    locked file or I/O error would; other opens (including the append) are
    untouched. Returns the real open so the test can restore it and prove the
    refusal was attributable to the injected failure, not a broken path."""
    real_open = builtins.open

    def guarded_open(file, mode="r", *args, **kwargs):
        if str(file).endswith("provenance.jsonl") and "r" in mode and "+" not in mode:
            raise PermissionError(13, "Permission denied: provenance.jsonl")
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded_open)
    return real_open


def _calling_from(session_key: str):
    return request_context(
        RequestContext(
            channel=session_key.split(":", 1)[0],
            chat_id="room",
            session_key=session_key,
        )
    )


# ---------------------------------------------------------------------------
# 1. record_dream_provenance writes one record per added fact, from the real diff.
# ---------------------------------------------------------------------------


def test_record_dream_provenance_from_real_dream_content_diff(tmp_path):
    """Drive it through the real ``dream_content_diff()`` output, not a hand-made diff."""
    workspace = tmp_path / "real" / "workspace"
    workspace.mkdir(parents=True)
    store = MemoryStore(workspace)

    diff_body = _write_fact_and_record(store, SESSION_KEY, [5, 6])

    # The diff really carried the added line (so the record is diff-derived, not invented).
    assert "memory/MEMORY.md" in diff_body, diff_body
    assert f"+{FACT}" in diff_body, diff_body

    records = store._read_provenance_records()
    assert len(records) == 1, records
    record = records[0]
    assert record["line"] == FACT
    assert record["key"] == provenance_key(FACT)
    assert record["session_key"] == SESSION_KEY
    assert record["cursor_start"] == 5 and record["cursor_end"] == 6


def test_provenance_sidecar_is_git_tracked(tmp_path):
    """The sidecar must ride in the same auto-commit as MEMORY.md: it is a GitStore
    tracked file, so ``git.init`` creates it even before any fact is recorded."""
    workspace = tmp_path / "tracked" / "workspace"
    workspace.mkdir(parents=True)
    store = MemoryStore(workspace)
    assert not store.provenance_file.exists()
    store.git.init()  # init only touches tracked files
    assert store.provenance_file.exists(), "memory/provenance.jsonl is not a tracked file"


def test_record_dream_provenance_only_parses_the_memory_hunk(tmp_path):
    """Negative control: a fact added to SOUL.md in the same diff must NOT gain a
    MEMORY.md provenance record — parsing stays inside the memory/MEMORY.md hunk."""
    workspace = tmp_path / "multifile" / "workspace"
    workspace.mkdir(parents=True)
    store = MemoryStore(workspace)
    store.git.init()
    store.write_soul("# Soul\n\n- A persona directive about always being terse\n")
    store.write_memory(f"# Memory\n\n{FACT}\n")
    diff_body = store.dream_content_diff()
    assert "SOUL.md" in diff_body and "memory/MEMORY.md" in diff_body, diff_body

    store.record_dream_provenance(diff_body, _batch(SESSION_KEY, [3]))

    lines = {record["line"] for record in store._read_provenance_records()}
    assert FACT in lines
    assert "- A persona directive about always being terse" not in lines, lines
    assert all(record["line"].strip() for record in store._read_provenance_records())


def test_record_dream_provenance_is_deduplicated_across_runs(tmp_path):
    """Re-recording the same line (a second run touching nothing new) adds no duplicate."""
    workspace = tmp_path / "dedup" / "workspace"
    workspace.mkdir(parents=True)
    store = MemoryStore(workspace)
    store.git.init()
    store.write_memory(f"# Memory\n\n{FACT}\n")
    diff_body = store.dream_content_diff()

    store.record_dream_provenance(diff_body, _batch(SESSION_KEY, [1]))
    store.record_dream_provenance(diff_body, _batch(SESSION_KEY, [1]))

    assert len(store._read_provenance_records()) == 1


def test_record_dream_provenance_skips_multi_session_batch(tmp_path):
    """A batch spanning two distinct session keys has no honest single source; it is
    skipped (no records written) and must not raise."""
    workspace = tmp_path / "multi" / "workspace"
    workspace.mkdir(parents=True)
    store = MemoryStore(workspace)
    store.git.init()
    store.write_memory(f"# Memory\n\n{FACT}\n")
    diff_body = store.dream_content_diff()

    batch = _batch(SESSION_KEY, [5]) + _batch(OTHER_SESSION, [6])
    store.record_dream_provenance(diff_body, batch)  # must not raise

    assert store._read_provenance_records() == []


def test_record_dream_provenance_survives_unreadable_sidecar(tmp_path, monkeypatch):
    """Reviewer case: the dedup read raising PermissionError must not raise into
    the Dream run ("Never raises into the Dream run"). The old code suppressed
    only FileNotFoundError, so any other OSError propagated to the async callers."""
    workspace = tmp_path / "unreadable" / "workspace"
    workspace.mkdir(parents=True)
    store = MemoryStore(workspace)
    store.git.init()
    store.write_memory(f"# Memory\n\n{FACT}\n")
    diff_body = store.dream_content_diff()
    assert f"+{FACT}" in diff_body, diff_body

    real_open = _fail_provenance_reads(monkeypatch)
    store.record_dream_provenance(diff_body, _batch(SESSION_KEY, [5, 6]))  # must not raise
    monkeypatch.setattr(builtins, "open", real_open)

    # Degraded, not crashed: the failed read did not stop the append either.
    records = store._read_provenance_records()
    assert [record["line"] for record in records] == [FACT]


def test_find_provenance_survives_unreadable_sidecar(tmp_path, monkeypatch):
    """memory_explain's read path never raises either: an unreadable sidecar reads
    as "no record" (the tool's honest no-source answer), and restoring the file
    shows the record was always there — the refusal was the read failure, not a
    broken write path."""
    workspace = tmp_path / "find-unreadable" / "workspace"
    workspace.mkdir(parents=True)
    store = MemoryStore(workspace)
    _write_fact_and_record(store, SESSION_KEY, [5])

    real_open = _fail_provenance_reads(monkeypatch)
    assert store.find_provenance(FACT) is None
    monkeypatch.setattr(builtins, "open", real_open)
    assert store.find_provenance(FACT) is not None


# ---------------------------------------------------------------------------
# 2. memory_explain returns the cited message's text and its message date.
# ---------------------------------------------------------------------------


async def test_memory_explain_returns_cited_message_with_context(tmp_path):
    """Full acceptance flow: a fact learned from message 5 explains back message 5's
    text and date when the caller is bound to that conversation."""
    manager, indexer, workspace = _index_conversation(
        tmp_path, "accept", SESSION_KEY, _fixture_messages(4)  # message 5 == index 4
    )
    store = MemoryStore(workspace)
    _write_fact_and_record(store, SESSION_KEY, [5])

    tool = _tool(store, indexer, manager, "session")
    with _calling_from(SESSION_KEY):
        out = str(await tool.execute(phrase=PHRASE))

    assert FACT in out, out  # the entry being explained is named
    assert NEEDLE in out, out  # the cited message's TEXT came back via the index
    assert DAY in out, out  # and its DATE is the message's, not the run's
    assert "[source:" in out, out


async def test_memory_explain_no_provenance(tmp_path):
    """A MEMORY.md line that exists but has no sidecar record says so plainly."""
    manager, indexer, workspace = _index_conversation(
        tmp_path, "nosrc", SESSION_KEY, _fixture_messages(4)
    )
    store = MemoryStore(workspace)
    store.write_memory(f"# Memory\n\n{FACT}\n")  # line present, no record recorded

    tool = _tool(store, indexer, manager, "session")
    with _calling_from(SESSION_KEY):
        out = str(await tool.execute(phrase=PHRASE))

    assert out == _NO_SOURCE


async def test_memory_explain_no_matching_line(tmp_path):
    """A phrase with no MEMORY.md line returns the honest no-match, not a source."""
    manager, indexer, workspace = _index_conversation(
        tmp_path, "nomatch", SESSION_KEY, _fixture_messages(4)
    )
    store = MemoryStore(workspace)
    store.write_memory(f"# Memory\n\n{FACT}\n")

    tool = _tool(store, indexer, manager, "session")
    with _calling_from(SESSION_KEY):
        out = str(await tool.execute(phrase="something utterly unrelated"))

    assert out == _NO_MATCH


# ---------------------------------------------------------------------------
# 3. The audience boundary is applied before any cited text is returned.
# ---------------------------------------------------------------------------


async def test_memory_explain_respects_recall_scope(tmp_path):
    """A provenance record pointing at another conversation is refused for a caller
    bound to a different session under ``session`` scope — and the SAME tool returns
    the text when bound to the real source, proving the boundary (not a broken path)
    is what refused."""
    manager, indexer, workspace = _index_conversation(
        tmp_path, "scope", OTHER_SESSION, _fixture_messages(4)
    )
    store = MemoryStore(workspace)
    _write_fact_and_record(store, OTHER_SESSION, [5])

    tool = _tool(store, indexer, manager, "session")

    # Caller bound to a different session under session scope -> refusal, no leak.
    with _calling_from(SESSION_KEY):
        refused = str(await tool.execute(phrase=PHRASE))
    assert refused == _OUT_OF_SCOPE, refused
    assert NEEDLE not in refused

    # Control: the same tool bound to the actual source returns the text.
    with _calling_from(OTHER_SESSION):
        allowed = str(await tool.execute(phrase=PHRASE))
    assert NEEDLE in allowed, allowed
    assert allowed != _OUT_OF_SCOPE

    # channel scope (same "telegram" channel) admits it, so the refusal tracks scope.
    tool_channel = _tool(store, indexer, manager, "channel")
    with _calling_from(SESSION_KEY):
        widened = str(await tool_channel.execute(phrase=PHRASE))
    assert NEEDLE in widened, widened


async def test_memory_explain_unbound_caller_is_refused(tmp_path):
    """With no bound session key (an internal/malformed turn) the tool fails closed:
    a conversation source is never quotable to an unattributed caller."""
    manager, indexer, workspace = _index_conversation(
        tmp_path, "unbound", SESSION_KEY, _fixture_messages(4)
    )
    store = MemoryStore(workspace)
    _write_fact_and_record(store, SESSION_KEY, [5])

    tool = _tool(store, indexer, manager, "session")
    out = str(await tool.execute(phrase=PHRASE))  # no request_context bound
    assert out == _OUT_OF_SCOPE, out


# ---------------------------------------------------------------------------
# 4. MIT-1591: entries for facts deleted from MEMORY.md are pruned, and the
#    dedup check + append + prune rewrite are one critical section.
# ---------------------------------------------------------------------------

FACT_B = "- The depot report ships on Fridays"  # the survivor, a distinct token


def _two_fact_store(tmp_path, name: str) -> MemoryStore:
    """A store whose MEMORY.md carries FACT + FACT_B, both recorded, as after
    a Dream run that added them; returns the store with a clean diff baseline."""
    workspace = tmp_path / name / "workspace"
    workspace.mkdir(parents=True)
    store = MemoryStore(workspace)
    store.git.init()
    store.write_memory(f"# Memory\n\n{FACT}\n{FACT_B}\n")
    diff_body = store.dream_content_diff()
    assert f"+{FACT}" in diff_body and f"+{FACT_B}" in diff_body, diff_body
    store.record_dream_provenance(diff_body, _batch(SESSION_KEY, [5, 6]))
    assert {r["line"] for r in store._read_provenance_records()} == {FACT, FACT_B}
    return store


def test_prune_removes_record_for_deleted_fact_keeps_unchanged(tmp_path):
    """Issue acceptance: delete a fact from MEMORY.md, run Dream, its provenance
    entry is gone; the entry for the unchanged fact survives. The deleting run
    adds nothing, so the prune must run on the no-additions path too."""
    store = _two_fact_store(tmp_path, "prune")

    store.write_memory(f"# Memory\n\n{FACT_B}\n")  # Dream deleted FACT
    store.record_dream_provenance(store.dream_content_diff(), _batch(SESSION_KEY, [7]))

    records = store._read_provenance_records()
    assert [record["line"] for record in records] == [FACT_B]
    assert store.find_provenance(FACT) is None  # memory_explain now says "no source"
    assert store.find_provenance(FACT_B) is not None


def test_prune_keeps_heading_only_and_blank_changes_no_record_alive(tmp_path):
    """Negative control on the prune's fact definition: growing the file with a
    heading and blank lines must NOT count as facts — the orphaned FACT entry
    still goes, FACT_B (a real line) still stays."""
    store = _two_fact_store(tmp_path, "prune-heading")

    store.write_memory(f"# Memory\n\n## Notes\n\n\n{FACT_B}\n")  # FACT gone, boilerplate grown
    store.record_dream_provenance(store.dream_content_diff(), _batch(SESSION_KEY, [7]))

    assert [r["line"] for r in store._read_provenance_records()] == [FACT_B]


def test_prune_rewrite_failure_never_raises_and_keeps_old_file(tmp_path, monkeypatch):
    """The rewrite is temp-file + fsync + replace; if the replace fails the
    Dream run must not see the error, the original sidecar must be intact, and
    no temp file may leak."""
    store = _two_fact_store(tmp_path, "rewrite-fail")
    store.write_memory(f"# Memory\n\n{FACT_B}\n")
    diff_body = store.dream_content_diff()  # computed before the fault is armed

    def failing_replace(src, dst, *args, **kwargs):
        raise OSError(5, "Input/output error")

    real_replace = os.replace
    monkeypatch.setattr(os, "replace", failing_replace)
    store.record_dream_provenance(diff_body, _batch(SESSION_KEY, [7]))
    monkeypatch.setattr(os, "replace", real_replace)

    # The failed rewrite neither raised (this line ran) nor destroyed anything:
    records = store._read_provenance_records()
    assert {record["line"] for record in records} == {FACT, FACT_B}
    assert not list(store.memory_dir.glob("*.tmp")), list(store.memory_dir.iterdir())
    # ...and once the I/O fault clears, the next run prunes as designed.
    store.record_dream_provenance(store.dream_content_diff(), _batch(SESSION_KEY, [8]))
    assert [record["line"] for record in store._read_provenance_records()] == [FACT_B]


def test_unreadable_sidecar_is_appended_to_not_rewritten(tmp_path, monkeypatch):
    """A sidecar whose READ fails must never be rewritten from the failed
    result (that is how records silently vanish): the run falls back to the
    old append, so pre-existing records survive."""
    store = _two_fact_store(tmp_path, "append-not-rewrite")
    # A brand-new fact (MEMORY.md diff vs the empty initial commit), appended
    # while every sidecar read fails.
    new_fact = "- The estuary survey uses transect seven"
    store.write_memory(f"# Memory\n\n{FACT_B}\n{new_fact}\n")
    diff_body = store.dream_content_diff()
    assert f"+{new_fact}" in diff_body, diff_body

    real_open = _fail_provenance_reads(monkeypatch)
    store.record_dream_provenance(diff_body, _batch(SESSION_KEY, [9]))  # must not raise
    monkeypatch.setattr(builtins, "open", real_open)

    lines = {record["line"] for record in store._read_provenance_records()}
    assert new_fact in lines  # the append still happened
    assert FACT in lines, "the unreadable sidecar must be kept, not rewritten away"


def _two_fact_store_prepared(tmp_path, name: str) -> MemoryStore:
    """Like _two_fact_store but nothing recorded yet: returns a store whose
    MEMORY.md additions are still pending in the working-tree diff."""
    workspace = tmp_path / name / "workspace"
    workspace.mkdir(parents=True)
    store = MemoryStore(workspace)
    store.git.init()
    store.write_memory(f"# Memory\n\n{FACT}\n{FACT_B}\n")
    return store


def test_concurrent_record_dream_provenance_writes_one_record_per_key(tmp_path):
    """Reviewer case (MIT-1591 #120 re-review): a manual /dream and the cron
    Dream can overlap on one store; without one lock over read+append+prune
    both threads pass the dedup check and double-append. The barrier makes the
    overlap real — without the lock both threads read an empty sidecar and
    write 2 records each."""
    store = _two_fact_store_prepared(tmp_path, "concurrent")
    diff_body = store.dream_content_diff()
    assert f"+{FACT}" in diff_body and f"+{FACT_B}" in diff_body, diff_body

    barrier = threading.Barrier(2)

    def dream() -> None:
        barrier.wait(5)
        store.record_dream_provenance(diff_body, _batch(SESSION_KEY, [5, 6]))

    threads = [threading.Thread(target=dream) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)

    records = store._read_provenance_records()
    keys = [record["key"] for record in records]
    assert len(keys) == len(set(keys)) == 2, records
    assert {record["line"] for record in records} == {FACT, FACT_B}


def _diff_for(fact: str) -> str:
    """A GitStore.summarize_working_tree-shaped diff adding one MEMORY.md line."""
    return (
        "memory/MEMORY.md: +1 -0\n"
        "```diff\n--- memory/MEMORY.md\n+++ memory/MEMORY.md\n"
        f"+{fact}\n```\n"
    )


def test_concurrent_provenance_runs_never_lose_records(tmp_path):
    """Lost-update detector for the MIT-1591 lock. Two overlapping Dream runs
    add DIFFERENT facts: without one lock over read+write, both threads read
    the empty sidecar and each full rewrite clobbers the other's record — a
    remembered fact silently loses its provenance. Repeated races so one lucky
    serialization cannot hide a missing lock."""
    for iteration in range(20):
        store = _two_fact_store_prepared(tmp_path, f"race-{iteration}")
        barrier = threading.Barrier(2)

        def dream(diff_body: str) -> None:
            barrier.wait(5)
            store.record_dream_provenance(diff_body, _batch(SESSION_KEY, [iteration]))

        threads = [
            threading.Thread(target=dream, args=(diff,))
            for diff in (_diff_for(FACT), _diff_for(FACT_B))
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)

        records = store._read_provenance_records()
        lines = {record["line"] for record in records}
        assert lines == {FACT, FACT_B}, (iteration, records)
