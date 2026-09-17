"""The ``recall`` tool: search this workspace's own conversation history.

Ziggy-local (fork, MIT-1013). This replaces a ChromaDB-backed implementation
that never returned anything. The old version depended on a second tool,
``ingest``, which the model had to choose to call on a directory of files;
nothing ever called it, no vector store was ever created on the host, and
``recall`` answered "no results" for every tenant for the life of the
deployment without ever erroring.

Two things changed. Ingestion is now automatic — the index is written from
:meth:`SessionManager.save`, so it is current whether or not the model thinks
about memory. And ``ingest`` is gone: with automatic ingestion it has no job
left, and a model-invoked "read this directory into permanent memory" primitive
is a liability, not a feature.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from loguru import logger
from pydantic import BaseModel, Field

from nanobot.agent.memory_index import KINDS, MemoryIndex, render_hits
from nanobot.agent.tools.base import Tool, ToolResult

if TYPE_CHECKING:
    from nanobot.agent.tools.context import ToolContext


class MemoryToolConfig(BaseModel):
    """Off-switch for conversation recall.

    The fork keeps an explicit knob because the 0.3.0 upgrade removed
    ``tools.rag`` and let RAG auto-register from a bare ``import chromadb``
    (cutover memo C13). Every tenant generator that wants recall off needs a
    key that actually exists in the schema.
    """

    enable: bool = True
    max_results: int = Field(default=5, ge=1, le=10)


_SCOPES = {"all", *KINDS}


class RecallTool(Tool):
    """Keyword search over this workspace's conversations and curated memory."""

    config_key = "memory"

    @classmethod
    def enabled(cls, ctx: "ToolContext") -> bool:
        if not bool(getattr(getattr(ctx.config, "memory", None), "enable", True)):
            return False
        # Only offer the tool when a real index is attached. A capability that
        # is present but inert is worse than one that is absent: the model
        # spends a turn on it and concludes the user has no history.
        return cls._index_for(ctx) is not None

    @staticmethod
    def _index_for(ctx: "ToolContext") -> MemoryIndex | None:
        sessions = getattr(ctx, "sessions", None)
        indexer = getattr(sessions, "indexer", None) if sessions is not None else None
        index = getattr(indexer, "index", None)
        return index if isinstance(index, MemoryIndex) else None

    @classmethod
    def create(cls, ctx: "ToolContext") -> Tool:
        # The index is taken from the SessionManager this agent was built with,
        # never from a path. A tool therefore cannot be pointed at another
        # workspace's memory even if its arguments say otherwise.
        index = cls._index_for(ctx)
        if index is None:
            raise RuntimeError("recall requires an attached memory index")
        limit = int(getattr(getattr(ctx.config, "memory", None), "max_results", 5))
        return cls(index=index, default_limit=limit)

    def __init__(self, index: MemoryIndex, *, default_limit: int = 5) -> None:
        self._index = index
        self._default_limit = default_limit

    @property
    def name(self) -> str:
        return "recall"

    @property
    def read_only(self) -> bool:
        return True

    @property
    def description(self) -> str:
        return (
            "Search your own past conversations with this user, plus their "
            "long-term memory notes. Use it when they refer to something from "
            "before that is not in the current conversation — 'what did we "
            "decide about X', 'that thing I mentioned last month', a name or "
            "number you no longer have in context. Searches by keyword, so "
            "include the distinctive words the user used."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "What to look for. Include distinctive words — names, "
                        "projects, places — rather than a full sentence."
                    ),
                },
                "scope": {
                    "type": "string",
                    "enum": sorted(_SCOPES),
                    "description": (
                        "'all' (default) searches everything. 'conversation' "
                        "searches past chat turns. 'fact' searches long-term "
                        "memory notes. 'history' searches consolidation "
                        "summaries of older conversations."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": "How many excerpts to return (1-10, default 5).",
                    "minimum": 1,
                    "maximum": 10,
                },
            },
            "required": ["query"],
        }

    async def execute(
        self,
        query: str,
        scope: str = "all",
        limit: int | None = None,
        **kwargs: Any,
    ) -> str:
        if not query or not query.strip():
            return ToolResult.error("Error: query must not be empty.")
        if scope not in _SCOPES:
            return ToolResult.error(
                f"Error: invalid scope {scope!r}. Must be one of: "
                f"{', '.join(sorted(_SCOPES))}"
            )
        kinds = None if scope == "all" else [scope]
        try:
            hits = self._index.search(
                query,
                limit=limit or self._default_limit,
                kinds=kinds,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("recall: search failed")
            return ToolResult.error(f"Error searching memory: {exc}")
        return render_hits(hits, query)
