"""Display layer for "What Ziggy knows about you" (MIT-1880, design §6).

Testers cannot see what Ziggy believes about them. The beliefs live in two
files of the runtime's own workspace — ``USER.md`` (the profile) and
``memory/MEMORY.md`` (the facts Dream curated) — and each fact line already
has a stable id: :func:`nanobot.agent.memory.provenance_key`, the sha256
prefix ``memory_explain`` uses to look the line up in ``memory/provenance.jsonl``.
This module turns those files into a renderable listing: items grouped per
file, tagged with the ``##`` section they sit under, keyed by the same stable
id, and annotated with the source conversation where the sidecar records one.

Each tester's runtime is its own container, so the store this reads is
always the caller's own workspace — there is no path to another tenant's
files and the route deliberately takes no parameters.
"""

from __future__ import annotations

import re
from typing import Any, Callable

from nanobot.agent.memory import MemoryStore, provenance_key

#: Token budget of the memory core (design §6). ``core_tokens`` is reported
#: against this so the client can show "n of 2000".
MEMORY_CORE_BUDGET_TOKENS = 2000

#: The files the view shows, in display order, as (label, reader) pairs.
#: SOUL.md is deliberately absent: it is Ziggy's persona, not what Ziggy
#: remembers about the user.
_MEMORY_FILES: tuple[tuple[str, str], ...] = ("USER.md", "memory/MEMORY.md")

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_RULE_RE = re.compile(r"^[-*_]{3,}$")
_BULLET_RE = re.compile(r"^[-*+]\s+")


def _is_template_placeholder(line: str) -> bool:
    """A whole-line ``(…)`` placeholder or the italic footer of a template.

    ``nanobot/templates/memory/MEMORY.md`` ships ``(Important facts about the
    user)``-style parenthesised prompts and an italic ``*This file is …*``
    footer; an untouched ``USER.md`` carries the same shapes. They are editor
    prompts, not beliefs about the user, so they never surface as items. A
    fact that merely *contains* parentheses (``- Likes (terse) answers``) is
    not a placeholder line and stays.
    """
    if line.startswith("(") and line.endswith(")") and len(line) > 1:
        return True
    return (
        len(line) > 2 and line.startswith("*") and line.endswith("*") and not line.startswith("**")
    )


def _source_from_record(
    record: Any, title_for: Callable[[str], str | None] | None
) -> dict[str, Any] | None:
    """The ``source`` annotation for one provenance record, or None.

    Renders the same citation semantics ``memory_explain`` shows through
    :func:`nanobot.agent.memory_index.format_citation`: the message band is
    1-based (the sidecar cursors already are, and ``format_citation`` adds 1
    to its 0-based ``MemoryHit`` range), the human title falls back to the
    session key, and the date is the day component. Validation of the record
    mirrors the tool's: a missing session key or an unusable cursor range is
    no source, not a fabricated one. The tool prefers the indexed message
    timestamp for the date; the webui has no live index, so this reports the
    date the sidecar itself recorded.
    """
    if not isinstance(record, dict):
        return None
    session_key = record.get("session_key")
    if not isinstance(session_key, str) or not session_key:
        return None
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
        return None
    title = title_for(session_key) if title_for is not None else None
    if not isinstance(title, str) or not title.strip():
        title = session_key
    date = record.get("date")
    return {
        "title": title,
        "messages": f"{cursor_start}\u2013{cursor_end}",
        "date": date[:10] if isinstance(date, str) else None,
    }


def _parse_file(
    label: str,
    content: str,
    store: MemoryStore,
    *,
    title_for: Callable[[str], str | None] | None,
) -> list[dict[str, Any]]:
    """One file's memory items: every non-structural line, in file order."""
    items: list[dict[str, Any]] = []
    section: str | None = None
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or _RULE_RE.match(line) or line in {"-", "*", "+"}:
            continue
        heading = _HEADING_RE.match(line)
        if heading:
            section = heading.group(2)
            continue
        if _is_template_placeholder(line):
            continue
        item: dict[str, Any] = {
            "id": provenance_key(line),
            "file": label,
            "section": section,
            "text": _BULLET_RE.sub("", line, count=1),
        }
        source = _source_from_record(store.find_provenance(line), title_for)
        if source is not None:
            item["source"] = source
        items.append(item)
    return items


def _estimate_core_tokens(user_text: str, memory_text: str) -> int:
    """Token estimate for the memory core the two files inject.

    SM-10's shared ``count_tokens`` helper is not merged yet, so this is the
    documented fallback (``len(text) // 4``); swap it for the real counter in
    one place once SM-10 lands.
    """
    return len(f"{user_text}\n\n{memory_text}") // 4


def list_items(
    store: MemoryStore,
    *,
    title_for: Callable[[str], str | None] | None = None,
) -> dict[str, Any]:
    """Render the workspace's memory as ``{"files": [...], "core_tokens": n, "budget": n}``.

    ``title_for`` optionally maps a provenance session key to a human title,
    exactly as ``memory_explain`` does through its sessions reader; with none
    given (or none found) the citation names the session key itself.
    """
    contents = {"USER.md": store.read_user(), "memory/MEMORY.md": store.read_memory()}
    files = [
        {"file": label, "items": _parse_file(label, contents[label], store, title_for=title_for)}
        for label in _MEMORY_FILES
    ]
    return {
        "files": files,
        "core_tokens": _estimate_core_tokens(contents["USER.md"], contents["memory/MEMORY.md"]),
        "budget": MEMORY_CORE_BUDGET_TOKENS,
    }


def make_session_title_reader(sessions: Any) -> Callable[[str], str | None]:
    """Best-effort session-key → title lookup for citation labels.

    Same metadata read ``memory_explain._title_for`` performs, and just as
    there it is an enhancement, never load-bearing: a missing manager, a
    raising reader or a titleless session all simply yield no title.
    """

    def title_for(session_key: str) -> str | None:
        reader = getattr(sessions, "read_session_metadata", None) if sessions is not None else None
        if not callable(reader):
            return None
        try:
            payload = reader(session_key)
        except Exception:  # noqa: BLE001 - citation titles are best-effort
            return None
        if not isinstance(payload, dict):
            return None
        metadata = payload.get("metadata")
        if not isinstance(metadata, dict):
            return None
        title = metadata.get("title")
        return title if isinstance(title, str) and title.strip() else None

    return title_for
