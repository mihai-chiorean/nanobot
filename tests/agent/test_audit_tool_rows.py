"""audit.jsonl tool rows on the live execution path (MIT-1849 / TP-08).

Design: docs/design/turn-provenance.md §6 (ziggy repo, PR mihai-chiorean/ziggy#511).

Since 2026-09-24 audit.jsonl carried only ``llm_call`` rows: tool rows were
written solely by ``ToolRegistry._audit``, which only ``ToolRegistry.execute``
calls — and the live path in ``execution.py`` dispatches via
``tool.execute(**params)`` directly, never touching the registry's audit
layer. The tester Activity screen (``GET /api/activity/audit``, MIT-1450 /
MIT-1480) reads this file, so testers saw no tool activity at all.

These tests drive the *production* caller (``execute_tool_calls`` with the
same arguments the runner passes) plus the direct ``ToolRegistry.execute``
path, and pin:

* exactly one tool row per call on both paths (no double write);
* the result the model receives is untouched — recording is record-only,
  redaction of the result is NOT restored here (open question 5 of the
  design doc);
* ``session_id`` / ``channel`` / ``turn_id`` come from the bound
  ``RequestContext`` when the registry's own values are empty.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.agent.hook import AgentHook, AgentHookContext, AgentTurnHookContext
from nanobot.agent.loop import AgentLoop, _ZiggyTurnHook
from nanobot.agent.tools.ask import AskUserInterrupt
from nanobot.agent.tools.audit import AuditLogger
from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.context import RequestContext, request_context
from nanobot.agent.tools.execution import execute_tool_calls
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.bus.events import INBOUND_META_ROOM_SCOPE, InboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import GenerationSettings, LLMResponse, ToolCallRequest
from nanobot.utils.llm_runtime import LLMRuntime


class _ScriptedTool(Tool):
    """Minimal tool whose ``execute`` follows a scripted outcome."""

    def __init__(
        self,
        name: str = "echo",
        *,
        result: Any = "done",
        raises: BaseException | None = None,
        read_only: bool = True,
    ) -> None:
        self._name = name
        self._result = result
        self._raises = raises
        self._read_only = read_only
        self.calls: list[dict[str, Any]] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f"scripted tool {self._name}"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "required": []}

    @property
    def read_only(self) -> bool:
        return self._read_only

    async def execute(self, **kwargs: Any) -> Any:
        self.calls.append(dict(kwargs))
        if self._raises is not None:
            raise self._raises
        return self._result


def _registry_with_audit(tmp_path: Path) -> tuple[ToolRegistry, AuditLogger]:
    registry = ToolRegistry()
    registry.set_audit_logger(AuditLogger(tmp_path / "audit.jsonl"))
    return registry, AuditLogger(tmp_path / "audit.jsonl")


def _tool_rows(tmp_path: Path) -> list[dict[str, Any]]:
    log = tmp_path / "audit.jsonl"
    if not log.exists():
        return []
    rows = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
    # llm_call rows carry event_type and no tool_name; tool rows are the
    # inverse. Same discrimination the Activity endpoint makes.
    return [row for row in rows if "tool_name" in row]


async def _execute(
    tools: ToolRegistry,
    name: str,
    arguments: dict[str, Any] | None = None,
) -> tuple[list[Any], list[dict[str, str]], BaseException | None]:
    """One tool call through the live path with the runner's real defaults."""
    return await execute_tool_calls(
        tools,
        [ToolCallRequest(id="call-1", name=name, arguments=arguments or {})],
        concurrent=False,
        external_lookup_counts={},
        workspace_violation_counts={},
        hook=AgentHook(),
        context=AgentHookContext(iteration=0, messages=[]),
    )


def _turn_ctx(**overrides: Any) -> RequestContext:
    fields: dict[str, Any] = {
        "channel": "websocket",
        "chat_id": "tester-1",
        "session_key": "websocket:tester-1",
        "turn_id": "turn-abc",
    }
    fields.update(overrides)
    return RequestContext(**fields)


