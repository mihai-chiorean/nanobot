"""TP-07: silent argument repairs are flagged, never their values.

Covers ``ToolRegistry.prepare_call_ex`` repair kinds and the propagation of
``args_repaired`` / ``args_repair_kinds`` to the tool event, the progress
payloads, the Work row source, the audit ``record_call`` extra, the Langfuse
tool span metadata and the turn provenance counters.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from nanobot.agent import turn_provenance
from nanobot.agent.hook import AgentHook, AgentHookContext
from nanobot.agent.tools import execution as execution_module
from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.providers.base import ToolCallRequest
from nanobot.utils.progress_events import (
    build_tool_event_finish_payloads,
    build_tool_event_start_payload,
)

_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string"},
        "count": {"type": "integer"},
    },
    "required": ["query"],
}


class _SearchTool(Tool):
    """Tool whose schema is {query: string, count: integer} (issue spec)."""

    def __init__(self) -> None:
        self.received: dict[str, Any] | None = None

    @property
    def name(self) -> str:
        return "search"

    @property
    def description(self) -> str:
        return "search"

    @property
    def parameters(self) -> dict[str, Any]:
        return _SCHEMA

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, **kwargs: Any) -> Any:
        self.received = kwargs
        return "ok"


class _RecordingRegistry(ToolRegistry):
    """Registry with the TP-08 ``record_call`` seam, capturing its arguments."""

    def __init__(self) -> None:
        super().__init__()
        self.recorded: list[dict[str, Any]] = []

    def record_call(
        self,
        tool_name: str,
        params: Any,
        status: str,
        duration_ms: float,
        error: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        self.recorded.append(
            {
                "tool_name": tool_name,
                "params": params,
                "status": status,
                "duration_ms": duration_ms,
                "error": error,
                "extra": extra,
            }
        )


def _registry() -> tuple[ToolRegistry, _SearchTool]:
    registry = ToolRegistry()
    tool = _SearchTool()
    registry.register(tool)
    return registry, tool


async def _run_call(
    registry: ToolRegistry,
    arguments: Any,
) -> tuple[dict[str, Any], _SearchTool]:
    tool = registry.get("search")
    assert isinstance(tool, _SearchTool)
    _results, events, fatal = await execution_module.execute_tool_calls(
        registry,
        [ToolCallRequest(id="call-1", name="search", arguments=arguments)],
        concurrent=False,
        external_lookup_counts={},
        workspace_violation_counts={},
        hook=AgentHook(),
        context=AgentHookContext(iteration=0, messages=[]),
    )
    assert fatal is None
    assert len(events) == 1
    return events[0], tool


# --- prepare_call_ex repair kinds ------------------------------------------


def test_prepare_call_ex_json_string_parsed() -> None:
    registry, _ = _registry()
    _tool, params, error, repairs = registry.prepare_call_ex("search", '{"query":"x"}')
    assert error is None
    assert params == {"query": "x"}
    assert repairs == ["json_string_parsed"]


def test_prepare_call_ex_arguments_unwrapped() -> None:
    registry, _ = _registry()
    _tool, params, error, repairs = registry.prepare_call_ex(
        "search", {"arguments": {"query": "x"}}
    )
    assert error is None
    assert params == {"query": "x"}
    assert repairs == ["arguments_unwrapped"]


def test_prepare_call_ex_unwrap_of_json_string_records_both_kinds() -> None:
    registry, _ = _registry()
    _tool, params, error, repairs = registry.prepare_call_ex(
        "search", {"arguments": '{"query":"x"}'}
    )
    assert error is None
    assert params == {"query": "x"}
    assert repairs == ["arguments_unwrapped", "json_string_parsed"]


def test_prepare_call_ex_type_cast() -> None:
    registry, _ = _registry()
    _tool, params, error, repairs = registry.prepare_call_ex(
        "search", {"query": "x", "count": "3"}
    )
    assert error is None
    assert params == {"query": "x", "count": 3}
    assert repairs == ["type_cast"]


def test_prepare_call_ex_clean_call_records_nothing() -> None:
    registry, _ = _registry()
    _tool, params, error, repairs = registry.prepare_call_ex(
        "search", {"query": "x", "count": 3}
    )
    assert error is None
    assert params == {"query": "x", "count": 3}
    assert repairs == []


def test_prepare_call_still_returns_three_tuple() -> None:
    registry, _ = _registry()
    prepared = registry.prepare_call("search", {"query": "x", "count": "3"})
    assert isinstance(prepared, tuple)
    assert len(prepared) == 3
    tool, params, error = prepared
    assert tool is not None
    assert params == {"query": "x", "count": 3}
    assert error is None


def test_prepares_are_idempotent_without_arguments_property_tool() -> None:
    # The unwrap must NOT fire on a tool that legitimately has an "arguments"
    # parameter — negative control beyond the issue's happy paths.
    registry = ToolRegistry()

    class _EchoTool(Tool):
        @property
        def name(self) -> str:
            return "echo"

        @property
        def description(self) -> str:
            return "echo"

        @property
        def parameters(self) -> dict[str, Any]:
            return {
                "type": "object",
                "properties": {"arguments": {"type": "object"}},
                "required": ["arguments"],
            }

        @property
        def read_only(self) -> bool:
            return True

        async def execute(self, **kwargs: Any) -> Any:
            return "ok"

    registry.register(_EchoTool())
    _tool, params, error, repairs = registry.prepare_call_ex(
        "echo", {"arguments": {"query": "x"}}
    )
    assert error is None
    assert params == {"arguments": {"query": "x"}}
    assert repairs == []


# --- event dict from the live execution path --------------------------------


async def test_repaired_call_flags_the_event() -> None:
    registry, _ = _registry()
    event, tool = await _run_call(registry, '{"query":"x"}')
    assert event["status"] == "ok"
    assert event["args_repaired"] is True
    assert event["args_repair_kinds"] == ["json_string_parsed"]
    # The tool really ran with the repaired arguments.
    assert tool.received == {"query": "x"}


async def test_clean_call_event_carries_no_repair_keys() -> None:
    registry, _ = _registry()
    event, tool = await _run_call(registry, {"query": "x", "count": 3})
    assert event["status"] == "ok"
    assert "args_repaired" not in event
    assert "args_repair_kinds" not in event
    assert tool.received == {"query": "x", "count": 3}


async def test_event_added_fields_never_carry_argument_values() -> None:
    registry, _ = _registry()
    event, _tool = await _run_call(registry, {"arguments": '{"query":"secret-x"}'})
    added = {
        key: event[key]
        for key in ("args_repaired", "args_repair_kinds")
    }
    assert added["args_repaired"] is True
    assert added["args_repair_kinds"] == ["arguments_unwrapped", "json_string_parsed"]
    assert "x" not in json.dumps(added)
    assert "secret" not in json.dumps(added)
    assert "query" not in json.dumps(added)


async def test_repaired_invalid_call_still_flags_event() -> None:
    # A repaired call that then fails validation keeps its repair flag on the
    # error event (the model was still silently corrected).
    registry, _ = _registry()
    event, _tool = await _run_call(registry, '{"count":"3"}')
    assert event["status"] == "error"
    assert event["args_repaired"] is True
    assert "json_string_parsed" in event["args_repair_kinds"]


# --- progress payloads -------------------------------------------------------


async def test_finish_payload_carries_the_flag_from_the_event() -> None:
    registry, _ = _registry()
    event, _tool = await _run_call(registry, {"query": "x", "count": "3"})
    tool_call = ToolCallRequest(id="call-1", name="search", arguments={"query": "x", "count": "3"})
    context = AgentHookContext(iteration=0, messages=[])
    context.tool_calls = [tool_call]
    context.tool_results = ["ok"]
    context.tool_events = [event]
    payloads = build_tool_event_finish_payloads(context)
    assert len(payloads) == 1
    payload = payloads[0]
    assert payload["phase"] == "end"
    assert payload["args_repaired"] is True
    assert payload["args_repair_kinds"] == ["type_cast"]
    added = {key: payload[key] for key in ("args_repaired", "args_repair_kinds")}
    assert "x" not in json.dumps(added)
    assert "3" not in json.dumps(added)


async def test_clean_finish_payload_carries_no_repair_keys() -> None:
    registry, _ = _registry()
    event, _tool = await _run_call(registry, {"query": "x", "count": 3})
    tool_call = ToolCallRequest(id="call-1", name="search", arguments={"query": "x", "count": 3})
    context = AgentHookContext(iteration=0, messages=[])
    context.tool_calls = [tool_call]
    context.tool_results = ["ok"]
    context.tool_events = [event]
    payload = build_tool_event_finish_payloads(context)[0]
    assert "args_repaired" not in payload
    assert "args_repair_kinds" not in payload


def test_start_payload_carries_repairs_when_caller_knows_them() -> None:
    tool_call = ToolCallRequest(id="call-1", name="search", arguments={"query": "x"})
    payload = build_tool_event_start_payload(tool_call, ["json_string_parsed"])
    assert payload["phase"] == "start"
    assert payload["args_repaired"] is True
    assert payload["args_repair_kinds"] == ["json_string_parsed"]
    # Nothing is added when there is nothing to flag.
    clean = build_tool_event_start_payload(tool_call)
    assert "args_repaired" not in clean
    assert "args_repair_kinds" not in clean
    empty = build_tool_event_start_payload(tool_call, [])
    assert "args_repaired" not in empty


# --- audit row (record_call seam, TP-08) ------------------------------------


async def test_record_call_extra_carries_the_flag() -> None:
    registry = _RecordingRegistry()
    registry.register(_SearchTool())
    event, _tool = await _run_call(registry, {"query": "x", "count": "3"})
    assert event["args_repaired"] is True
    assert len(registry.recorded) == 1
    row = registry.recorded[0]
    assert row["tool_name"] == "search"
    assert row["status"] == "ok"
    assert row["extra"] == {"args_repaired": True, "args_repair_kinds": ["type_cast"]}
    assert "x" not in json.dumps(row["extra"])


async def test_record_call_has_no_extra_for_clean_calls() -> None:
    registry = _RecordingRegistry()
    registry.register(_SearchTool())
    event, _tool = await _run_call(registry, {"query": "x", "count": 3})
    assert "args_repaired" not in event
    assert len(registry.recorded) == 1
    assert registry.recorded[0]["extra"] is None


async def test_audit_jsonl_row_carries_the_flag_once(tmp_path) -> None:
    from nanobot.agent.tools.audit import AuditLogger

    registry, _tool = _registry()
    registry.set_audit_logger(AuditLogger(tmp_path / "audit.jsonl"))
    await _run_call(registry, {"query": "x", "count": "3"})
    rows = [
        json.loads(line)
        for line in (tmp_path / "audit.jsonl").read_text().splitlines()
        if line.strip()
    ]
    tool_rows = [row for row in rows if row.get("tool_name") == "search"]
    assert len(tool_rows) == 1
    assert tool_rows[0]["args_repaired"] is True
    assert tool_rows[0]["args_repair_kinds"] == ["type_cast"]


def test_record_call_signature_matches_tp08() -> None:
    import inspect

    sig = inspect.signature(_RecordingRegistry.record_call)
    names = list(sig.parameters)
    assert names[:5] == ["self", "tool_name", "params", "status", "duration_ms"]
    assert sig.parameters["error"].default is None
    assert sig.parameters["extra"].default is None


# --- Langfuse tool span metadata ---------------------------------------------


async def test_observe_tool_receives_repair_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, Any] | None] = []
    real_observe_tool = execution_module.observe_tool

    def _spy_observe_tool(**kwargs: Any):
        seen.append(kwargs.get("metadata"))
        return real_observe_tool(**kwargs)

    monkeypatch.setattr(execution_module, "observe_tool", _spy_observe_tool)
    registry, _ = _registry()
    await _run_call(registry, '{"query":"x"}')
    assert seen == [{"ziggy.args_repaired": True, "ziggy.args_repair_kinds": "json_string_parsed"}]

    seen.clear()
    registry, _ = _registry()
    await _run_call(registry, {"query": "x", "count": 3})
    assert seen == [None]


def test_observe_tool_accepts_metadata_argument() -> None:
    import inspect

    from nanobot.observability import observe_tool

    sig = inspect.signature(observe_tool)
    assert "metadata" in sig.parameters
    assert sig.parameters["metadata"].default is None


# --- turn provenance counters -------------------------------------------------


async def test_turn_provenance_counters_incremented(monkeypatch: pytest.MonkeyPatch) -> None:
    record = SimpleNamespace(args_repaired={})
    monkeypatch.setattr(
        turn_provenance, "current_turn_provenance", lambda: record, raising=False
    )
    registry, _ = _registry()
    await _run_call(registry, {"arguments": {"query": "x"}})
    await _run_call(registry, {"query": "x", "count": "3"})
    assert record.args_repaired == {"arguments_unwrapped": 1, "type_cast": 1}


async def test_turn_provenance_absent_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delattr(turn_provenance, "current_turn_provenance", raising=False)
    registry, _ = _registry()
    event, _tool = await _run_call(registry, {"query": "x", "count": "3"})
    assert event["args_repair_kinds"] == ["type_cast"]


async def test_turn_provenance_returns_none_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(turn_provenance, "current_turn_provenance", lambda: None, raising=False)
    registry, _ = _registry()
    event, _tool = await _run_call(registry, {"query": "x", "count": "3"})
    assert event["args_repair_kinds"] == ["type_cast"]
