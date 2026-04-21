"""Memory system for persistent agent memory."""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

from nanobot.utils.helpers import ensure_dir

if TYPE_CHECKING:
    from nanobot.providers.base import LLMProvider
    from nanobot.session.manager import Session


_SAVE_MEMORY_TOOL = [
    {
        "type": "function",
        "function": {
            "name": "save_memory",
            "description": "Save the memory consolidation result to persistent storage.",
            "parameters": {
                "type": "object",
                "properties": {
                    "history_entry": {
                        "type": "string",
                        "description": "A paragraph (2-5 sentences) summarizing key events/decisions/topics. "
                        "Start with [YYYY-MM-DD HH:MM]. Include detail useful for grep search.",
                    },
                    "memory_update": {
                        "type": "string",
                        "description": "Full updated long-term memory as markdown. Include all existing "
                        "facts plus new ones. Return unchanged if nothing new.",
                    },
                },
                "required": ["history_entry", "memory_update"],
            },
        },
    }
]


class MemoryStore:
    """Two-layer memory: MEMORY.md (long-term facts) + HISTORY.md (grep-searchable log).

    After each consolidation the same data is also pushed into the RAG store
    so it can be retrieved later via semantic search (the ``recall`` tool).
    ChromaDB is imported lazily; if it is not installed the RAG step is silently
    skipped so the existing memory system is never broken.
    """

    def __init__(self, workspace: Path):
        self._workspace = workspace
        self.memory_dir = ensure_dir(workspace / "memory")
        self.memory_file = self.memory_dir / "MEMORY.md"
        self.history_file = self.memory_dir / "HISTORY.md"
        self._rag_store = None

    # ------------------------------------------------------------------
    # Internal: lazy RAGStore accessor (cached)
    # ------------------------------------------------------------------

    def _get_rag(self):
        """Return a RAGStore instance, or None if chromadb is unavailable."""
        if self._rag_store is not None:
            return self._rag_store
        try:
            from nanobot.agent.rag import RAGStore
            self._rag_store = RAGStore(self._workspace)
            return self._rag_store
        except Exception as exc:
            logger.debug("RAG unavailable ({}), skipping semantic indexing", exc)
            return None

    # ------------------------------------------------------------------
    # Public read/write helpers (unchanged interface)
    # ------------------------------------------------------------------

    def read_long_term(self) -> str:
        if self.memory_file.exists():
            return self.memory_file.read_text(encoding="utf-8")
        return ""

    def write_long_term(self, content: str) -> None:
        """Atomic write: temp file + fsync + rename."""
        fd, tmp = tempfile.mkstemp(
            dir=str(self.memory_dir), suffix=".tmp", prefix=".memory_"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.memory_file)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def append_history(self, entry: str) -> None:
        with open(self.history_file, "a", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                f.write(entry.rstrip() + "\n\n")
                f.flush()
                os.fsync(f.fileno())
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    def get_memory_context(self) -> str:
        long_term = self.read_long_term()
        return f"## Long-term Memory\n{long_term}" if long_term else ""

    # ------------------------------------------------------------------
    # Consolidation
    # ------------------------------------------------------------------

    async def consolidate(
        self,
        session: Session,
        provider: LLMProvider,
        model: str,
        *,
        archive_all: bool = False,
        memory_window: int = 50,
    ) -> bool:
        """Consolidate old messages into MEMORY.md + HISTORY.md via LLM tool call.

        After a successful consolidation the same messages and extracted facts
        are also pushed into the RAG semantic store.

        Returns True on success (including no-op), False on failure.
        """
        if archive_all:
            old_messages = session.messages
            keep_count = 0
            logger.info("Memory consolidation (archive_all): {} messages", len(session.messages))
        else:
            keep_count = memory_window // 2
            if len(session.messages) <= keep_count:
                return True
            if len(session.messages) - session.last_consolidated <= 0:
                return True
            old_messages = session.messages[session.last_consolidated:-keep_count]
            if not old_messages:
                return True
            logger.info("Memory consolidation: {} to consolidate, {} keep", len(old_messages), keep_count)

        lines = []
        for m in old_messages:
            if not m.get("content"):
                continue
            tools = f" [tools: {', '.join(m['tools_used'])}]" if m.get("tools_used") else ""
            lines.append(f"[{m.get('timestamp', '?')[:16]}] {m['role'].upper()}{tools}: {m['content']}")

        current_memory = self.read_long_term()
        prompt = f"""Process this conversation and call the save_memory tool with your consolidation.

## Current Long-term Memory
{current_memory or "(empty)"}

## Conversation to Process
{chr(10).join(lines)}"""

        try:
            response = await provider.chat(
                messages=[
                    {"role": "system", "content": "You are a memory consolidation agent. Call the save_memory tool with your consolidation of the conversation."},
                    {"role": "user", "content": prompt},
                ],
                tools=_SAVE_MEMORY_TOOL,
                model=model,
            )

            if not response.has_tool_calls:
                logger.warning("Memory consolidation: LLM did not call save_memory, skipping")
                return False

            args = response.tool_calls[0].arguments
            # Some providers return arguments as a JSON string instead of dict
            if isinstance(args, str):
                args = json.loads(args)
            if not isinstance(args, dict):
                logger.warning("Memory consolidation: unexpected arguments type {}", type(args).__name__)
                return False

            history_entry = args.get("history_entry", "")
            memory_update = args.get("memory_update", "")

            if history_entry:
                if not isinstance(history_entry, str):
                    history_entry = json.dumps(history_entry, ensure_ascii=False)
                # Sanitize LLM output to prevent persistent prompt injection
                from nanobot.utils.security import sanitize_input
                history_entry, _ = sanitize_input(history_entry, log_detections=False)
                self.append_history(history_entry)
            if memory_update:
                if not isinstance(memory_update, str):
                    memory_update = json.dumps(memory_update, ensure_ascii=False)
                # Sanitize LLM output to prevent persistent prompt injection
                from nanobot.utils.security import sanitize_input
                memory_update, _ = sanitize_input(memory_update, log_detections=False)
                if memory_update != current_memory:
                    self.write_long_term(memory_update)

            session.last_consolidated = 0 if archive_all else len(session.messages) - keep_count
            logger.info("Memory consolidation done: {} messages, last_consolidated={}", len(session.messages), session.last_consolidated)

            # --- RAG integration -------------------------------------------
            # Index the consolidated messages and extracted facts so they are
            # reachable via semantic search.  Failures here are non-fatal.
            self._rag_index_consolidation(
                session_id=session.key,
                messages=old_messages,
                history_entry=history_entry,
                memory_update=memory_update,
            )
            # ---------------------------------------------------------------

            return True
        except Exception:
            logger.exception("Memory consolidation failed")
            return False

    # ------------------------------------------------------------------
    # Private RAG helpers
    # ------------------------------------------------------------------

    def _rag_index_consolidation(
        self,
        session_id: str,
        messages: list[dict],
        history_entry: str,
        memory_update: str,
    ) -> None:
        """Push consolidated data into the RAG store.  Never raises."""
        rag = self._get_rag()
        if rag is None:
            return

        from datetime import datetime
        ts = datetime.now().isoformat()

        try:
            # 1. Embed the raw conversation segments.
            if messages:
                rag.add_conversation(
                    session_id=session_id,
                    messages=messages,
                    timestamp=ts,
                )

            # 2. Extract facts from history_entry + memory_update and index them.
            if history_entry or memory_update:
                rag.extract_and_store_facts(
                    history_entry=history_entry,
                    memory_update=memory_update,
                    timestamp=ts,
                )

            logger.debug("RAG: indexed consolidation for session {}", session_id)
        except Exception:
            logger.exception("RAG: indexing failed for session {} (non-fatal)", session_id)
