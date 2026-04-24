"""Tests for MIT-203 ToolRegistry error classification.

Exercises the explicit-enum classifier that replaced
``result.startswith("Error")`` as the sole failure detector. Each test
drives a fake tool that returns (or raises) a specific shape, and asserts
the correct ``error_type`` lands in the audit log.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from nanobot.agent.tools.audit import AuditLogger
from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.schema import StringSchema, tool_parameters_schema


def _read_entries(path: Path) -> list[dict[str, Any]]:
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


class _FakeTool(Tool):
    """Minimal tool whose ``execute`` returns whatever the test supplies."""

    def __init__(self, return_value: Any = "", raise_exc: Exception | None = None):
        self._rv = return_value
        self._exc = raise_exc

    @property
    def name(self) -> str:
        return "fake"

    @property
    def description(self) -> str:
        return "fake tool for tests"

    @property
    def parameters(self) -> dict:
        return tool_parameters_schema(msg=StringSchema("any"), required=["msg"])

    async def execute(self, **kwargs):
        if self._exc is not None:
            raise self._exc
        return self._rv


async def _run_once(tmp_path: Path, tool: _FakeTool, params: dict[str, Any] | None = None) -> dict:
    registry = ToolRegistry()
    registry.register(tool)
    audit = AuditLogger(log_path=tmp_path / "audit.jsonl")
    registry.set_audit_logger(audit)
    # NB: use ``is None`` not truthiness — an empty-dict ``params`` is a
    # valid test input (exercises the "missing required field" path).
    await registry.execute("fake", {"msg": "hi"} if params is None else params)
    return _read_entries(tmp_path / "audit.jsonl")[0]


async def test_classify_prescreen_shell_marker(tmp_path: Path) -> None:
    tool = _FakeTool(return_value="Error: Command blocked by safety guard (dangerous pattern detected)")
    entry = await _run_once(tmp_path, tool)
    assert entry["result_status"] == "error"
    assert entry["error_type"] == "prescreen"
    assert "exit_code" not in entry


async def test_classify_prescreen_invalid_params(tmp_path: Path) -> None:
    tool = _FakeTool(return_value="ok")
    # Missing required `msg` parameter triggers validate_params → prescreen.
    entry = await _run_once(tmp_path, tool, params={})
    assert entry["result_status"] == "error"
    assert entry["error_type"] == "prescreen"


async def test_classify_timeout(tmp_path: Path) -> None:
    tool = _FakeTool(return_value="Error: Command timed out after 180 seconds")
    entry = await _run_once(tmp_path, tool)
    assert entry["error_type"] == "timeout"


async def test_successful_result_starting_with_error_keyword(tmp_path: Path) -> None:
    """User command output that happens to start with 'Error' is still OK.

    Historical contract: only tool-framework failures wrap themselves in an
    "Error:" prefix. Any subprocess output is returned as-is and classified
    as ``result_status=ok``. We lock this in to make sure the new classifier
    hasn't tightened the contract.
    """
    tool = _FakeTool(return_value="normal program output\n\nExit code: 0")
    entry = await _run_once(tmp_path, tool)
    assert entry["result_status"] == "ok"


async def test_classify_exec_exception_with_stderr(tmp_path: Path) -> None:
    """A tool that wraps a subprocess error as 'Error executing command: ...'."""
    result = "Error executing command: cmd failed\nSTDERR:\nboom\n\nExit code: 2"
    tool = _FakeTool(return_value=result)
    entry = await _run_once(tmp_path, tool)
    assert entry["error_type"] == "exception"
    assert entry["exit_code"] == 2
    assert "boom" in entry["stderr_tail"]


async def test_classify_exception_raised(tmp_path: Path) -> None:
    tool = _FakeTool(raise_exc=RuntimeError("boom"))
    entry = await _run_once(tmp_path, tool)
    assert entry["result_status"] == "error"
    assert entry["error_type"] == "exception"
    assert "boom" in entry["error"]


async def test_classify_misclassified_legacy_error_string(tmp_path: Path) -> None:
    """Legacy tool returning 'Error: something weird' with no known shape."""
    tool = _FakeTool(return_value="Error: something a human wrote a decade ago")
    entry = await _run_once(tmp_path, tool)
    assert entry["error_type"] == "misclassified"


async def test_prescreen_takes_precedence_over_exit_code(tmp_path: Path) -> None:
    """If a safety-guard marker and an exit-code footer both appear, prescreen wins."""
    tool = _FakeTool(return_value="Error: Command blocked by safety guard (x)\n\nExit code: 0")
    entry = await _run_once(tmp_path, tool)
    assert entry["error_type"] == "prescreen"
    # exit_code stays out of the entry for prescreen rows.
    assert "exit_code" not in entry
