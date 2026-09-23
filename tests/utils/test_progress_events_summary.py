"""Tool-event payloads must carry a human-readable ``summary`` (prod parity, 7ed34a36).

The iOS client (``WebSocketModels.swift``) falls back to the raw tool name when
``summary`` is missing, so a frame without it renders ``web_search`` instead of
a sentence.
"""

from __future__ import annotations

import pytest

from nanobot.agent.hook import AgentHookContext
from nanobot.providers.base import ToolCallRequest
from nanobot.utils import progress_events
from nanobot.utils.progress_events import (
    build_tool_event_finish_payloads,
    build_tool_event_start_payload,
)


def _web_search_call() -> ToolCallRequest:
    return ToolCallRequest(id="call-1", name="web_search", arguments={"query": "weather Paris"})


def _finish_context(*tool_calls: ToolCallRequest, results: list[object], events: list[dict]) -> AgentHookContext:
    context = AgentHookContext(iteration=0, messages=[])
    context.tool_calls = list(tool_calls)
    context.tool_results = list(results)
    context.tool_events = list(events)
    return context


def test_start_payload_web_search_summary_is_human_readable() -> None:
    payload = build_tool_event_start_payload(_web_search_call())
    summary = payload["summary"]
    assert isinstance(summary, str)
    assert summary
    assert summary != "web_search"
    assert "weather Paris" in summary


def test_finish_payload_web_search_summary_is_human_readable() -> None:
    context = _finish_context(
        _web_search_call(),
        results=["1 result"],
        events=[{"name": "web_search", "status": "ok", "detail": "1 result"}],
    )
    payloads = build_tool_event_finish_payloads(context)
    assert len(payloads) == 1
    summary = payloads[0]["summary"]
    assert isinstance(summary, str)
    assert summary
    assert summary != "web_search"
    assert "weather Paris" in summary


def test_unknown_tool_with_odd_arguments_falls_back_without_raising() -> None:
    odd_call = ToolCallRequest(
        id="call-2",
        name="frobnicate_widget",
        arguments={"cfg": {"deep": [1, {"x": None}]}, "flag": True, "empty": ""},
    )
    payload = build_tool_event_start_payload(odd_call)
    assert payload["summary"] == "frobnicate_widget"

    context = _finish_context(
        odd_call,
        results=["done"],
        events=[{"name": "frobnicate_widget", "status": "ok", "detail": "done"}],
    )
    payloads = build_tool_event_finish_payloads(context)
    assert payloads[0]["summary"] == "frobnicate_widget"


def test_summary_falls_back_to_tool_name_when_formatter_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(tool_calls: object, max_length: int = 40) -> str:
        raise RuntimeError("formatter exploded")

    monkeypatch.setattr(progress_events, "format_tool_hints", boom)
    payload = build_tool_event_start_payload(_web_search_call())
    assert payload["summary"] == "web_search"


def test_existing_fields_unchanged_by_summary_addition() -> None:
    payload = build_tool_event_start_payload(_web_search_call())
    assert payload == {
        "version": 1,
        "phase": "start",
        "call_id": "call-1",
        "name": "web_search",
        "summary": payload["summary"],
        "arguments": {"query": "weather Paris"},
        "result": None,
        "error": None,
        "files": [],
        "embeds": [],
    }

    context = _finish_context(
        _web_search_call(),
        results=[{"content": "sunny", "files": [{"path": "a.txt"}], "embeds": ["e1"]}],
        events=[{"name": "web_search", "status": "ok", "detail": "sunny"}],
    )
    payloads = build_tool_event_finish_payloads(context)
    assert payloads[0] == {
        "version": 1,
        "phase": "end",
        "call_id": "call-1",
        "name": "web_search",
        "summary": payloads[0]["summary"],
        "arguments": {"query": "weather Paris"},
        "result": {"content": "sunny", "files": [{"path": "a.txt"}], "embeds": ["e1"]},
        "error": None,
        "files": [{"path": "a.txt"}],
        "embeds": ["e1"],
    }


def test_error_phase_payload_also_carries_summary() -> None:
    context = _finish_context(
        _web_search_call(),
        results=["upstream 502"],
        events=[{"name": "web_search", "status": "error", "detail": "upstream 502"}],
    )
    payloads = build_tool_event_finish_payloads(context)
    assert payloads[0]["phase"] == "error"
    assert payloads[0]["error"] == "upstream 502"
    assert payloads[0]["summary"] != ""
    assert "weather Paris" in payloads[0]["summary"]