@pytest.mark.asyncio
async def test_ok_call_through_execute_tool_calls_writes_exactly_one_row(
    tmp_path: Path,
) -> None:
    tools, _audit = _registry_with_audit(tmp_path)
    tools.register(_ScriptedTool("echo", result="done"))

    with request_context(_turn_ctx()):
        results, events, fatal = await _execute(tools, "echo")

    assert fatal is None
    assert results == ["done"]
    assert events == [{"name": "echo", "status": "ok", "detail": "done"}]
    rows = _tool_rows(tmp_path)
    assert len(rows) == 1, f"expected exactly one tool row, got {rows!r}"
    row = rows[0]
    assert row["tool_name"] == "echo"
    assert row["result_status"] == "ok"
    assert isinstance(row.get("duration_ms"), (int, float)), row
    assert row["session_id"] == "websocket:tester-1"
    assert row["channel"] == "websocket"
    assert row["turn_id"] == "turn-abc"


@pytest.mark.asyncio
async def test_raising_tool_writes_one_error_row(tmp_path: Path) -> None:
    tools, _audit = _registry_with_audit(tmp_path)
    tools.register(_ScriptedTool("boom", raises=RuntimeError("x" * 300)))

    with request_context(_turn_ctx()):
        results, _events, fatal = await _execute(tools, "boom")

    assert fatal is None
    assert "RuntimeError" in str(results[0])
    rows = _tool_rows(tmp_path)
    assert len(rows) == 1, f"expected exactly one tool row, got {rows!r}"
    row = rows[0]
    assert row["tool_name"] == "boom"
    assert row["result_status"] == "error"
    assert row["error"] == "x" * 200  # str(exc) truncated to 200 chars


@pytest.mark.asyncio
async def test_read_only_turn_denial_writes_one_refused_row(tmp_path: Path) -> None:
    tools, _audit = _registry_with_audit(tmp_path)
    write_tool = _ScriptedTool("write_note", result="written", read_only=False)
    tools.register(write_tool)

    with request_context(_turn_ctx(metadata={"read_only": True})):
        _results, events, _fatal = await _execute(tools, "write_note")

    assert write_tool.calls == [], "denied tool must not execute"
    assert events[0]["status"] == "error"
    rows = _tool_rows(tmp_path)
    assert len(rows) == 1, f"expected exactly one tool row, got {rows!r}"
    assert rows[0]["tool_name"] == "write_note"
    assert rows[0]["result_status"] == "refused"


@pytest.mark.asyncio
async def test_room_denial_writes_one_refused_row(tmp_path: Path) -> None:
    """Negative control on the refused classifier: a *different* denial family
    (shared room, not read-only) must also land as ``refused``."""
    tools, _audit = _registry_with_audit(tmp_path)
    tools.register(_ScriptedTool("echo", result="done"))

    scope = {"room_id": "room-1", "chat_id": "tester-1", "participant_id": "p1", "role": "guest"}
    with request_context(_turn_ctx(metadata={INBOUND_META_ROOM_SCOPE: scope})):
        await _execute(tools, "echo")

    rows = _tool_rows(tmp_path)
    assert len(rows) == 1, f"expected exactly one tool row, got {rows!r}"
    assert rows[0]["result_status"] == "refused"


@pytest.mark.asyncio
async def test_non_denial_prepare_error_writes_error_not_refused(tmp_path: Path) -> None:
    """Negative control: a prepare_call failure that is *not* a room /
    read-only / ask_user denial (here: unknown tool name) records error."""
    tools, _audit = _registry_with_audit(tmp_path)
    tools.register(_ScriptedTool("echo", result="done"))

    with request_context(_turn_ctx()):
        await _execute(tools, "no_such_tool")

    rows = _tool_rows(tmp_path)
    assert len(rows) == 1, f"expected exactly one tool row, got {rows!r}"
    assert rows[0]["result_status"] == "error"
    assert rows[0]["tool_name"] == "no_such_tool"


@pytest.mark.asyncio
async def test_ask_user_interrupt_writes_one_waiting_row(tmp_path: Path) -> None:
    tools, _audit = _registry_with_audit(tmp_path)
    tools.register(
        _ScriptedTool("ask_later", raises=AskUserInterrupt("which file?"))
    )

    with request_context(_turn_ctx()):
        _results, _events, fatal = await _execute(tools, "ask_later")

    assert isinstance(fatal, AskUserInterrupt)
    rows = _tool_rows(tmp_path)
    assert len(rows) == 1, f"expected exactly one tool row, got {rows!r}"
    assert rows[0]["result_status"] == "waiting"


