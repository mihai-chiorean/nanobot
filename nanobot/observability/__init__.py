"""Observability helpers for Ziggy / nanobot.

This package centralizes Langfuse integration. All public entry points are
fail-safe: when Langfuse is not installed or its env vars are not
configured, every helper degrades into a no-op context manager so the
agent loop keeps running normally.

See `nanobot.observability.langfuse` for the primary integration surface.
"""

from nanobot.observability.langfuse import (
    LANGFUSE_ENABLED,
    capture_trace_context,
    is_langfuse_ready,
    observe_llm_iteration,
    observe_subagent,
    observe_tool,
    observe_turn,
)

__all__ = [
    "LANGFUSE_ENABLED",
    "capture_trace_context",
    "is_langfuse_ready",
    "observe_llm_iteration",
    "observe_subagent",
    "observe_tool",
    "observe_turn",
]
