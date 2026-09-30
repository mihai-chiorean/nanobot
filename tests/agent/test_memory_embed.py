"""MIT-1442: the optional vector layer of the memory index.

Three contracts, per the issue:

* with the extra installed, indexing fills ``chunks_vec`` (vec0, 384 dims,
  keyed by ``chunks.id``) in the same per-workspace file;
* with the extra missing (import mocked to fail), indexing and search keep
  working through FTS5 alone — the class contract is "never raises into the
  caller", so this is not a nicety, it is the acceptance bar;
* the lazy backfill processes at most N chunks per call, so a large owner
  index cannot stall startup (MIT-1439's shape).

No test here downloads the model: vectors come from a fake embedder with
deterministic 384-dim output, and the real fastembed path is only checked
for its *offline* behaviour (uncached model => no vectors, no fetch).
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from contextlib import closing
from typing import Iterator

import pytest

from nanobot.agent import memory_embed
from nanobot.agent.memory_index import BACKFILL_PER_CALL, MemoryIndex


class FakeEmbedder:
    """Deterministic stand-in for the ONNX model. Same shape as LocalEmbedder."""

    def __init__(self, dims: int = 384) -> None:
        self.dims = dims
        self.batches: list[list[str]] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.batches.append(list(texts))
        out = []
        for text in texts:
            seed = sum(bytearray(text.encode("utf-8"))) or 1
            out.append([((seed * (i + 1)) % 1000) / 1000.0 for i in range(self.dims)])
        return out


@pytest.fixture(autouse=True)
def _clean_embedder_state(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    """The embedder singleton is process-wide; no test may leak it to another."""
    monkeypatch.delenv(memory_embed.DOWNLOAD_ENV, raising=False)
    memory_embed.reset_embedder()
    yield
    memory_embed.reset_embedder()


def _query_vec(index: MemoryIndex, sql: str) -> list[tuple]:
    sqlite_vec = pytest.importorskip(
        "sqlite_vec", reason="memory-embeddings extra required for the vector table"
    )
    with closing(sqlite3.connect(str(index.path))) as conn:
        sqlite_vec.load(conn)
        return conn.execute(sql).fetchall()


def _append_chunks(
    index: MemoryIndex, n: int, *, source: str = "memory/MEMORY.md"
) -> int:
    written = 0
    for i in range(n):
        written += index.append_text(
            source, f"deterministic chunk number {i}: the spark runs vllm", kind="fact"
        )
    return written


def _chunk_ids(index: MemoryIndex) -> list[int]:
    return [row[0] for row in _query_vec(index, "SELECT id FROM chunks ORDER BY id")]


def _vec_rowids(index: MemoryIndex) -> list[int]:
    return [
        row[0]
        for row in _query_vec(index, "SELECT rowid FROM chunks_vec ORDER BY rowid")
    ]


# ---------------------------------------------------------------------------
# 1. With the extra: indexing fills chunks_vec with 384-dim rows.
# ---------------------------------------------------------------------------


def test_indexing_fills_chunks_vec_with_384_dim_vectors(tmp_path):
    embedder = FakeEmbedder()
    memory_embed.set_embedder(embedder)
    index = MemoryIndex(tmp_path / "ws")

    assert _append_chunks(index, 10) == 10

    assert _vec_rowids(index) == _chunk_ids(index)
    assert len(_vec_rowids(index)) == 10
    assert {
        row[0] for row in _query_vec(index, "SELECT DISTINCT vec_length(embedding) FROM chunks_vec")
    } == {384}

    embedded = [text for batch in embedder.batches for text in batch]
    assert len(embedded) == 10, "every indexed window is embedded exactly once"


def test_index_messages_embeds_transcript_windows_too(tmp_path):
    memory_embed.set_embedder(FakeEmbedder())
    index = MemoryIndex(tmp_path / "ws")

    messages = [
        {"role": "user", "content": "note 1: their ARM dev box runs the model"},
        {"role": "assistant", "content": "reply 1: noted on the ARM box"},
    ]
    assert index.index_messages("discord:general", messages) > 0

    assert _chunk_ids(index)  # non-vacuous: something was actually indexed
    assert _vec_rowids(index) == _chunk_ids(index)


def test_index_text_replacement_leaves_no_orphan_vectors(tmp_path):
    memory_embed.set_embedder(FakeEmbedder())
    index = MemoryIndex(tmp_path / "ws")
    index.index_text("SOUL.md", "one " * 300, kind="fact")
    index.index_text("SOUL.md", "two " * 900, kind="fact")

    assert _chunk_ids(index)
    assert _vec_rowids(index) == _chunk_ids(index)


def test_drop_source_removes_the_vectors_too(tmp_path):
    memory_embed.set_embedder(FakeEmbedder())
    index = MemoryIndex(tmp_path / "ws")
    _append_chunks(index, 4, source="cli:direct")

    index.drop_source("cli:direct")

    assert _vec_rowids(index) == []


# ---------------------------------------------------------------------------
# 2. Without the extra: FTS5 only, no errors, same recall.
# ---------------------------------------------------------------------------


def test_indexing_and_search_survive_the_missing_extension(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    # Mock the import exactly the way a missing wheel breaks it.
    monkeypatch.setitem(sys.modules, "sqlite_vec", None)
    memory_embed.set_embedder(None)
    index = MemoryIndex(tmp_path / "ws")

    written = index.index_messages("cli:direct", [
        {"role": "user", "content": "we should pin the vllm context on the DGX Spark"},
        {"role": "assistant", "content": "pinned, the DGX Spark restarts clean"},
    ])
    assert written > 0
    assert index.stats()["chunks"] > 0

    hits = index.search("DGX Spark vllm")
    assert hits and "DGX Spark" in hits[0].body

    # No vector table exists at all — the file is a pure FTS5 index.
    with closing(sqlite3.connect(str(index.path))) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "chunks_vec" not in tables
        assert "chunks_fts" in tables


def test_embedder_is_none_when_fastembed_is_not_installed(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setitem(sys.modules, "fastembed", None)
    memory_embed.reset_embedder()
    assert memory_embed.get_embedder() is None


def test_uncached_model_never_downloads_and_degrades_to_none(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    if importlib.util.find_spec("fastembed") is None:
        pytest.skip("fastembed not installed here; real-model path is untestable")
    # Point the cache at an empty dir: the default (no
    # NANOBOT_MEMORY_EMBED_DOWNLOAD) must refuse to fetch and report "no
    # vectors", not download 130 MB mid-process.
    monkeypatch.setattr(memory_embed, "model_cache_dir", lambda: tmp_path / "cache")
    memory_embed.reset_embedder()
    assert memory_embed.get_embedder() is None
    assert not (tmp_path / "cache" / "models--BAAI--bge-small-en-v1.5").exists()


def test_dimension_mismatch_is_skipped_and_fts_keeps_working(tmp_path):
    # Negative control: an embedder that is present but wrong must not
    # poison the write path or the read path, and must not half-write rows.
    memory_embed.set_embedder(FakeEmbedder(dims=8))
    index = MemoryIndex(tmp_path / "ws")

    assert _append_chunks(index, 3) == 3
    assert _vec_rowids(index) == []

    assert index.search("spark deterministic")


# ---------------------------------------------------------------------------
# 3. The lazy backfill is bounded per call.
# ---------------------------------------------------------------------------


def test_backfill_processes_at_most_n_chunks_per_call(tmp_path):
    memory_embed.set_embedder(None)  # vectors off while chunking happens
    index = MemoryIndex(tmp_path / "ws")
    assert _append_chunks(index, 12) == 12
    assert _vec_rowids(index) == [], "nothing embeds while the model is off"

    memory_embed.set_embedder(FakeEmbedder())
    assert index.backfill_embeddings(limit=5) == 5
    assert index.backfill_embeddings(limit=5) == 5
    assert index.backfill_embeddings(limit=5) == 2  # only the remainder
    assert index.backfill_embeddings(limit=5) == 0  # idempotent when done

    assert _vec_rowids(index) == _chunk_ids(index)


def test_backfill_default_slice_bounds_a_large_index(tmp_path):
    # The contract is the vec table's fill ratio, so this test needs the
    # extension installed: without it _embed_new is a no-op by design and the
    # count assertion below would fail for the wrong reason.
    pytest.importorskip(
        "sqlite_vec", reason="memory-embeddings extra required for the vector table"
    )
    memory_embed.set_embedder(None)
    index = MemoryIndex(tmp_path / "ws")
    _append_chunks(index, BACKFILL_PER_CALL + 3)

    memory_embed.set_embedder(FakeEmbedder())
    made = index.backfill_embeddings()  # default limit
    assert made == BACKFILL_PER_CALL, "one call never embeds more than the bound"
    assert len(_vec_rowids(index)) == BACKFILL_PER_CALL


def test_search_tops_up_vectors_in_a_bounded_lazy_slice(tmp_path):
    memory_embed.set_embedder(None)
    index = MemoryIndex(tmp_path / "ws")
    _append_chunks(index, BACKFILL_PER_CALL + 3)
    memory_embed.set_embedder(FakeEmbedder())

    index.search("spark deterministic")  # first read triggers one bounded slice
    after_first = len(_vec_rowids(index))
    assert 0 < after_first <= BACKFILL_PER_CALL

    index.search("spark deterministic")  # throttled: no second slice
    assert len(_vec_rowids(index)) == after_first
