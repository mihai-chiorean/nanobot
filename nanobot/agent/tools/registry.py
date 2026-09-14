"""Tool registry for dynamic tool management."""

from __future__ import annotations

import json
import re
import time
from typing import TYPE_CHECKING, Any, cast

from loguru import logger

from nanobot.agent.tools.base import Tool, ToolResult
from nanobot.agent.tools.context import ContextAware, current_request_context
# Ziggy-local (fork): audit + redaction layer.
from nanobot.agent.tools.audit import ErrorType
from nanobot.utils.sensitive import redact_if_sensitive

if TYPE_CHECKING:
    from nanobot.runtime_context import RuntimeContextProvider


def is_tool_error_result(result: Any) -> bool:
    return isinstance(result, ToolResult) and result.is_error


# MIT-203: explicit error-type classifier. Historically the registry used
# ``result.startswith("Error")`` — brittle, and conflated a user command
# whose stdout happens to start with "Error" with a framework-level failure.
# We now match against known tool failure markers (shell tool returns the
# only subprocess-backed failure strings today) and fall back to
# ``misclassified`` for legacy tools that still emit "Error:"-prefixed
# strings without a recognisable shape.

_PRESCREEN_MARKERS: tuple[str, ...] = (
    "Error: Command blocked by safety guard",
    "Error: working_dir could not be resolved",
    "Error: working_dir is outside the configured workspace",
)
_TIMEOUT_MARKER = "Error: Command timed out after"
_EXEC_EXCEPTION_MARKER = "Error executing command:"

# Captures the trailing "Exit code: N" footer appended by shell.py so we can
# distinguish a user command that exited non-zero from a framework error.
_EXIT_CODE_RE = re.compile(r"\nExit code:\s*(-?\d+)\s*$")


def _classify_tool_error(result: str, tool_name: str) -> tuple[ErrorType, int | None, str | None]:
    """Bucket a tool result string into an ``(error_type, exit_code, stderr_tail)`` triple.

    Only called when :func:`_looks_like_error` already returned True. Returns
    a best-effort classification — ``misclassified`` for anything we don't
    recognise, with a debug log so we can notice new failure shapes.
    """
    exit_code: int | None = None
    stderr_tail: str | None = None

    exit_match = _EXIT_CODE_RE.search(result)
    if exit_match is not None:
        try:
            exit_code = int(exit_match.group(1))
        except ValueError:
            exit_code = None

    # Capture the tail of embedded STDERR (shell.py prefixes it with
    # "STDERR:\n"). Truncate to ~256 chars for audit-log compactness.
    stderr_idx = result.rfind("STDERR:\n")
    if stderr_idx != -1:
        tail = result[stderr_idx + len("STDERR:\n"):]
        # Drop the trailing exit-code footer if present.
        tail = _EXIT_CODE_RE.sub("", tail).strip()
        if tail:
            stderr_tail = tail[-256:]

    for marker in _PRESCREEN_MARKERS:
        if marker in result:
            return "prescreen", None, None
    if _TIMEOUT_MARKER in result:
        return "timeout", None, stderr_tail
    if _EXEC_EXCEPTION_MARKER in result:
        return "exception", exit_code, stderr_tail
    if exit_code is not None and exit_code != 0:
        return "nonzero_exit", exit_code, stderr_tail

    # Legacy tools that return free-form "Error:" strings without any of the
    # known shell-tool markers. Log at DEBUG so we can fingerprint them later
    # and promote them into a real bucket; don't spam at WARNING.
    logger.debug(
        "audit: misclassified error from tool={} preview={!r}",
        tool_name,
        result[:80],
    )
    return "misclassified", exit_code, stderr_tail


def _looks_like_error(result: Any) -> bool:
    """Ziggy-local (fork): did this tool call fail?

    MIT-203 replaced the brittle ``result.startswith("Error")`` heuristic with
    an explicit marker. Upstream reached the same conclusion and made it
    structural via ``ToolResult.is_error``, so this now simply defers to
    upstream and the fork's string-marker scan is gone. A plain string that
    merely *starts with* "Error" (e.g. a user command whose stdout does) is
    no longer misreported as a framework failure.
    """
    return is_tool_error_result(result)




