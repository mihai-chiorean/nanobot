"""Tool registry for dynamic tool management."""

import time
from typing import Any

from nanobot.agent.tools.base import Tool


class ToolRegistry:
    """
    Registry for agent tools.

    Allows dynamic registration and execution of tools.
    """

    def __init__(self):
        self._tools: dict[str, Tool] = {}
        self._definitions_cache: list[dict[str, Any]] | None = None
        self._audit_logger = None
        self._session_id: str = ""
        self._channel: str = ""
        self._sender_id: str = ""

    def register(self, tool: Tool) -> None:
        """Register a tool."""
        self._tools[tool.name] = tool
        self._definitions_cache = None

    def unregister(self, name: str) -> None:
        """Unregister a tool by name."""
        self._tools.pop(name, None)
        self._definitions_cache = None

    def set_audit_logger(self, audit_logger) -> None:
        """Attach an AuditLogger to record all tool executions."""
        self._audit_logger = audit_logger

    def set_context(self, session_id: str, channel: str, sender_id: str = "") -> None:
        """Set session context for audit logging and access control."""
        self._session_id = session_id
        self._channel = channel
        self._sender_id = sender_id
        # Propagate sender to filesystem tools for protected-path checks
        from nanobot.agent.tools import filesystem as _fs
        _fs._current_sender_id = sender_id

    def get(self, name: str) -> Tool | None:
        """Get a tool by name."""
        return self._tools.get(name)

    def has(self, name: str) -> bool:
        """Check if a tool is registered."""
        return name in self._tools

    def get_definitions(self) -> list[dict[str, Any]]:
        """Get all tool definitions in OpenAI format."""
        if self._definitions_cache is not None:
            return self._definitions_cache
        self._definitions_cache = [tool.to_schema() for tool in self._tools.values()]
        return self._definitions_cache

    async def execute(
        self,
        name: str,
        params: dict[str, Any],
        *,
        session_id: str | None = None,
        channel: str | None = None,
    ) -> str:
        """Execute a tool by name with given parameters.

        *session_id* and *channel* are per-call overrides for audit logging,
        avoiding shared mutable state across concurrent workers.
        """
        _HINT = "\n\n[Analyze the error above and try a different approach.]"

        tool = self._tools.get(name)
        if not tool:
            return f"Error: Tool '{name}' not found. Available: {', '.join(self.tool_names)}"

        sid = session_id or self._session_id
        ch = channel or self._channel

        t0 = time.monotonic()
        try:
            errors = tool.validate_params(params)
            if errors:
                self._audit("error", name, params, t0, sid, ch, error="; ".join(errors))
                return f"Error: Invalid parameters for tool '{name}': " + "; ".join(errors) + _HINT
            result = await tool.execute(**params)
            duration_ms = (time.monotonic() - t0) * 1000
            status = "error" if isinstance(result, str) and result.startswith("Error") else "ok"
            self._audit(status, name, params, t0, sid, ch)
            self._prom_observe(name, status, duration_ms)
            if status == "error":
                return result + _HINT
            return result
        except Exception as e:
            duration_ms = (time.monotonic() - t0) * 1000
            self._audit("error", name, params, t0, sid, ch, error=str(e))
            self._prom_observe(name, "error", duration_ms)
            return f"Error executing {name}: {str(e)}" + _HINT

    def _audit(
        self,
        status: str,
        name: str,
        params: dict,
        t0: float,
        session_id: str = "",
        channel: str = "",
        error: str | None = None,
    ) -> None:
        """Log a tool execution to the audit logger if attached."""
        if self._audit_logger is None:
            return
        try:
            self._audit_logger.log(
                tool_name=name,
                arguments=params,
                result_status=status,
                session_id=session_id,
                channel=channel,
                error=error,
                duration_ms=(time.monotonic() - t0) * 1000,
            )
        except Exception:
            pass  # Audit must never crash tool execution

    @staticmethod
    def _prom_observe(tool_name: str, status: str, duration_ms: float) -> None:
        """Feed Prometheus metrics if available."""
        try:
            from nanobot.dashboard.server import _PROM_AVAILABLE
            if _PROM_AVAILABLE:
                from nanobot.dashboard.server import PROM_TOOL_CALLS, PROM_TOOL_DURATION
                PROM_TOOL_CALLS.labels(tool_name=tool_name, status=status).inc()
                PROM_TOOL_DURATION.labels(tool_name=tool_name).observe(duration_ms)
        except Exception:
            pass

    @property
    def tool_names(self) -> list[str]:
        """Get list of registered tool names."""
        return list(self._tools.keys())

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools
