"""Hot reload and context optimization utilities."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Callable

from loguru import logger


# ---------------------------------------------------------------------------
# ConfigWatcher — polls config and skills for changes, emits reload events
# ---------------------------------------------------------------------------

class ConfigWatcher:
    """
    Watches config and skills files for changes using mtime polling.

    Monitors:
    - ~/.nanobot/config.json  — emits "config_reloaded" on change
    - ~/.nanobot/workspace/skills/ — reloads skills cache on change

    Thread-safe; call ``start()`` / ``stop()`` to manage the background thread.
    """

    _DEFAULT_CONFIG_PATH = Path.home() / ".nanobot" / "config.json"
    _DEFAULT_SKILLS_DIR = Path.home() / ".nanobot" / "workspace" / "skills"

    def __init__(
        self,
        config_path: Path | None = None,
        skills_dir: Path | None = None,
        poll_interval: float = 30.0,
        on_config_reloaded: Callable[[dict[str, Any]], None] | None = None,
        on_skills_changed: Callable[[], None] | None = None,
    ):
        self.config_path = config_path or self._DEFAULT_CONFIG_PATH
        self.skills_dir = skills_dir or self._DEFAULT_SKILLS_DIR
        self.poll_interval = poll_interval

        self._on_config_reloaded = on_config_reloaded
        self._on_skills_changed = on_skills_changed

        self._config_mtime: float = 0.0
        self._skills_mtime: float = 0.0

        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    # -- public API ---------------------------------------------------------

    def start(self) -> None:
        """Start the background polling thread."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                logger.debug("ConfigWatcher already running")
                return
            self._stop_event.clear()
            self._config_mtime = self._get_mtime(self.config_path)
            self._skills_mtime = self._get_skills_mtime()
            self._thread = threading.Thread(
                target=self._poll_loop, name="config-watcher", daemon=True,
            )
            self._thread.start()
            logger.info("ConfigWatcher started (polling every {}s)", self.poll_interval)

    def stop(self) -> None:
        """Signal the polling thread to stop and wait for it."""
        self._stop_event.set()
        with self._lock:
            thread = self._thread
            self._thread = None
        if thread is not None:
            thread.join(timeout=self.poll_interval + 5)
        logger.info("ConfigWatcher stopped")

    @property
    def running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    # -- internal -----------------------------------------------------------

    def _poll_loop(self) -> None:
        """Background loop: check mtimes, fire callbacks."""
        while not self._stop_event.is_set():
            try:
                self._check_config()
                self._check_skills()
            except Exception:
                logger.exception("ConfigWatcher poll error")
            self._stop_event.wait(timeout=self.poll_interval)

    def _check_config(self) -> None:
        mtime = self._get_mtime(self.config_path)
        if mtime and mtime != self._config_mtime:
            self._config_mtime = mtime
            logger.info("Config file changed, reloading: {}", self.config_path)
            config_data = self._load_config()
            if config_data is not None and self._on_config_reloaded:
                self._on_config_reloaded(config_data)

    def _check_skills(self) -> None:
        mtime = self._get_skills_mtime()
        if mtime and mtime != self._skills_mtime:
            self._skills_mtime = mtime
            logger.info("Skills directory changed, reloading")
            if self._on_skills_changed:
                self._on_skills_changed()

    def _load_config(self) -> dict[str, Any] | None:
        """Read and parse the config file. Returns None on error."""
        try:
            text = self.config_path.read_text(encoding="utf-8")
            return json.loads(text)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Failed to read config {}: {}", self.config_path, exc)
            return None

    @staticmethod
    def _get_mtime(path: Path) -> float:
        """Return file mtime, or 0.0 if the file doesn't exist."""
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    def _get_skills_mtime(self) -> float:
        """Return the most recent mtime across the skills directory tree."""
        if not self.skills_dir.is_dir():
            return 0.0
        latest = 0.0
        try:
            for path in self.skills_dir.rglob("*"):
                try:
                    mt = path.stat().st_mtime
                    if mt > latest:
                        latest = mt
                except OSError:
                    continue
        except OSError:
            pass
        return latest


# ---------------------------------------------------------------------------
# ContextOptimizer — pure-text context window management
# ---------------------------------------------------------------------------

