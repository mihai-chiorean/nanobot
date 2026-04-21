"""Shell execution tool."""

import asyncio
import os
import re
import shlex
from pathlib import Path
from typing import Any

from nanobot.agent.tools.base import Tool
from nanobot.utils.sensitive import check_shell_command, redact_if_sensitive


# Commands that are safe to appear in pipe chains (read-only / filtering)
_SAFE_PIPE_COMMANDS = frozenset({
    "grep", "egrep", "fgrep", "rg",
    "head", "tail", "less", "more",
    "cat", "tac", "nl",
    "sort", "uniq", "shuf",
    "wc", "cut", "tr", "awk", "sed",
    "column", "paste", "fold", "fmt",
    "tee", "xargs",
    "jq", "yq",
    "find", "ls", "stat", "file", "du", "df",
    "git", "diff", "comm",
    "echo", "printf", "date", "whoami", "hostname", "uname", "id",
    "ps", "top", "htop", "free", "uptime",
    "pip", "npm", "cargo", "go", "make", "cmake",
    "python3", "python",  # allowed in pipes for things like python3 -c "..."
})


class ExecTool(Tool):
    """Tool to execute shell commands.

    Simple commands run via create_subprocess_exec for safety.
    Pipe chains are allowed when all commands in the chain are known-safe.
    Dangerous patterns (subshells, chaining, redirects to files) are blocked.
    """

    def __init__(
        self,
        timeout: int = 60,
        working_dir: str | None = None,
        deny_patterns: list[str] | None = None,
        allow_patterns: list[str] | None = None,
        restrict_to_workspace: bool = False,
        path_append: str = "",
    ):
        self.timeout = timeout
        self.working_dir = working_dir
        self.deny_patterns = deny_patterns or [
            r"\brm\s+-[rf]{1,2}\b",          # rm -r, rm -rf, rm -fr
            r"\bdel\s+/[fq]\b",              # del /f, del /q
            r"\brmdir\s+/s\b",               # rmdir /s
            r"(?:^|[;&|]\s*)format\b",       # format (as standalone command only)
            r"\b(mkfs|diskpart)\b",          # disk operations
            r"\bdd\s+if=",                   # dd
            r">\s*/dev/sd",                  # write to disk
            r"\b(shutdown|reboot|poweroff)\b",  # system power
            r":\(\)\s*\{.*\};\s*:",          # fork bomb
        ]
        # Blocked executables — prevent bypass via direct binary invocation
        self._blocked_executables = frozenset({
            "rm", "rmdir", "mkfs", "dd", "shutdown", "reboot", "poweroff",
            "diskpart", "format", "del",
        })
        self.allow_patterns = allow_patterns or []
        self.restrict_to_workspace = restrict_to_workspace
        self.path_append = path_append

    @property
    def name(self) -> str:
        return "exec"

    @property
    def description(self) -> str:
        return "Execute a shell command and return its output. Supports pipes between commands."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to execute"
                },
                "working_dir": {
                    "type": "string",
                    "description": "Optional working directory for the command"
                }
            },
            "required": ["command"]
        }

    def _is_safe_pipe_chain(self, command: str) -> bool:
        """Check if a pipe chain uses only known-safe commands."""
        # Split on pipe, check each segment's first word
        segments = command.split("|")
        for segment in segments:
            segment = segment.strip()
            if not segment:
                return False
            try:
                parts = shlex.split(segment)
            except ValueError:
                return False
            if not parts:
                return False
            exe = Path(parts[0]).name.lower()
            if exe not in _SAFE_PIPE_COMMANDS:
                return False
        return True

    async def execute(self, command: str, working_dir: str | None = None, **kwargs: Any) -> str:
        cwd = working_dir or self.working_dir or os.getcwd()
        guard_error = self._guard_command(command, cwd)
        if guard_error:
            return guard_error

        # Layer 3a: Block commands that target sensitive data
        sensitive_error = check_shell_command(command)
        if sensitive_error:
            return sensitive_error

        # Block null bytes and carriage returns always
        if "\x00" in command or "\r" in command:
            return "Error: Null bytes and carriage returns are not allowed"

        # Block dangerous patterns: subshells, command chaining, backticks, process substitution
        if re.search(r'[;&`${}]', command) or '$((' in command or '\n' in command:
            return "Error: Shell metacharacters are not allowed (no chaining, subshells, or variable expansion)"

        env = os.environ.copy()
        if self.path_append:
            env["PATH"] = env.get("PATH", "") + os.pathsep + self.path_append

        # Determine execution mode
        has_pipe = "|" in command
        has_redirect = bool(re.search(r'[<>]', command))

        if has_redirect:
            return "Error: File redirects (< >) are not allowed"

        if has_pipe:
            # Pipe chain — validate all commands are safe, then run via sh -c
            if not self._is_safe_pipe_chain(command):
                return "Error: Pipe chains may only use common read-only commands (grep, head, tail, sort, wc, git, etc.)"
            # Run via shell for pipe support, but command is validated
            try:
                process = await asyncio.create_subprocess_shell(
                    command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=cwd,
                    env=env,
                )
            except Exception as e:
                return f"Error executing command: {str(e)}"
        else:
            # Simple command — run via exec (no shell)
            try:
                argv = shlex.split(command)
            except ValueError as e:
                return f"Error: Failed to parse command: {e}"

            if not argv:
                return "Error: Empty command"

            # Block dangerous executables
            exe_name = Path(argv[0]).name.lower()
            if exe_name in self._blocked_executables:
                return "Error: Command blocked by safety guard (dangerous executable)"

            try:
                process = await asyncio.create_subprocess_exec(
                    *argv,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=cwd,
                    env=env,
                )
            except Exception as e:
                return f"Error executing command: {str(e)}"

        # Common output handling
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=self.timeout
            )
        except asyncio.TimeoutError:
            process.kill()
            try:
                await asyncio.wait_for(process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                pass
            return f"Error: Command timed out after {self.timeout} seconds"

        output_parts = []

        if stdout:
            output_parts.append(stdout.decode("utf-8", errors="replace"))

        if stderr:
            stderr_text = stderr.decode("utf-8", errors="replace")
            if stderr_text.strip():
                output_parts.append(f"STDERR:\n{stderr_text}")

        if process.returncode != 0:
            output_parts.append(f"\nExit code: {process.returncode}")

        result = "\n".join(output_parts) if output_parts else "(no output)"

        # Truncate very long output
        max_len = 10000
        if len(result) > max_len:
            result = result[:max_len] + f"\n... (truncated, {len(result) - max_len} more chars)"

        # Layer 3b: Redact any sensitive content that leaked into output
        result = redact_if_sensitive(result)

        return result

    def _guard_command(self, command: str, cwd: str) -> str | None:
        """Best-effort safety guard for potentially destructive commands."""
        cmd = command.strip()
        lower = cmd.lower()

        for pattern in self.deny_patterns:
            if re.search(pattern, lower):
                return "Error: Command blocked by safety guard (dangerous pattern detected)"

        if self.allow_patterns:
            if not any(re.search(p, lower) for p in self.allow_patterns):
                return "Error: Command blocked by safety guard (not in allowlist)"

        if self.restrict_to_workspace:
            if "..\\" in cmd or "../" in cmd:
                return "Error: Command blocked by safety guard (path traversal detected)"

            cwd_path = Path(cwd).resolve()

            win_paths = re.findall(r"[A-Za-z]:\\[^\\\"']+", cmd)
            posix_paths = re.findall(r"(?:^|[\s|>])(/[^\s\"'>]+)", cmd)

            for raw in win_paths + posix_paths:
                try:
                    p = Path(raw.strip()).resolve()
                except Exception:
                    continue
                if p.is_absolute() and cwd_path not in p.parents and p != cwd_path:
                    return "Error: Command blocked by safety guard (path outside working dir)"

        return None
