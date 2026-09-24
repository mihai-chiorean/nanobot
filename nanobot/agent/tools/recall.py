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

from typing import TYPE_CHECKING, Any, Literal

from loguru import logger
from pydantic import BaseModel, Field

from nanobot.agent.memory_index import (
    CURATED_SOURCES,
    KINDS,
    MemoryIndex,
    render_hits,
)
from nanobot.agent.tools.base import Tool, ToolResult
from nanobot.agent.tools.context import current_request_session_key

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
    scope: Literal["session", "channel", "workspace"] = "session"
    """How far a recall query may reach across conversations.

    One workspace holds every conversation its runtime serves. The index is
    per-tenant, but *inside* a tenant the owner's Telegram DMs, a Discord
    channel and — once shared rooms land — a room with guests in it all share
    one store. Scope is the audience boundary within that store.

    - ``session`` (default): the calling conversation's own transcript and its
      consolidation summaries, plus the curated memory files. Safe in a shared
      room, because a guest reaches only the room they are already in.
    - ``channel``: additionally every conversation on the same channel. For a
      runtime where one channel means one audience.
    - ``workspace``: everything. **Only** for a runtime where every
      conversation has the same single audience. This is true of the owner's
      gateway today and stops being true the moment shared rooms land
      (cutover memo C2) — at which point this must become audience-derived
      rather than configured.

    The default is the narrow one on purpose: a safe default must not depend
    on a feature merely being absent.
    """


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
        memory_config = getattr(ctx.config, "memory", None)
        limit = int(getattr(memory_config, "max_results", 5))
        scope = str(getattr(memory_config, "scope", "session"))
        return cls(
            index=index,
            default_limit=limit,
            scope=scope,
            sessions=getattr(ctx, "sessions", None),
        )

    def __init__(
        self,
        index: MemoryIndex,
        *,
        default_limit: int = 5,
        scope: str = "session",
        sessions: Any = None,
    ) -> None:
        self._index = index
        self._default_limit = default_limit
        self._scope = scope if scope in {"session", "channel", "workspace"} else "session"
        # Only ever read, to turn a source key into a human title for the
        # citation line. The index itself is the authority for what is searchable.
        self._sessions = sessions

    def _title_for(self, source: str) -> str | None:
        """Best human label for a source: a session title, else fall back to the key.

        Curated files and consolidation summaries have no session to name them,
        so they keep their key. Any failure to read metadata degrades to the key
        rather than dropping the hit -- a citation with a key is still provenance.
        """
        if not self._sessions or source in CURATED_SOURCES or source.startswith("history:"):
            return None
        reader = getattr(self._sessions, "read_session_metadata", None)
        if not callable(reader):
            return None
        try:
            payload = reader(source)
        except Exception:  # noqa: BLE001 - provenance is best-effort, never load-bearing
            logger.debug("recall: could not read metadata for {}", source)
            return None
        if not isinstance(payload, dict):
            return None
        metadata = payload.get("metadata")
        if not isinstance(metadata, dict):
            return None
        title = metadata.get("title")
        return title if isinstance(title, str) and title.strip() else None

    def _visible_sources(self) -> tuple[list[str] | None, list[str] | None]:
        """Resolve the audience this call may search.

        The session key comes from the per-request contextvar the agent loop
        binds around tool execution — server-side, per turn, and never from a
        tool argument, so the model cannot widen its own reach by asking. The
        tool object itself is shared across every conversation this runtime
        serves, so the boundary has to be resolved per call, not per tool.

        Fails closed: a call with no bound session key (an internal or
        malformed invocation) sees curated memory only, never another
        conversation's transcript.
        """
        if self._scope == "workspace":
            return None, None

        session_key = current_request_session_key()
        if not session_key:
            logger.warning("recall: no bound session key; restricting to curated memory")
            return list(CURATED_SOURCES), None

        sources = [*CURATED_SOURCES, session_key, f"history:{session_key}"]
        if self._scope == "channel" and ":" in session_key:
            channel = session_key.split(":", 1)[0]
            return sources, [f"{channel}:", f"history:{channel}:"]
        return sources, None

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
            "include the distinctive words the user used. Each result carries "
            "a `[source: ..., messages N–M, date]` citation line; when you "
            "state something from a result, name that source to the user "
            "instead of asserting it without one."
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
        sources, prefixes = self._visible_sources()
        try:
            hits = self._index.search(
                query,
                limit=limit or self._default_limit,
                kinds=kinds,
                sources=sources,
                source_prefixes=prefixes,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("recall: search failed")
            return ToolResult.error(f"Error searching memory: {exc}")
        return render_hits(hits, query, title_for=self._title_for)
