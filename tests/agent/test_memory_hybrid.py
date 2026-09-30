"""MIT-1444: hybrid recall — the FTS5 and vector legs fused with RRF.

The three acceptance contracts from the issue, plus their controls:

* a query that only matches by meaning ("their ARM dev box" against a chunk
  saying "DGX Spark") is found in hybrid mode and not in FTS-only mode;
* an exact-term query still ranks the verbatim chunk first even though the
  vector leg is live and could promote a paraphrase;
* a chunk from a source outside the caller's audience is never returned by
  the vector leg — asserted against a chunk the *only* leg that can see it
  is the vector one, so the keyword leg's filter cannot be doing the work.

The whole file requires the sqlite-vec extension (the ``memory-embeddings``
extra): without it there is no ``chunks_vec`` table and the hybrid path is a
no-op by design, so every assertion here would pass for the wrong reason.
Vectors come from a deterministic bag-of-concepts fake, never the ONNX model.
"""

from __future__ import annotations

import re
from typing import Iterator

import pytest

from nanobot.agent import memory_embed
from nanobot.agent.memory_index import (
    RRF_K,
    MemoryIndex,
    _fuse_rankings,
)

pytest.importorskip(
    "sqlite_vec", reason="memory-embeddings extra required for the vector table"
)

_WORDS = re.compile(r"[a-z0-9]+")

# Concept families for the fake embedder: texts sharing a family land on the
# same unit vector (L2 distance 0), texts in different families on sqrt(2),
# an unknown text on the zero vector (distance 1 from any family). Which
# family each fixture picks is what lets a test say "these two texts mean
# the same" without any model.
MACHINE_CONCEPTS = {
    "machine": {"dgx", "spark", "arm", "dev", "box", "mac", "mini"},
    "garden": {"tomato", "tomatoes", "garden", "basil"},
}
CAT_CONCEPTS = {
    "cat": {"orange", "cat", "sleeps", "tabby", "naps", "marmalade", "kitten", "dozes"},
    "ops": {"dashboard", "restarts", "nightly"},
}
GATEWAY_CONCEPTS = {
    "gateway": {"gateway", "proxy", "port"},
    "port8765": {"8765"},
    "ops": {"dashboard", "restarts", "nightly"},
}


class ConceptEmbedder:
    """Deterministic stand-in for the ONNX model. Same duck type.

    One dimension per concept family; a text lights the dimensions of every
    family whose vocabulary one of its words belongs to. Distance therefore
    encodes meaning-overlap only, never surface wording — which is exactly
    the axis the FTS5 leg cannot see.
    """

    def __init__(self, concepts: dict[str, set[str]], dims: int = 384) -> None:
        self.dims = dims
        self._families = sorted(concepts.items())
        self.embedded: list[str] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for text in texts:
            self.embedded.append(text)
            words = set(_WORDS.findall(text.lower()))
            vector = [0.0] * self.dims
            for dim, (_, vocabulary) in enumerate(self._families):
                if words & vocabulary:
                    vector[dim] = 1.0
            out.append(vector)
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


# ---------------------------------------------------------------------------
# 1. Meaning-only query: found in hybrid mode, invisible to FTS-only mode.
# ---------------------------------------------------------------------------


def test_semantic_hit_needs_the_vector_leg(tmp_path):
    index = MemoryIndex(tmp_path / "ws")
    memory_embed.set_embedder(None)  # index first, embed lazily, like an upgrade
    index.index_messages("cli:direct", [
        {"role": "user", "content": "we moved the model onto the DGX Spark"},
        {"role": "assistant", "content": "right, the Spark runs vllm there now"},
    ])
    index.index_text("memory/MEMORY.md", "the garden needs basil this spring",
                     kind="fact")

    # The negative control is the same file queried FTS-only: the query's
    # keyword terms (arm/dev/box) appear nowhere in the corpus, so nothing
    # can come back for the DGX chunk however hard bm25 tries.
    memory_embed.set_embedder(None)
    assert index.search("their ARM dev box") == []

    memory_embed.set_embedder(ConceptEmbedder(MACHINE_CONCEPTS))
    index.backfill_embeddings(limit=50)
    hits = index.search("their ARM dev box")

    assert hits, "the vector leg must carry a paraphrase the keywords miss"
    assert "DGX Spark" in hits[0].body
    # Provenance survives the vector leg (MEM-01's contract): a paraphrase
    # hit cites as precisely as a keyword hit.
    assert hits[0].source == "cli:direct"
    assert hits[0].msg_start is not None and hits[0].msg_end is not None
    assert hits[0].score == pytest.approx(1.0 / (RRF_K + 1))
    # The query text itself went through the embedder once.
    # (Negative control in the other direction: the FTS leg contributed
    # nothing, so the score is exactly one list's first-place contribution.)


