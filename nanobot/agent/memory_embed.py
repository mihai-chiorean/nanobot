"""Optional local embedding backend for the memory index (Ziggy fork, MIT-1442).

One ONNX model on CPU — ``BAAI/bge-small-en-v1.5`` via
`fastembed <https://github.com/qdrant/fastembed>`_, ~130 MB, no torch —
serving the 384-dim vectors the index stores in ``chunks_vec`` (sqlite-vec,
same per-workspace SQLite file). No second service: serving embeddings from
vLLM would need a second model instance on a host where vLLM already holds
most of the memory, and per-tenant processes cannot each pin a GPU slot.

Everything here is optional and nothing here raises into a caller. The
``memory-embeddings`` extra may be absent, the model may be uncached, the
machine may be out of memory: every entry point degrades to ``None`` and the
memory index stays pure FTS5, which is the class contract there ("never
raises into the caller"). An embedding is an enhancement to recall, never a
dependency of a turn.

Two independent gates, checked once per process:

* the extra. ``import fastembed`` failing is the documented no-op.
* the weights. By default the model is **never downloaded at runtime**:
  a first-use fetch lands mid-session-save on a shared host, which is how
  MIT-1439-style startup bursts get built. Deployment pre-caches the model
  under the data dir (``models/fastembed``), or sets
  ``NANOBOT_MEMORY_EMBED_DOWNLOAD=1`` to allow the first use to fetch it.

Tenant isolation is inherited from the index: one SQLite file per workspace,
so vectors are partitioned by construction and there is no shared table and
no ``tenant_id`` filter to get wrong.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any, Sequence

from loguru import logger

from nanobot.config.paths import get_data_dir

EMBED_MODEL_NAME = "BAAI/bge-small-en-v1.5"
EMBED_DIMS = 384

# Env opt-in for downloading the model at runtime. Unset means
# ``local_files_only``: use the weights deployment cached, or no vectors.
DOWNLOAD_ENV = "NANOBOT_MEMORY_EMBED_DOWNLOAD"

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def model_cache_dir() -> Path:
    """Where fastembed keeps its model weights, under the nanobot data dir."""
    return get_data_dir() / "models" / "fastembed"


def download_allowed() -> bool:
    return os.environ.get(DOWNLOAD_ENV, "").strip().lower() in _TRUTHY


class LocalEmbedder:
    """Thin duck-typable wrapper over a loaded fastembed model.

    ``embed`` returns plain ``list[list[float]]`` (not generator, not numpy)
    so callers can serialise it straight into SQLite and so tests can hand
    :class:`MemoryIndex` a fake with the same shape.
    """

    def __init__(self, model: Any, dims: int) -> None:
        self._model = model
        self.dims = dims

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [[float(x) for x in vector] for vector in self._model.embed(list(texts))]


_lock = threading.Lock()
_embedder: Any | None = None
_attempted = False


def get_embedder() -> Any | None:
    """The process-wide embedder, or ``None`` when embeddings are unavailable.

    Lazy: importing fastembed and loading the ONNX graph (hundreds of MB of
    RSS, a second or two) happens on the first call, which is the first
    write to an index that actually wants vectors — never at import time of
    anything, and never for a tenant that never indexes.
    """
    global _embedder, _attempted
    with _lock:
        if _attempted:
            return _embedder
        _attempted = True
        try:
            from fastembed import TextEmbedding
        except ImportError:
            logger.debug(
                "memory-embeddings extra not installed; recall stays FTS5-only"
            )
            return None
        try:
            model = TextEmbedding(
                model_name=EMBED_MODEL_NAME,
                cache_dir=str(model_cache_dir()),
                local_files_only=not download_allowed(),
            )
        except Exception:
            # Missing weights (the common offline case), a bad cache, an
            # out-of-memory ONNX init: all mean "no vectors today", not
            # "fail the turn". One line, not a traceback per attempt.
            logger.opt(exception=True).warning(
                "Embedding model {} unavailable; recall stays FTS5-only",
                EMBED_MODEL_NAME,
            )
            return None
        dims = int(getattr(model, "embedding_size", 0) or EMBED_DIMS)
        if dims != EMBED_DIMS:
            logger.warning(
                "Embedding model returned {} dims, index expects {}; disabling vectors",
                dims, EMBED_DIMS,
            )
            return None
        _embedder = LocalEmbedder(model, dims)
        logger.info(
            "Memory index embeddings enabled ({} {}, cache {})",
            EMBED_MODEL_NAME, EMBED_DIMS, model_cache_dir(),
        )
        return _embedder


def set_embedder(embedder: Any | None) -> None:
    """Install an embedder (or ``None``) directly, for tests.

    A fake with ``embed(texts) -> list[list[float]]`` and ``dims`` is enough;
    the index never imports fastembed itself.
    """
    global _embedder, _attempted
    with _lock:
        _attempted = True
        _embedder = embedder


def reset_embedder() -> None:
    """Forget the cached decision so the next ``get_embedder`` retries."""
    global _embedder, _attempted
    with _lock:
        _attempted = False
        _embedder = None
