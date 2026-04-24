"""Tests for the MIT-203 audit schema extensions.

Covers schema-level behaviour only: new ``error_type`` / ``exit_code`` /
``stderr_tail`` fields are persisted when supplied and omitted otherwise,
and the existing ``error`` field continues to land in every error row.
Registry-level classification is exercised in
``tests/tools/test_registry_classification.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from nanobot.agent.tools.audit import AuditLogger


def _read_entries(path: Path) -> list[dict[str, Any]]:
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


def test_audit_logger_persists_error_field(tmp_path: Path) -> None:
    """Regression: when an error message is supplied, it lands in the JSONL."""
    audit = AuditLogger(log_path=tmp_path / "audit.jsonl")
    audit.log(
        tool_name="exec",
        arguments={"command": "false"},
        result_status="error",
        error="Exit code: 1",
    )
    entries = _read_entries(tmp_path / "audit.jsonl")
    assert len(entries) == 1
    assert entries[0]["error"] == "Exit code: 1"


def test_audit_logger_persists_new_fields(tmp_path: Path) -> None:
    audit = AuditLogger(log_path=tmp_path / "audit.jsonl")
    audit.log(
        tool_name="exec",
        arguments={"command": "false"},
        result_status="error",
        error="nonzero",
        error_type="nonzero_exit",
        exit_code=2,
        stderr_tail="bash: command not found",
    )
    entry = _read_entries(tmp_path / "audit.jsonl")[0]
    assert entry["error_type"] == "nonzero_exit"
    assert entry["exit_code"] == 2
    assert entry["stderr_tail"] == "bash: command not found"


def test_audit_logger_omits_new_fields_when_absent(tmp_path: Path) -> None:
    """Backward compat: lines from legacy callers have no new keys."""
    audit = AuditLogger(log_path=tmp_path / "audit.jsonl")
    audit.log(
        tool_name="read_file",
        arguments={"path": "/tmp/x"},
        result_status="ok",
    )
    entry = _read_entries(tmp_path / "audit.jsonl")[0]
    assert "error_type" not in entry
    assert "exit_code" not in entry
    assert "stderr_tail" not in entry


def test_audit_logger_log_llm_call_unchanged(tmp_path: Path) -> None:
    """log_llm_call has a separate schema and must not grow new keys."""
    audit = AuditLogger(log_path=tmp_path / "audit.jsonl")
    audit.log_llm_call(
        session_id="s", channel="cli", model="minimax",
        tokens_in=100, tokens_out=200, latency_ms=1234.5,
    )
    entry = _read_entries(tmp_path / "audit.jsonl")[0]
    assert entry["event_type"] == "llm_call"
    assert "error_type" not in entry
