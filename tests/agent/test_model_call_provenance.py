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


# ---------------------------------------------------------------------------
# Review round 1 (MIT-1862)
# ---------------------------------------------------------------------------

import hashlib  # noqa: E402
import json  # noqa: E402
from contextlib import contextmanager  # noqa: E402

from nanobot.agent import runner as runner_mod  # noqa: E402
from nanobot.providers.base import LLMUsage  # noqa: E402


def _expected_sha(defs: list[dict[str, Any]] | None) -> str:
    """Independent statement of the fingerprint contract (design section 3)."""
    payload = json.dumps(
        sorted(defs or [], key=lambda d: d.get("function", d).get("name", "")),
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


class _FakeSpan:
    def __init__(self, metadata: dict[str, Any]) -> None:
        self.metadata = dict(metadata)
        self.updates: list[dict[str, Any]] = []

    def update(self, *, metadata: dict[str, Any]) -> None:
        self.updates.append(dict(metadata))
        self.metadata = dict(metadata)


class _FakeLangfuse:
    def __init__(self) -> None:
        self.spans: list[_FakeSpan] = []

    @contextmanager
    def start_as_current_observation(self, *, name: str, as_type: str, metadata: dict[str, Any]):
        span = _FakeSpan(metadata)
        span.name = name  # type: ignore[attr-defined]
        self.spans.append(span)
        yield span


def _malformed_then_recovered_provider(seen_tools: list[Any]) -> MagicMock:
    provider = MagicMock(spec=LLMProvider)

    async def chat_stream_with_retry(*, messages, tools=None, **kwargs):
        seen_tools.append(tools)
        if len(seen_tools) < 3:
            return LLMResponse(
                content="bad tool request",
                tool_calls=[ToolCallRequest(id=f"bad{len(seen_tools)}", name=None, arguments={})],
                finish_reason="tool_calls",
                usage=LLMUsage.reported(input_tokens=10, output_tokens=1),
            )
        return LLMResponse(content="recovered", usage=LLMUsage.reported(input_tokens=10, output_tokens=1))

    provider.chat_stream_with_retry = chat_stream_with_retry
    return provider


@pytest.mark.asyncio
async def test_fingerprint_failure_never_fails_the_run(monkeypatch) -> None:
    def boom(defs: Any) -> Any:
        raise RuntimeError("hash exploded")

    monkeypatch.setattr(runner_mod, "tools_offered_fingerprint", boom)
    record = TurnProvenance(turn_id="t-boom")
    token = bind_turn_provenance(record)
    try:
        result, _lines = await _run_turn(tools=_registry_with([]), tool_name="alpha_tool")
    finally:
        reset_turn_provenance(token)
    assert result.final_content == "done"
    assert record.calls == []


@pytest.mark.asyncio
async def test_release_lookup_failure_never_fails_the_run(monkeypatch) -> None:
    def boom() -> str:
        raise RuntimeError("release exploded")

    monkeypatch.setattr(runner_mod, "release_id", boom)
    result, _lines = await _run_turn(tools=_registry_with([]), tool_name=None)
    assert result.final_content == "done"


@pytest.mark.asyncio
async def test_no_tools_recovery_call_is_recorded_on_every_surface(monkeypatch) -> None:
    """Two malformed tool responses then a no-tools recovery: 3 provider calls,
    3 calls entries, 3 log lines; the recovery records what it was actually
    offered (nothing), and the Langfuse iteration ends on that count."""
    fake = _FakeLangfuse()
    monkeypatch.setattr(lf, "_safe_get_client", lambda: fake)
    seen_tools: list[Any] = []
    provider = _malformed_then_recovered_provider(seen_tools)
    tools = _registry_with([])
    record = TurnProvenance(turn_id="t-rec")
    token = bind_turn_provenance(record)
    lines: list[str] = []
    sink_id = logger.add(lambda m: lines.append(str(m)), level="INFO")
    try:
        result = await AgentRunner().run(make_run_spec(
            provider,
            initial_messages=[{"role": "user", "content": "use a tool"}],
            tools=tools,
            model="test-model",
            max_iterations=1,
            max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
            turn_id="t-rec",
        ))
    finally:
        logger.remove(sink_id)
        reset_turn_provenance(token)

    assert result.final_content == "recovered"
    assert len(seen_tools) == 3 and seen_tools[2] is None
    assert [(c["tools_n"], c["tools_sha"]) for c in record.calls] == [
        (2, _expected_sha(seen_tools[0])),
        (2, _expected_sha(seen_tools[1])),
        (0, _expected_sha([])),
    ]
    assert len([line for line in lines if "model_call turn=t-rec" in line]) == 3
    iteration_spans = [s for s in fake.spans if s.name.startswith("llm-iteration-")]  # type: ignore[attr-defined]
    assert len(iteration_spans) == 1
    final_meta = iteration_spans[0].metadata
    assert final_meta["ziggy.tools.offered.count"] == 0
    assert final_meta["ziggy.tools.offered.sha"] == _expected_sha([])


@pytest.mark.asyncio
async def test_runner_records_the_real_sha_and_updates_the_langfuse_span(monkeypatch) -> None:
    """End to end through AgentRunner.run: the recorded sha is the digest of the
    definitions the provider actually received, the two registries differ, and
    the open Langfuse iteration span carries the same values."""
    fake = _FakeLangfuse()
    monkeypatch.setattr(lf, "_safe_get_client", lambda: fake)
    shas: list[str] = []
    for extra in ([], ["gamma_tool"]):
        seen: list[Any] = []
        provider = _scripted_provider(tool_name=None)
        original = provider.chat_stream_with_retry

        async def spy(*, messages, tools=None, _seen=seen, _orig=original, **kwargs):
            _seen.append(tools)
            return await _orig(messages=messages, tools=tools, **kwargs)

        provider.chat_stream_with_retry = spy
        record = TurnProvenance(turn_id="t-sha")
        token = bind_turn_provenance(record)
        try:
            await AgentRunner().run(make_run_spec(
                provider,
                initial_messages=[{"role": "user", "content": "hi"}],
                tools=_registry_with(extra),
                model="test-model",
                max_iterations=1,
                max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
                turn_id="t-sha",
                reasoning_profile="think",
            ))
        finally:
            reset_turn_provenance(token)
        assert record.calls[0]["tools_sha"] == _expected_sha(seen[0])
        shas.append(record.calls[0]["tools_sha"])
        span = [s for s in fake.spans if s.name == "llm-iteration-0"][-1]  # type: ignore[attr-defined]
        assert span.updates, "the runner must update the open iteration span"
        assert span.metadata["ziggy.tools.offered.sha"] == record.calls[0]["tools_sha"]
        assert span.metadata["ziggy.tools.offered.count"] == len(seen[0])
        assert span.metadata["ziggy.turn_id"] == "t-sha"
        assert span.metadata["ziggy.reasoning.profile"] == "think"
        assert span.metadata["iteration"] == 0
    assert shas[0] != shas[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["inline", "background"])
async def test_subagent_keeps_its_own_record_and_carries_the_parent_turn_id(
    tmp_path, mode: str
) -> None:
    """One rule for inline and background children: the child never appends to
    the parent's record; its usage and logs carry the parent turn id; its own
    record carries ``parent_turn_id``."""
    import asyncio

    from nanobot.agent.subagent import SubagentManager
    from nanobot.bus.queue import MessageBus
    from nanobot.providers.base import GenerationSettings
    from nanobot.utils.llm_runtime import LLMRuntime

    usage_turn_ids: list[str | None] = []
    provider = MagicMock(spec=LLMProvider)
    provider.get_default_model.return_value = "test"
    provider.generation = GenerationSettings()

    async def chat_stream_with_retry(*, messages, **kwargs):
        usage_turn_ids.append(current_llm_usage_turn_id())
        return LLMResponse(content="child done", tool_calls=[])

    provider.chat_stream_with_retry = chat_stream_with_retry
    runtime = LLMRuntime.capture(provider, "test", context_window_tokens=128_000)
    sm = SubagentManager(workspace=tmp_path, bus=MessageBus(), max_tool_result_chars=16_000)
    sm._announce_result = MagicMock(side_effect=lambda *a, **k: asyncio.sleep(0))

    parent = TurnProvenance(turn_id="parent-1")
    record_token = bind_turn_provenance(parent)
    turn_token = bind_llm_usage_turn_id("parent-1")
    lines: list[str] = []
    sink_id = logger.add(lambda m: lines.append(str(m)), level="INFO")
    try:
        if mode == "inline":
            result = await sm.run_inline(task="do it", session_key="websocket:t", runtime=runtime)
            assert result == "child done"
        else:
            await sm.spawn(task="do it", session_key="websocket:t", runtime=runtime)
            await asyncio.gather(*list(sm._running_tasks.values()))
    finally:
        logger.remove(sink_id)
        reset_llm_usage_turn_id(turn_token)
        reset_turn_provenance(record_token)

    assert parent.calls == []
    assert usage_turn_ids == ["parent-1"]
    assert [line for line in lines if "model_call turn=parent-1" in line]
    child_lines = [line for line in lines if "subagent_provenance " in line]
    assert len(child_lines) == 1
    child = json.loads(child_lines[0].split("subagent_provenance ", 1)[1].strip())
    assert child["parent_turn_id"] == "parent-1"
    assert child["turn_id"].startswith("subagent:")
    assert len(child["calls"]) == 1


# ---------------------------------------------------------------------------
# Review round 2 (MIT-1862)
# ---------------------------------------------------------------------------

_LEAK_SENTINEL = "SENTINEL-hunter2-pw"


@pytest.mark.asyncio
async def test_recording_failure_log_is_content_free(monkeypatch) -> None:
    """With Loguru diagnose on (prod), a recording failure must not print the
    exception text, a traceback or frame locals (user text, tool arguments)."""

    def boom(defs: Any) -> Any:
        raise RuntimeError(f"hash exploded {_LEAK_SENTINEL}")

    monkeypatch.setattr(runner_mod, "tools_offered_fingerprint", boom)
    provider = MagicMock(spec=LLMProvider)
    state = {"n": 0}

    async def chat_stream_with_retry(*, messages, **kwargs):
        state["n"] += 1
        if state["n"] == 1:
            return LLMResponse(
                content="working",
                tool_calls=[ToolCallRequest(
                    id="c1", name="alpha_tool", arguments={"password": _LEAK_SENTINEL},
                )],
            )
        return LLMResponse(content="done", tool_calls=[])

    provider.chat_stream_with_retry = chat_stream_with_retry
    lines: list[str] = []
    sink_id = logger.add(
        lambda m: lines.append(str(m)), level="WARNING", diagnose=True, backtrace=True
    )
    try:
        result = await AgentRunner().run(make_run_spec(
            provider,
            initial_messages=[{"role": "user", "content": f"my password is {_LEAK_SENTINEL}"}],
            tools=_registry_with([]),
            model="test-model",
            max_iterations=3,
            max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
            turn_id="t-leak",
        ))
    finally:
        logger.remove(sink_id)
    assert result.final_content == "done"
    joined = "".join(lines)
    assert "recording a model call failed (RuntimeError)" in joined
    assert _LEAK_SENTINEL not in joined
    assert "Traceback" not in joined


@pytest.mark.asyncio
async def test_budget_finalization_gets_its_own_span_with_zero_tools(monkeypatch) -> None:
    """max_iterations reached -> no-tools finalization: 2 provider calls,
    2 calls entries, 2 iteration spans; the second carries the zero-tool sha."""
    fake = _FakeLangfuse()
    monkeypatch.setattr(lf, "_safe_get_client", lambda: fake)
    seen_tools: list[Any] = []
    provider = MagicMock(spec=LLMProvider)

    async def chat_stream_with_retry(*, messages, tools=None, **kwargs):
        seen_tools.append(tools)
        if tools:
            return LLMResponse(
                content="working",
                tool_calls=[ToolCallRequest(id="c1", name="alpha_tool", arguments={})],
            )
        return LLMResponse(content="final summary", tool_calls=[])

    provider.chat_stream_with_retry = chat_stream_with_retry
    record = TurnProvenance(turn_id="t-budget")
    token = bind_turn_provenance(record)
    try:
        result = await AgentRunner().run(make_run_spec(
            provider,
            initial_messages=[{"role": "user", "content": "go"}],
            tools=_registry_with([]),
            model="test-model",
            max_iterations=1,
            max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
            turn_id="t-budget",
        ))
    finally:
        reset_turn_provenance(token)
    assert result.final_content == "final summary"
    assert len(seen_tools) == 2 and seen_tools[1] is None
    assert [c["tools_n"] for c in record.calls] == [2, 0]
    spans = [s for s in fake.spans if s.name.startswith("llm-iteration-")]  # type: ignore[attr-defined]
    assert len(spans) == 2
    assert spans[0].metadata["ziggy.tools.offered.count"] == 2
    assert spans[1].metadata["ziggy.tools.offered.count"] == 0
    assert spans[1].metadata["ziggy.tools.offered.sha"] == _expected_sha([])
