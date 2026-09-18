"""System-level notification for config file changes."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path

from loguru import logger
from watchfiles import Change, awatch  # pyright: ignore[reportUnknownVariableType]

ConfigChangeCallback = Callable[[], Awaitable[None] | None]


async def watch_config_file(config_path: Path, on_change: ConfigChangeCallback) -> None:
    """Notify ``on_change`` after the configured file changes.

    ``on_change`` may be a plain callable or a coroutine function; an awaitable
    result is awaited before the next batch of file events is consumed, so a
    slow handler (such as an MCP reconnect) never overlaps with itself and
    edits made while it runs arrive as one follow-up notification.  A handler
    that raises is logged and the watch continues; only cancellation stops it.
    """
    target = config_path.resolve(strict=False)

    def is_config_file(_change: Change, changed_path: str) -> bool:
        return Path(changed_path).resolve(strict=False) == target

    async for _changes in awatch(
        target.parent,
        watch_filter=is_config_file,
        recursive=False,
    ):
        try:
            result = on_change()
            if result is not None:
                await result
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Config change handler failed for {}; still watching", target)