@pytest.mark.asyncio
async def test_direct_execute_writes_exactly_one_row(tmp_path: Path) -> None:
    """The registry's own ``execute`` audited before the fix; wiring the live
    path onto the same recorder must not double-write."""
    tools, _audit = _registry_with_audit(tmp_path)
    tools.register(_ScriptedTool("echo", result="done"))

    result = await tools.execute(
        "echo", {}, session_id="websocket:direct", channel="websocket",
    )

    assert str(result) == "done"
    assert not result.is_error
    rows = _tool_rows(tmp_path)
    assert len(rows) == 1, f"expected exactly one tool row, got {rows!r}"
    row = rows[0]
    assert row["tool_name"] == "echo"
    assert row["result_status"] == "ok"
    assert row["session_id"] == "websocket:direct"
    assert row["channel"] == "websocket"


@pytest.mark.asyncio
async def test_model_result_is_untouched_by_recording(tmp_path: Path) -> None:
    """Record-only: a result carrying an API-key-shaped string must reach the
    model byte-identically on the live path (redaction of the result is a
    separate decision, design §6 open question 5)."""
    secret_result = "token=sk-test-1234567890abcdef trailing"
    tools, _audit = _registry_with_audit(tmp_path)
    tools.register(_ScriptedTool("leaky", result=secret_result))

    with request_context(_turn_ctx()):
        results, events, fatal = await _execute(tools, "leaky")

    assert fatal is None
    assert results == [secret_result]
    assert "sk-test-1234567890abcdef" in events[0]["detail"]
    assert len(_tool_rows(tmp_path)) == 1


def _make_loop(tmp_path: Path) -> AgentLoop:
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = GenerationSettings()
    return AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        model="test-model",
        cron_service=MagicMock(),
    )


@pytest.mark.asyncio
async def test_restricted_registry_for_disabled_tools_writes_rows(
    tmp_path: Path,
) -> None:
    """The per-turn registry built for ``disabled_tools`` sessions had no
    audit logger at all; it must write rows like the main registry."""
    loop = _make_loop(tmp_path)
    loop.provider.chat_stream_with_retry = AsyncMock(
        side_effect=[
            LLMResponse(
                content=None,
                tool_calls=[ToolCallRequest(id="call-1", name="echo", arguments={})],
            ),
            LLMResponse(content="all done", tool_calls=[]),
        ]
    )
    loop.tools.register(_ScriptedTool("echo", result="done"))
    key = "websocket:restricted-tester"
    loop.sessions.get_or_create_transient(key, disabled_tools={"spawn"})

    outbound = await loop._process_message(
        InboundMessage(
            channel="websocket",
            sender_id="tester",
            chat_id="restricted-tester",
            content="please echo",
            session_key_override=key,
            require_existing_session=True,
        )
    )

    assert outbound is not None
    rows = _tool_rows(tmp_path)
    assert [row["tool_name"] for row in rows] == ["echo"], rows
    row = rows[0]
    assert row["result_status"] == "ok"
    assert row["session_id"] == key


@pytest.mark.asyncio
async def test_llm_call_row_uses_the_turn_runtime_model(tmp_path: Path) -> None:
    """MIT-1849 item 5: the per-iteration llm_call row records the turn's
    ``runtime.model``, not the loop default — a session pinned to a preset
    answers under another model, and the Activity screen must say which."""
    loop = _make_loop(tmp_path)

    def _all_rows() -> list[dict[str, Any]]:
        return [
            json.loads(line)
            for line in (tmp_path / "audit.jsonl").read_text().splitlines()
            if line.strip()
        ]

    preset_runtime = LLMRuntime(
        provider=loop.provider,
        model="preset-model",
        generation=GenerationSettings(),
        context_window_tokens=4096,
    )
    with request_context(_turn_ctx(runtime=preset_runtime)):
        hook = _ZiggyTurnHook(loop, AgentTurnHookContext())
        await hook.after_iteration(
            AgentHookContext(iteration=0, messages=[], latency_ms=5.0, response=None),
        )
    # The fallback: a hook built with no bound request context keeps the
    # loop default.
    fallback_hook = _ZiggyTurnHook(loop, AgentTurnHookContext())
    await fallback_hook.after_iteration(
        AgentHookContext(iteration=0, messages=[], latency_ms=5.0, response=None),
    )

    llm_rows = [row for row in _all_rows() if row.get("event_type") == "llm_call"]
    assert [row["model"] for row in llm_rows] == ["preset-model", "test-model"], llm_rows