class ToolRegistry:
    """
    Registry for agent tools.

    Allows dynamic registration and execution of tools.
    """

    def __init__(self):
        self._tools: dict[str, Tool] = {}
        self._cached_definitions: list[dict[str, Any]] | None = None
        self._audit_logger = None
        self._session_id: str = ""
        self._channel: str = ""
        self._sender_id: str = ""

    def register(self, tool: Tool) -> None:
        """Register a tool."""
        self._tools[tool.name] = tool
        self._cached_definitions = None

    def unregister(self, name: str) -> None:
        """Unregister a tool by name."""
        self._tools.pop(name, None)
        self._cached_definitions = None

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

    def get_runtime_context_providers(self) -> list[RuntimeContextProvider]:
        """Return tool-owned providers in stable tool-name order."""
        providers: list[RuntimeContextProvider] = []
        for name in sorted(self._tools):
            provider = self._tools[name].runtime_context_provider()
            if provider is not None:
                providers.append(provider)
        return providers

    @staticmethod
    def _lookup_key(name: str) -> str:
        """Normalize names for suggestions only; never for execution."""
        return "".join(ch.lower() for ch in name if ch.isalnum())

    def _suggest_name(self, name: str) -> str | None:
        key = self._lookup_key(str(name or ""))
        if not key:
            return None
        matches = [
            registered
            for registered in self._tools
            if self._lookup_key(registered) == key
        ]
        if len(matches) == 1:
            return matches[0]
        return None

    def has(self, name: str) -> bool:
        """Check if a tool is registered."""
        return self.get(name) is not None

    @staticmethod
    def _schema_name(schema: dict[str, Any]) -> str:
        """Extract a normalized tool name from either OpenAI or flat schemas."""
        fn = schema.get("function")
        if isinstance(fn, dict):
            name = cast(dict[str, Any], fn).get("name")
            if isinstance(name, str):
                return name
        name = schema.get("name")
        return name if isinstance(name, str) else ""

    def get_definitions(self) -> list[dict[str, Any]]:
        """Get tool definitions with stable ordering for cache-friendly prompts.

        Built-in tools are sorted first as a stable prefix, then MCP tools are
        sorted and appended. The result is cached until the next
        register/unregister call.
        """
        if self._cached_definitions is None:
            definitions = [tool.to_schema() for tool in self._tools.values()]
            builtins: list[dict[str, Any]] = []
            mcp_tools: list[dict[str, Any]] = []
            for schema in definitions:
                name = self._schema_name(schema)
                if name.startswith("mcp_"):
                    mcp_tools.append(schema)
                else:
                    builtins.append(schema)

            builtins.sort(key=self._schema_name)
            mcp_tools.sort(key=self._schema_name)
            self._cached_definitions = builtins + mcp_tools

        return self._cached_definitions

    def prepare_call(
        self,
        name: str,
        params: Any,
    ) -> tuple[Tool | None, Any, str | None]:
        """Resolve, cast, and validate one tool call."""
        tool = self.get(name)
        if not tool:
            suggestion = self._suggest_name(str(name))
            hint = f" Did you mean '{suggestion}'? Tool names must match exactly." if suggestion else ""
            return None, params, (
                ToolResult.error(
                    f"Error: Tool '{name}' not found.{hint} Available: {', '.join(self.tool_names)}"
                )
            )
        # Compatibility for external tools that still implement the legacy
        # setter protocol. Built-ins read the authoritative ContextVar
        # directly and never copy routing state.
        if isinstance(tool, ContextAware) and (ctx := current_request_context()) is not None:
            tool.set_context(ctx)

        params = self._coerce_params(tool, params)
        if not isinstance(params, dict):
            return tool, params, (
                ToolResult.error(
                    f"Error: Tool '{name}' parameters must be a JSON object, got "
                    f"{type(params).__name__}. Use named parameters like "
                    'tool_name(param1="value1", param2="value2") matching the tool schema.'
                )
            )

        cast_params = tool.cast_params(cast(dict[str, Any], params))
        errors = tool.validate_params(cast_params)
        if errors:
            return tool, cast_params, (
                ToolResult.error(f"Error: Invalid parameters for tool '{name}': " + "; ".join(errors))
            )
        return tool, cast_params, None

    @classmethod
    def _coerce_argument_value(cls, value: Any) -> Any:
        if value is None:
            return {}
        if not isinstance(value, str):
            return value

        stripped = value.strip()
        if not stripped:
            return {}

        if not stripped.startswith(("{", "[")):
            return value

        try:
            parsed = json.loads(stripped)
        except Exception:
            return value

        return parsed

    @classmethod
    def _coerce_params(cls, tool: Tool, params: Any) -> Any:
        params = cls._coerce_argument_value(params)
        return cls._unwrap_arguments_payload(tool, params)

    @classmethod
    def _unwrap_arguments_payload(cls, tool: Tool, params: Any) -> Any:
        if not isinstance(params, dict):
            return params
        arguments_payload = cast(dict[str, Any], params)
        if set(arguments_payload) != {"arguments"}:
            return arguments_payload
        properties = (tool.parameters or {}).get("properties", {})
        if isinstance(properties, dict) and "arguments" in properties:
            return arguments_payload
        return cls._coerce_argument_value(arguments_payload.get("arguments"))

    async def execute(
        self,
        name: str,
        params: Any,
        *,
        session_id: str | None = None,
        channel: str | None = None,
    ) -> Any:
        """Execute a tool by name with given parameters.

        Ziggy-local (fork): *session_id* and *channel* are per-call audit
        overrides so concurrent workers do not share mutable registry state.
        """
        hint = "\n\n[Analyze the error above and try a different approach.]"
        sid = session_id or self._session_id
        ch = channel or self._channel
        t0 = time.monotonic()
        tool, params, error = self.prepare_call(name, params)
        if error:
            # Ziggy-local (fork, MIT-203): a rejected call never ran — audit it
            # as a prescreen-class failure (no exit code, no stderr).
            self._audit(
                "error", name, params if isinstance(params, dict) else {}, t0, sid, ch,
                error=str(error)[:2048],
                error_type="prescreen",
            )
            self._prom_observe(name, "error", (time.monotonic() - t0) * 1000)
            return ToolResult.error(str(error) + hint)

        try:
            assert tool is not None  # guarded by prepare_call()
            result = await tool.execute(**params)
            duration_ms = (time.monotonic() - t0) * 1000
            # Ziggy-local (fork, MIT-203): audit + Prometheus + redaction layered
            # on upstream's ToolResult contract. Upstream's ``is_tool_error_result``
            # replaces the old startswith("Error") heuristic; the finer-grained
            # classifier below still buckets *why* the call failed for the audit log.
            if _looks_like_error(result):
                status = "error"
                error_type, exit_code, stderr_tail = _classify_tool_error(str(result), name)
                self._audit(
                    status, name, params, t0, sid, ch,
                    error=str(result)[:2048],
                    error_type=error_type,
                    exit_code=exit_code,
                    stderr_tail=stderr_tail,
                )
            else:
                status = "ok"
                self._audit(status, name, params, t0, sid, ch)
            self._prom_observe(name, status, duration_ms)

            # Ziggy-local (fork, MIT-122/MIT-147): scrub embedded secrets from any
            # string result, on BOTH the success and error paths. Tool authors
            # legitimately echo the offending payload into an error message, and
            # before MIT-147 the error branch short-circuited the redactor. The
            # error flag is carried on the rewrapped ToolResult, so upstream's
            # structured failure detection survives redaction (this replaces the
            # old "re-prefix with Error:" hack, which existed only because the
            # contract used to be a bare string).
            if isinstance(result, str):
                redacted = redact_if_sensitive(str(result))
                if status == "error":
                    return ToolResult.error(redacted + hint)
                return ToolResult(redacted, is_error=False)
            return result
        except Exception as e:
            duration_ms = (time.monotonic() - t0) * 1000
            self._audit(
                "error", name, params, t0, sid, ch,
                error=str(e),
                error_type="exception",
            )
            self._prom_observe(name, "error", duration_ms)
            # Exception strings can also carry secrets (MIT-147).
            raw = f"Error executing {name}: {str(e)}"
            return ToolResult.error(redact_if_sensitive(raw) + hint)

    def _audit(
        self,
        status: str,
        name: str,
        params: dict,
        t0: float,
        session_id: str = "",
        channel: str = "",
        error: str | None = None,
        error_type: ErrorType | None = None,
        exit_code: int | None = None,
        stderr_tail: str | None = None,
    ) -> None:
        """Log a tool execution to the audit logger if attached.

        MIT-203: when *status* is ``"error"``, callers pass ``error_type``
        / ``exit_code`` / ``stderr_tail`` so downstream dashboards can
        separate user-painful failures (``timeout``/``nonzero_exit``/
        ``exception``) from safety-guard rejects (``prescreen``).
        """
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
                error_type=error_type,
                exit_code=exit_code,
                stderr_tail=stderr_tail,
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
        return self.has(name)