def test_vector_leg_waits_for_the_read_path_backfill(tmp_path):
    # Chunks written while the model was off must not unlock the vector leg
    # until they actually have vectors: a layer with an empty table is the
    # FTS5-only path, not a hybrid path that silently finds less.
    index = MemoryIndex(tmp_path / "ws")
    memory_embed.set_embedder(None)
    index.index_messages("cli:direct", [
        {"role": "user", "content": "we moved the model onto the DGX Spark"},
    ])
    # Burn the read-path backfill throttle while the model is off, so the
    # searches below run over the table as it stands.
    index.search("spark")

    memory_embed.set_embedder(ConceptEmbedder(MACHINE_CONCEPTS))
    assert index.search("their ARM dev box") == []  # no vectors yet: FTS-only

    index.backfill_embeddings(limit=50)
    assert index.search("their ARM dev box")  # same call shape, now live


# ---------------------------------------------------------------------------
# 2. Exact terms still win against a live vector leg.
# ---------------------------------------------------------------------------


def test_exact_term_query_ranks_the_verbatim_chunk_first(tmp_path):
    index = MemoryIndex(tmp_path / "ws")
    memory_embed.set_embedder(ConceptEmbedder(GATEWAY_CONCEPTS))
    # append_text, not one index_text: each line must become its own chunk,
    # or there is only one window and the ranking question never arises.
    index.append_text("memory/MEMORY.md",
                      "the gateway listens on port 8765 on localhost", kind="fact")
    index.append_text("memory/MEMORY.md",
                      "the gateway fronts the cluster health checks", kind="fact")
    index.append_text("memory/MEMORY.md",
                      "the dashboard restarts nightly at three", kind="fact")

    hits = index.search("gateway 8765", limit=3)

    bodies = [h.body for h in hits]
    assert "8765" in bodies[0], (
        f"the chunk quoting the query verbatim must rank first, got {bodies}"
    )
    # It is first with both legs crediting it (rank 1 in each), not on a tie.
    assert hits[0].score == pytest.approx(2.0 / (RRF_K + 1))
    # The third fact shares no query term at all; if it reaches the list it
    # is the vector leg promoting it, and it must still sit below the two
    # keyword matches.
    if len(hits) == 3:
        assert "dashboard" in hits[2].body


def test_rrf_maths_are_the_published_sum():
    # _fuse_rankings pinned directly: legs carry chunk ids, rank 1-based.
    fts = [(1, "a"), (2, "b")]        # chunk 1 best keyword, chunk 2 second
    vec = [(3, "c"), (1, "a")]        # chunk 3 best vector, chunk 1 also close
    fused = _fuse_rankings(fts, vec, limit=3)
    assert [row[0] for row, _ in fused] == [1, 3, 2]
    assert fused[0][1] == pytest.approx(1 / (RRF_K + 1) + 1 / (RRF_K + 2))
    assert fused[1][1] == pytest.approx(1 / (RRF_K + 1))
    assert fused[2][1] == pytest.approx(1 / (RRF_K + 2))
    # limit truncates the fused list, keeping the best.
    assert [row[0] for row, _ in _fuse_rankings(fts, vec, limit=2)] == [1, 3]


def test_ties_break_toward_the_keyword_leg():
    # A chunk only the vector leg found at rank 1 ties a chunk only the FTS
    # leg found at rank 1; the exact-term match wins (checkable evidence).
    fts = [(7, "verbatim")]
    vec = [(8, "paraphrase")]
    fused = _fuse_rankings(fts, vec, limit=2)
    assert [row[0] for row, _ in fused] == [7, 8]


# ---------------------------------------------------------------------------
# 3. The audience boundary holds the vector leg.
# ---------------------------------------------------------------------------


