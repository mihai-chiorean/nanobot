"""Per-model-call provenance record (MIT-1862 / TP-05).

Design: docs/design/turn-provenance.md §3 (ziggy repo). Every call sends the
whole registry (``runner._request_model`` -> ``spec.tools.get_definitions()``)
but nothing recorded *which* set. These tests pin the three surfaces TP-05
adds, all content-free:

* ``tools_offered_fingerprint``: count + order-stable 16-hex sha256 prefix;
* the ``model_call`` INFO log line and the turn record's ``calls`` list:
  one entry per iteration, no message text;
* ``llm_usage`` rows keyed on the turn id bound by the runner next to
  ``source``, read by ``LLMProvider._observe_llm_call``.

Drives the real ``AgentRunner.run`` with a scripted provider, as the loop
does (``tests/agent/test_runner_core.py`` pattern).
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import MagicMock

import pytest
from loguru import logger

from agent.runner_helpers import make_run_spec
from nanobot.agent.runner import AgentRunner, tools_offered_fingerprint
from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.turn_provenance import (
    TurnProvenance,
    bind_turn_provenance,
    reset_turn_provenance,
)
from nanobot.config.schema import AgentDefaults
from nanobot.llm_usage.context import (
    bind_llm_usage_turn_id,
    current_llm_usage_turn_id,
    reset_llm_usage_turn_id,
)
from nanobot.observability import langfuse as lf
from nanobot.providers.base import (
    LLMProvider,
    LLMResponse,
    ToolCallRequest,
)

_MAX_TOOL_RESULT_CHARS = AgentDefaults().max_tool_result_chars
_SECRET_MARKER = "ZZTOPSECRET-quiet-fox"


class _MarkerTool(Tool):
    """Minimal tool; its name is the only thing the fingerprint sees."""

    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f"marker tool {self._name}"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs: Any) -> Any:
        return "ok"


def _registry_with(extra_names: list[str]) -> ToolRegistry:
    registry = ToolRegistry()
    for name in ["alpha_tool", "beta_tool", *extra_names]:
        registry.register(_MarkerTool(name))
    return registry


def _scripted_provider(tool_name: str | None) -> MagicMock:
    """Provider whose first round optionally calls a tool, then finishes."""
    provider = MagicMock(spec=LLMProvider)
    state = {"n": 0}

    async def chat_stream_with_retry(*, messages, **kwargs):
        state["n"] += 1
        if state["n"] == 1 and tool_name is not None:
            return LLMResponse(
                content="working",
                tool_calls=[ToolCallRequest(id="call-1", name=tool_name, arguments={})],
            )
        return LLMResponse(content="done", tool_calls=[])

    provider.chat_stream_with_retry = chat_stream_with_retry
    return provider


async def _run_turn(
    *,
    tools: ToolRegistry,
    tool_name: str | None,
    turn_id: str | None = None,
    reasoning_profile: str | None = "think",
) -> tuple[Any, list[str]]:
    """One full runner turn; returns the result and the captured log lines."""
    provider = _scripted_provider(tool_name)
    runner = AgentRunner()
    spec = make_run_spec(
        provider,
        initial_messages=[{"role": "user", "content": f"hello {_SECRET_MARKER}"}],
        tools=tools,
        model="test-model",
        max_iterations=3,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
        session_key="websocket:tester",
        turn_id=turn_id,
        reasoning_profile=reasoning_profile,
    )
    lines: list[str] = []
    sink_id = logger.add(lambda message: lines.append(str(message)), level="INFO")
    try:
        result = await runner.run(spec)
    finally:
        logger.remove(sink_id)
    return result, lines


# ---------------------------------------------------------------------------
# tools_offered_fingerprint
# ---------------------------------------------------------------------------


def test_fingerprint_count_and_sha_differ_across_registries_one_tool_apart() -> None:
    base = _registry_with([])
    extended = _registry_with(["gamma_tool"])

    n_base, sha_base = tools_offered_fingerprint(base.get_definitions())
    n_ext, sha_ext = tools_offered_fingerprint(extended.get_definitions())

    assert (n_base, n_ext) == (2, 3)
    assert sha_base != sha_ext
    assert len(sha_base) == 16 and len(sha_ext) == 16
    assert all(c in "0123456789abcdef" for c in sha_base + sha_ext)


def test_fingerprint_is_stable_for_the_same_registry() -> None:
    registry = _registry_with(["gamma_tool"])
    first = tools_offered_fingerprint(registry.get_definitions())
    second = tools_offered_fingerprint(registry.get_definitions())
    assert first == second


def test_fingerprint_ignores_definition_order() -> None:
    defs = _registry_with([]).get_definitions()
    assert tools_offered_fingerprint(defs) == tools_offered_fingerprint(list(reversed(defs)))


def test_fingerprint_distinguishes_same_count_different_names() -> None:
    left = _registry_with([])
    right = ToolRegistry()
    right.register(_MarkerTool("alpha_tool"))
    right.register(_MarkerTool("zeta_tool"))

    n_left, sha_left = tools_offered_fingerprint(left.get_definitions())
    n_right, sha_right = tools_offered_fingerprint(right.get_definitions())

    assert n_left == n_right == 2
    assert sha_left != sha_right


# ---------------------------------------------------------------------------
# runner: log line + turn record calls
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_turn_record_gets_one_calls_entry_per_iteration() -> None:
    record = TurnProvenance(turn_id="turn-42", reasoning_profile="think")
    token = bind_turn_provenance(record)
    try:
        _result, _lines = await _run_turn(
            tools=_registry_with([]), tool_name="alpha_tool", turn_id="turn-42"
        )
    finally:
        reset_turn_provenance(token)

    # iteration 0 issues the tool call, iteration 1 finishes the turn.
    assert len(record.calls) == 2
    assert [entry["iter"] for entry in record.calls] == [0, 1]
    for entry in record.calls:
        assert entry == {
            "iter": entry["iter"],
            "tools_n": 2,
            "tools_sha": record.calls[0]["tools_sha"],
            "profile": "think",
            "model": "test-model",
        }


@pytest.mark.asyncio
async def test_model_call_log_line_has_counts_and_no_content() -> None:
    _result, lines = await _run_turn(
        tools=_registry_with(["gamma_tool"]),
        tool_name="alpha_tool",
        turn_id="turn-9",
        reasoning_profile="fast",
    )

    model_call_lines = [line for line in lines if "model_call turn=" in line]
    assert len(model_call_lines) == 2
    for line in model_call_lines:
        assert "tools n=3" in line
        assert "sha=" in line
        assert "profile=fast" in line
        assert "model=test-model" in line
        assert "release=" in line
        # Content-free by contract: neither the user message nor the tool
        # result nor any argument value may appear.
        assert _SECRET_MARKER not in line
        assert "hello" not in line
        assert "arguments" not in line


@pytest.mark.asyncio
async def test_unbound_provenance_record_does_not_break_the_turn() -> None:
    # current_turn_provenance() is None outside a turn (subagents, tests):
    # the runner must log and carry on.
    result, lines = await _run_turn(tools=_registry_with([]), tool_name=None)
    assert result.final_content == "done"
    assert len([line for line in lines if "model_call turn=" in line]) == 1


@pytest.mark.asyncio
async def test_runner_binds_usage_turn_id_around_provider_calls() -> None:
    seen: list[str | None] = []
    provider = _scripted_provider(tool_name=None)
    original = provider.chat_stream_with_retry

    async def spy(*, messages, **kwargs):
        seen.append(current_llm_usage_turn_id())
        return await original(messages=messages, **kwargs)

    provider.chat_stream_with_retry = spy
    runner = AgentRunner()
    spec = make_run_spec(
        provider,
        initial_messages=[{"role": "user", "content": "hi"}],
        tools=_registry_with([]),
        model="test-model",
        max_iterations=1,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
        turn_id="turn-77",
    )
    assert current_llm_usage_turn_id() is None
    await runner.run(spec)
    assert seen == ["turn-77"]
    # Reset on the way out of run(), like source.
    assert current_llm_usage_turn_id() is None


# ---------------------------------------------------------------------------
# providers/base._observe_llm_call reads the bound turn id
# ---------------------------------------------------------------------------


def test_observe_llm_call_tags_record_with_bound_turn_id() -> None:
    captured: list[Any] = []
    # Unspec'd: _llm_call_observer/provider_name are instance attributes set
    # in LLMProvider.__init__, outside the reach of a class spec. The real
    # unbound method is called with this as ``self``.
    provider = MagicMock()
    provider._llm_call_observer = captured.append
    provider._usage_for_call.return_value = None
    provider.provider_name = "fake"
    provider.get_default_model.return_value = "test-model"
    response = LLMResponse(content="ok", tool_calls=[], finish_reason="stop")

    token = bind_llm_usage_turn_id("t1")
    try:
        LLMProvider._observe_llm_call(
            provider,
            response,
            {"model": "test-model"},
            started_at_ms=0,
            started_at_ns=time.monotonic_ns(),
            stream=False,
        )
    finally:
        reset_llm_usage_turn_id(token)

    assert len(captured) == 1
    assert captured[0].turn_id == "t1"


# ---------------------------------------------------------------------------
# Langfuse iteration-span metadata
# ---------------------------------------------------------------------------


def test_update_llm_iteration_metadata_merges_into_open_span() -> None:
    class _FakeSpan:
        def __init__(self) -> None:
            self.metadata: dict[str, Any] = {"iteration": 0, "model": "test-model"}
            self.updates: list[dict[str, Any]] = []

        def update(self, *, metadata: dict[str, Any]) -> None:
            self.updates.append(metadata)

    span = _FakeSpan()
    token = lf._CURRENT_LLM_ITERATION_SPAN.set(span)
    try:
        lf.update_llm_iteration_metadata(
            {"ziggy.turn_id": "turn-1", "ziggy.tools.offered.count": 3}
        )
    finally:
        lf._CURRENT_LLM_ITERATION_SPAN.reset(token)

    assert span.updates == [
        {
            "iteration": 0,
            "model": "test-model",
            "ziggy.turn_id": "turn-1",
            "ziggy.tools.offered.count": 3,
        }
    ]


def test_update_llm_iteration_metadata_is_a_noop_without_a_span() -> None:
    # Langfuse disabled: the runner calls this on every model call; it must
    # degrade silently, as the whole module does.
    lf.update_llm_iteration_metadata({"ziggy.turn_id": "turn-1"})


def test_observe_llm_iteration_accepts_metadata_and_tracks_the_span() -> None:
    # Fail-safe path (Langfuse unavailable here): still yields None and
    # leaves no stale span behind for the update helper.
    with lf.observe_llm_iteration(iteration=0, model="m", metadata={"ziggy.release": "r"}) as span:
        assert span is None
        assert lf._CURRENT_LLM_ITERATION_SPAN.get() is None
    assert lf._CURRENT_LLM_ITERATION_SPAN.get() is None
