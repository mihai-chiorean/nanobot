"""The ``memory_explain`` tool: trace a MEMORY.md fact back to its source.

Ziggy-local (fork, MIT-1441). ``recall`` (MIT-1440) made remembered *search
hits* citable; this closes the other half: a curated long-term fact in
``memory/MEMORY.md`` carries no link to the conversation it was learned from,
so "why do you think I prefer X?" had no answer and a wrong fact had no
source to check. :meth:`MemoryStore.record_dream_provenance` records, from the
Dream run's real git diff, which conversation and message band each added
fact line came from; this tool reads that sidecar and shows the cited
messages through the same citation plumbing ``recall`` uses.

Like ``recall``, the audience boundary is resolved per call from the session
key the agent loop binds around tool execution — never from an argument — and
a source outside it is refused as a normal tool outcome, not raised into the
agent loop.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from loguru import logger

from nanobot.agent.memory import MemoryStore
from nanobot.agent.memory_index import (
    KIND_CONVERSATION,
    MAX_SNIPPET_CHARS,
    MAX_TOTAL_CHARS,
    UNTRUSTED_BANNER,
    MemoryHit,
    MemoryIndex,
    format_citation,
)
from nanobot.agent.tools.base import Tool, ToolResult
from nanobot.agent.tools.context import current_request_session_key
from nanobot.agent.tools.recall import source_is_visible, visible_sources_for

if TYPE_CHECKING:
    from pathlib import Path

    from nanobot.agent.tools.context import ToolContext

_NO_MATCH = "No MEMORY.md entry matches that phrase."
_NO_SOURCE = "This entry has no recorded source."
_OUT_OF_SCOPE = "That source is outside what this conversation can see."


class MemoryExplainTool(Tool):
    """Show where a long-term memory fact came from, with the cited messages."""

    config_key = "memory"

    @classmethod
    def enabled(cls, ctx: "ToolContext") -> bool:
        if not bool(getattr(getattr(ctx.config, "memory", None), "enable", True)):
            return False
        # Same gate as recall: without a live index the cited messages cannot
        # be resolved, and a capability that answers only "no recorded source"
        # is worse than absent.
        return cls._index_for(ctx) is not None

    @staticmethod
    def _index_for(ctx: "ToolContext") -> MemoryIndex | None:
        sessions = getattr(ctx, "sessions", None)
        indexer = getattr(sessions, "indexer", None) if sessions is not None else None
        index = getattr(indexer, "index", None)
        return index if isinstance(index, MemoryIndex) else None

    @classmethod
    def create(cls, ctx: "ToolContext") -> Tool:
        index = cls._index_for(ctx)
        if index is None:
            raise RuntimeError("memory_explain requires an attached memory index")
        memory_config = getattr(ctx.config, "memory", None)
        scope = str(getattr(memory_config, "scope", "session"))
        store = MemoryStore(_workspace_path(ctx))
        return cls(store=store, index=index, scope=scope, sessions=getattr(ctx, "sessions", None))

    def __init__(
        self,
        store: MemoryStore,
        index: MemoryIndex,
        *,
        scope: str = "session",
        sessions: Any = None,
    ) -> None:
        self._store = store
        self._index = index
        self._scope = scope if scope in {"session", "channel", "workspace"} else "session"
        # Only ever read, to turn a source key into a human title for the
        # citation line, exactly as recall does.
        self._sessions = sessions

    def _title_for(self, source: str) -> str | None:
        if not self._sessions:
            return None
        reader = getattr(self._sessions, "read_session_metadata", None)
        if not callable(reader):
            return None
        try:
            payload = reader(source)
        except Exception:  # noqa: BLE001 - provenance is best-effort, never load-bearing
            logger.debug("memory_explain: could not read metadata for {}", source)
            return None
        if not isinstance(payload, dict):
            return None
        metadata = payload.get("metadata")
        if not isinstance(metadata, dict):
            return None
        title = metadata.get("title")
        return title if isinstance(title, str) and title.strip() else None

    @property
    def name(self) -> str:
        return "memory_explain"

    @property
    def read_only(self) -> bool:
        return True

    @property
    def description(self) -> str:
        return (
            "Explain where a long-term memory fact came from. Given a phrase "
            "or a MEMORY.md line, shows that entry's recorded source: the "
            "cited messages from the conversation it was learned in, plus one "
            "message of context on each side, behind a `[source: ..., "
            "messages N–M, date]` citation line. Use it when the user asks "
            "why you believe something — 'where did you get that?', 'why do "
            "you think I prefer X?' — or before trusting a suspect fact. "
            "Facts recorded before provenance existed answer honestly that "
            "they have no recorded source."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "phrase": {
                    "type": "string",
                    "description": (
                        "A phrase from the MEMORY.md entry to explain, or the "
                        "full line. Include distinctive words — the fact's "
                        "subject, a name, a preference."
                    ),
                },
            },
            "required": ["phrase"],
        }

    def _matching_line(self, phrase: str) -> str | None:
        needle = phrase.strip().casefold()
        if not needle:
            return None
        for line in self._store.read_memory().splitlines():
            if line.strip() and needle in line.strip().casefold():
                return line.strip()
        return None

    async def execute(self, phrase: str, **kwargs: Any) -> Any:
        if not phrase or not phrase.strip():
            return ToolResult.error("Error: phrase must not be empty.")
        fact = self._matching_line(phrase)
        if fact is None:
            return _NO_MATCH
        record = self._store.find_provenance(fact)
        if not isinstance(record, dict):
            return _NO_SOURCE
        session_key = record.get("session_key")
        if not isinstance(session_key, str) or not session_key:
            return _NO_SOURCE
        cursor_start = record.get("cursor_start")
        cursor_end = record.get("cursor_end")
        if (
            not isinstance(cursor_start, int)
            or isinstance(cursor_start, bool)
            or not isinstance(cursor_end, int)
            or isinstance(cursor_end, bool)
            or cursor_start < 1
            or cursor_end < cursor_start
        ):
            logger.debug(
                "memory_explain: provenance record for {!r} carries an unusable "
                "cursor range; treating as no recorded source",
                fact,
            )
            return _NO_SOURCE

        # The audience boundary, applied before a single word of the cited
        # conversation is resolved: a provenance record is only quotable to a
        # caller that could already have recalled that conversation.
        sources, prefixes = visible_sources_for(current_request_session_key(), self._scope)
        if not source_is_visible(session_key, sources, prefixes):
            return _OUT_OF_SCOPE

        # The sidecar's cursors name the cited band; read as 1-based message
        # numbers, widened by one message of context on each side.
        band_start = cursor_start - 1
        band_end = cursor_end - 1
        windows = await asyncio.to_thread(
            self._index.windows_for_range,
            session_key,
            max(0, band_start - 1),
            band_end + 1,
            kind=KIND_CONVERSATION,
        )
        if not windows:
            title = self._title_for(session_key)
            citation = format_citation(
                MemoryHit(
                    source=session_key,
                    kind=KIND_CONVERSATION,
                    ts="",
                    body="",
                    score=0.0,
                    msg_start=band_start,
                    msg_end=band_end,
                ),
                title,
            )
            return (
                f"MEMORY.md entry: {fact}\n{citation}\n"
                "No stored conversation window covers the cited messages."
            )
        # The citation is built from the real indexed window (its ts is the
        # message date the index recorded), never from the sidecar's run-date —
        # so the date shown is when the fact was *said*, not when Dream wrote
        # the file. Same rendering recall uses for a conversation hit.
        title = self._title_for(session_key)
        citation = format_citation(windows[0], title)
        lines = [
            UNTRUSTED_BANNER,
            "",
            f"MEMORY.md entry: {fact}",
            citation,
            "",
        ]
        budget = MAX_TOTAL_CHARS
        for hit in windows:
            body = hit.body[:MAX_SNIPPET_CHARS]
            if len(body) > budget:
                body = body[:budget]
            block = [*(f"    {line}" for line in body.splitlines()), ""]
            rendered = "\n".join(block)
            budget -= len(rendered) + 1
            lines.extend(block)
            if budget <= 0:
                lines.append(
                    "(remaining context omitted: memory_explain output limit reached)"
                )
                break
        return "\n".join(lines).rstrip()


def _workspace_path(ctx: "ToolContext") -> "Path":
    from pathlib import Path

    workspace = getattr(ctx, "workspace", None)
    if not workspace:
        raise RuntimeError("memory_explain requires a workspace on the tool context")
    return Path(workspace)