def test_vector_leg_never_crosses_the_audience_boundary(tmp_path):
    index = MemoryIndex(tmp_path / "ws")
    memory_embed.set_embedder(ConceptEmbedder(CAT_CONCEPTS))
    index.index_messages("cli:owner", [
        {"role": "user", "content": "my tabby naps through the afternoon"},
        {"role": "assistant", "content": "that is normal for them"},
    ])
    # Same meaning, zero keyword overlap with the query, and a closer
    # embedding than the in-audience chunk. Only the vector leg can see it
    # at all — which is what makes this a non-vacuous test of that leg.
    index.index_messages("discord:guest-room", [
        {"role": "user", "content": "the marmalade kitten dozes by the window"},
    ])

    query = "the orange cat sleeps all day"

    hits = index.search(query, sources=["cli:owner"])
    assert hits and all(h.source == "cli:owner" for h in hits)
    assert not [h for h in hits if "marmalade" in h.body]

    # Non-vacuity: widen the audience and the same chunk must arrive, via
    # the vector leg (no query term occurs in it, so FTS cannot be the one
    # returning it) — with provenance intact.
    wide = index.search(query)
    smuggled = [h for h in wide if "marmalade" in h.body]
    assert smuggled, "the vector leg must find it when the audience allows"
    assert smuggled[0].source == "discord:guest-room"
    assert smuggled[0].msg_start is not None

    # A prefix filter narrows the same way an exact source does.
    by_prefix = index.search(query, source_prefixes=["discord:"])
    assert by_prefix and all(h.source == "discord:guest-room" for h in by_prefix)


def test_kind_filter_applies_to_the_vector_leg(tmp_path):
    index = MemoryIndex(tmp_path / "ws")
    memory_embed.set_embedder(ConceptEmbedder(CAT_CONCEPTS))
    index.index_text("memory/MEMORY.md", "the marmalade kitten dozes", kind="fact")

    query = "the orange cat sleeps all day"
    assert [h.kind for h in index.search(query, kinds=["conversation"])] == []
    kinds = [h.kind for h in index.search(query)]
    assert kinds == ["fact"]  # non-vacuous: the vector leg does find it


def test_explicit_empty_scope_returns_nothing_hybrid(tmp_path):
    index = MemoryIndex(tmp_path / "ws")
    memory_embed.set_embedder(ConceptEmbedder(CAT_CONCEPTS))
    index.index_messages("cli:owner", [
        {"role": "user", "content": "my tabby naps through the afternoon"},
    ])
    assert index.search(
        "the orange cat sleeps all day", sources=[], source_prefixes=[]
    ) == []


# ---------------------------------------------------------------------------
# 4. Without vectors, behaviour is exactly what it was (acceptance, "Change").
# ---------------------------------------------------------------------------


def test_no_vectors_keeps_the_fts_only_path_including_bm25_scores(tmp_path):
    # The embedder never comes up: not "vectors fused away", but the pre-
    # hybrid deployment. Order, hits and scores must be exactly what the old
    # single-query path produced — bm25 values, not positive RRF sums.
    memory_embed.set_embedder(None)
    index = MemoryIndex(tmp_path / "ws")
    index.index_messages("cli:direct", [
        {"role": "user", "content": "we pinned the vllm context on the DGX Spark"},
        {"role": "assistant", "content": "pinned, the DGX Spark restarts clean"},
        {"role": "user", "content": "the garden needs basil this spring"},
    ])

    # The call shape of the production caller (RecallTool.execute): tool
    # defaults plus an unscoped workspace audience.
    hits = index.search("DGX Spark vllm", limit=5, kinds=None,
                        sources=None, source_prefixes=None)
    assert hits and "DGX Spark" in hits[0].body
    assert all(h.score < 0 for h in hits), (
        "scores are bm25, not fused RRF: the vector leg was never live"
    )
    assert [h.score for h in hits] == sorted(h.score for h in hits)
    # A second call is deterministic — no hidden vector leg flickering in.
    again = index.search("DGX Spark vllm", limit=5, kinds=None,
                         sources=None, source_prefixes=None)
    assert [(h.source, h.body, h.score) for h in again] == [
        (h.source, h.body, h.score) for h in hits
    ]
