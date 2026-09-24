"""Owner-chat ``message.ack`` and ``client_message_id`` de-duplication.

Ziggy-local (MIT-1402), ported from production's ``websocket.py``
(``_send_message_ack``, ``_remember_client_message``, the
``_accepted_client_messages`` LRU and ``_discard_duplicate_media``).

A client that sends ``client_message_id`` on a ``message`` frame gets exactly
one ``message.ack`` per frame with ``status`` ``accepted``, ``duplicate`` or
``rejected``. The iOS outbox resends until it sees ``accepted`` or
``duplicate``, so a resend of an accepted id must never start a second turn.
The durable :class:`ChatInboxStore` is the dedupe boundary; the in-memory LRU
is only used when there is no workspace to hold that store.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from loguru import logger

CLIENT_MESSAGE_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
MAX_ACCEPTED_CLIENT_MESSAGES = 4_096


def parse_client_message_id(raw: Any) -> tuple[bool, str | None]:
    """Return ``(valid, normalized_id)``; an absent id is valid and ``None``."""
    if raw is None:
        return True, None
    if not isinstance(raw, str) or CLIENT_MESSAGE_ID_RE.fullmatch(raw.lower()) is None:
        return False, None
    return True, raw.lower()


class AcceptedClientMessages:
    """Bounded LRU of ``(chat_id, client_message_id)`` accepted by this process."""

    def __init__(self, limit: int = MAX_ACCEPTED_CLIENT_MESSAGES) -> None:
        self._limit = limit
        self._keys: dict[tuple[str, str], None] = {}

    def __contains__(self, key: object) -> bool:
        return key in self._keys

    def remember(self, chat_id: str, client_message_id: str) -> None:
        key = (chat_id, client_message_id)
        self._keys.pop(key, None)
        self._keys[key] = None
        while len(self._keys) > self._limit:
            self._keys.pop(next(iter(self._keys)), None)

    def forget(self, chat_id: str, client_message_id: str) -> None:
        self._keys.pop((chat_id, client_message_id), None)


def message_ack_fields(
    *,
    chat_id: str,
    client_message_id: str,
    status: str,
    detail: str | None = None,
) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "chat_id": chat_id,
        "client_message_id": client_message_id,
        "status": status,
    }
    if detail:
        fields["detail"] = detail
    return fields


def discard_duplicate_media(candidate: list[str], retained: list[str]) -> None:
    """Delete media a resend saved again when the first copy is kept."""
    retained_paths = set(retained)
    for item in candidate:
        if item in retained_paths:
            continue
        try:
            Path(item).unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("Failed to remove duplicate chat media {}: {}", item, exc)
