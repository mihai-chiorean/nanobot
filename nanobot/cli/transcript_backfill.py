"""Backfill WebUI transcripts for journal-less websocket sessions (MIT-1489).

A websocket-channel session is journal-less when its session file has a
non-empty ``messages`` list but neither the active transcript
(``webui_transcript_path``) nor the legacy thread (``_legacy_webui_thread_path``)
exists on disk. The backfill regenerates the transcript from the session store
with the same row-shaping the fork rebuild uses
(``write_session_messages_as_transcript``), so a repaired session is
indistinguishable from one journaled live.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast

from loguru import logger

from nanobot.session.manager import SessionManager
from nanobot.webui.session_identity import is_webui_session_key
from nanobot.webui.transcript import (
    has_webui_transcript,
    write_session_messages_as_transcript,
)


@dataclass
class TranscriptBackfillResult:
    """Outcome counters for one backfill run over a session store."""

    scanned: int = 0
    backfilled: int = 0
    skipped_existing: int = 0
    skipped_empty: int = 0
    skipped_unreadable: int = 0
    failed: int = 0
    backfilled_keys: list[str] = field(default_factory=list)


def backfill_journalless_webui_transcripts(
    manager: SessionManager,
) -> TranscriptBackfillResult:
    """Write transcripts for websocket sessions that have messages but no journal.

    Enumeration goes through the deployed session store
    (``SessionManager.list_sessions``) and row shaping goes through
    ``write_session_messages_as_transcript``; neither is reimplemented here.
    The run is idempotent: a session whose transcript (active or legacy)
    already exists is never rewritten, and unreadable sessions are logged and
    skipped instead of aborting the run.
    """
    result = TranscriptBackfillResult()
    for info in manager.list_sessions():
        key = info.get("key")
        if not isinstance(key, str) or not is_webui_session_key(key):
            continue
        result.scanned += 1
        if has_webui_transcript(key):
            result.skipped_existing += 1
            continue
        messages = _read_session_messages(manager, key, result)
        if messages is None:
            continue
        if not messages:
            result.skipped_empty += 1
            continue
        try:
            write_session_messages_as_transcript(key, messages)
        except Exception as e:
            logger.warning("Transcript backfill: failed to write {}: {}", key, e)
            result.failed += 1
            continue
        result.backfilled += 1
        result.backfilled_keys.append(key)
    return result


def _read_session_messages(
    manager: SessionManager,
    key: str,
    result: TranscriptBackfillResult,
) -> list[dict[str, Any]] | None:
    """Return the session's messages, or ``None`` when it must be skipped."""
    try:
        data = manager.read_session_file(key)
    except Exception as e:
        logger.warning("Transcript backfill: unreadable session {}: {}", key, e)
        result.skipped_unreadable += 1
        return None
    if not isinstance(data, dict):
        logger.warning("Transcript backfill: no session file for {}; skipping", key)
        result.skipped_unreadable += 1
        return None
    raw_messages = data.get("messages")
    if not isinstance(raw_messages, list):
        logger.warning("Transcript backfill: session {} has no message list; skipping", key)
        result.skipped_unreadable += 1
        return None
    messages: list[dict[str, Any]] = []
    for item in cast(list[Any], raw_messages):
        if isinstance(item, dict):
            messages.append(cast(dict[str, Any], item))
    return messages