class ContextOptimizer:
    """
    Utility for fitting conversation context within a token budget.

    All methods are pure text manipulation — no LLM calls involved.
    Token estimation uses the simple ``len(text) / 4`` heuristic.
    """

    # Minimum chars to keep when truncating a tool result
    _TOOL_RESULT_KEEP = 200
    _SUMMARY_PLACEHOLDER = "[earlier tool interaction omitted]"

    @staticmethod
    def estimate_tokens(text: str) -> int:
        """Rough token estimate: ``len(text) // 4``."""
        return len(text) // 4 if text else 0

    def _estimate_messages_tokens(self, messages: list[dict[str, Any]]) -> int:
        """Sum estimated tokens across all messages."""
        total = 0
        for msg in messages:
            content = msg.get("content")
            if isinstance(content, str):
                total += self.estimate_tokens(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and "text" in block:
                        total += self.estimate_tokens(block["text"])
            # Count tool call arguments
            for tc in msg.get("tool_calls", []):
                fn = tc.get("function", {})
                total += self.estimate_tokens(fn.get("arguments", ""))
        return total

    def optimize_context(
        self,
        system_prompt: str,
        messages: list[dict[str, Any]],
        max_tokens: int,
    ) -> tuple[str, list[dict[str, Any]]]:
        """
        Progressively reduce context to fit within *max_tokens*.

        Strategy (applied in order until budget is met):
            1. Truncate tool results in older messages.
            2. Collapse sequences of tool-call/tool-result pairs into summaries.
            3. Drop the oldest messages one at a time.

        Args:
            system_prompt: The system prompt text.
            messages: Conversation messages (list of dicts).
            max_tokens: Maximum token budget for the entire context.

        Returns:
            A ``(system_prompt, messages)`` tuple that fits within *max_tokens*.
            The system prompt is returned unchanged; only messages are trimmed.
        """
        system_tokens = self.estimate_tokens(system_prompt)
        budget = max_tokens - system_tokens
        if budget <= 0:
            # System prompt alone exceeds budget — nothing we can do
            return system_prompt, messages

        # Work on a shallow copy so we don't mutate the caller's list
        msgs = [dict(m) for m in messages]

        # -- Step 1: truncate tool results in older messages ----------------
        if self._estimate_messages_tokens(msgs) > budget:
            msgs = self._truncate_tool_results(msgs)

        # -- Step 2: collapse tool-call / tool-result sequences -------------
        if self._estimate_messages_tokens(msgs) > budget:
            msgs = self._collapse_tool_sequences(msgs)

        # -- Step 3: drop oldest messages -----------------------------------
        while msgs and self._estimate_messages_tokens(msgs) > budget:
            msgs.pop(0)

        return system_prompt, msgs

    # -- internal helpers ---------------------------------------------------

    def _truncate_tool_results(
        self, messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Truncate long tool-result content in older messages (keep last 4)."""
        if len(messages) <= 4:
            return messages

        result = []
        cutoff = len(messages) - 4
        for i, msg in enumerate(messages):
            if i < cutoff and msg.get("role") == "tool":
                content = msg.get("content", "")
                if isinstance(content, str) and len(content) > self._TOOL_RESULT_KEEP * 4:
                    msg = dict(msg)
                    msg["content"] = content[: self._TOOL_RESULT_KEEP] + "\n... (truncated)"
            result.append(msg)
        return result

    def _collapse_tool_sequences(
        self, messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """
        Replace older sequences of assistant(tool_calls) + tool results
        with a single summary message.

        Preserves the last 4 messages unconditionally.
        """
        if len(messages) <= 4:
            return messages

        keep_tail = messages[-4:]
        head = messages[:-4]

        collapsed: list[dict[str, Any]] = []
        i = 0
        while i < len(head):
            msg = head[i]

            # Detect assistant message with tool_calls
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                tool_names: list[str] = []
                for tc in msg["tool_calls"]:
                    fn = tc.get("function", {})
                    name = fn.get("name", "tool")
                    tool_names.append(name)

                # Skip subsequent tool-result messages that belong to this call
                j = i + 1
                while j < len(head) and head[j].get("role") == "tool":
                    j += 1

                summary_text = self._SUMMARY_PLACEHOLDER
                if tool_names:
                    summary_text = f"[used {', '.join(tool_names)} — result omitted]"

                collapsed.append({
                    "role": "assistant",
                    "content": summary_text,
                })
                i = j
            else:
                collapsed.append(msg)
                i += 1

        return collapsed + keep_tail
