"""Tests for MCP hot reload driven by the gateway config watcher.

Covers ``_config_change_handler``: a config write still invalidates the LLM
runtime, and the ``mcpServers`` section is reconciled live -- servers added,
removed, or changed (including ``enabledTools``) take effect without a
restart, unchanged MCP config does not reconnect anything, and a failing
reload leaves the previously registered tool set in place.
"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

import pytest

from nanobot.agent.tools import mcp as mcp_mod
from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.mcp import MCPProvider
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.cli import gateway_runtime
from nanobot.cli.gateway_runtime import _config_change_handler, _hot_reload_mcp_servers
from nanobot.config.schema import MCPServerConfig

_SERVER_TOOLS = ("navigate", "screenshot")


class _FakeMcpTool(Tool):
    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return "fake MCP tool"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, **_kwargs: Any) -> str:
        return "ok"


class _FakeAgent:
    def __init__(self) -> None:
        self.invalidations = 0

    def invalidate_runtime_config(self) -> None:
        self.invalidations += 1


class _ConfigSource:
    """Mutable stand-in for the ``mcpServers`` section read from config.json."""

    def __init__(self, servers: dict[str, MCPServerConfig]) -> None:
        self.servers = dict(servers)
        self.loads = 0
        self.error: Exception | None = None

    def __call__(self) -> dict[str, MCPServerConfig]:
        self.loads += 1
        if self.error is not None:
            raise self.error
        return dict(self.servers)


class _FakeConnections:
    """Record ``connect_mcp_servers`` calls and register per-server fake tools."""

    def __init__(self) -> None:
        self.attempts: list[dict[str, MCPServerConfig]] = []
        self.closed: list[str] = []
        self.refuse: set[str] = set()
        self.error: BaseException | None = None
        self.gate: asyncio.Event | None = None

    async def _mark_closed(self, name: str) -> None:
        self.closed.append(name)

    async def __call__(self, servers: dict[str, MCPServerConfig], registry: ToolRegistry) -> dict:
        self.attempts.append(dict(servers))
        if self.gate is not None:
            await self.gate.wait()
        if self.error is not None:
            raise self.error
        stacks: dict[str, AsyncExitStack] = {}
        for name, cfg in servers.items():
            if name in self.refuse:
                continue
            for tool in _SERVER_TOOLS:
                if "*" in cfg.enabled_tools or tool in cfg.enabled_tools:
                    registry.register(_FakeMcpTool(f"mcp_{name}_{tool}"))
            stack = AsyncExitStack()
            await stack.__aenter__()
            stack.push_async_callback(self._mark_closed, name)
            stacks[name] = stack
        return stacks


def _stdio_server(enabled_tools: list[str] | None = None) -> MCPServerConfig:
    return MCPServerConfig(
        type="stdio",
        command="browserbase-mcp",
        enabled_tools=enabled_tools if enabled_tools is not None else ["*"],
    )


async def _live_runtime(
    monkeypatch: pytest.MonkeyPatch,
    servers: dict[str, MCPServerConfig],
) -> tuple[MCPProvider, ToolRegistry, _ConfigSource, _FakeConnections]:
    connections = _FakeConnections()
    monkeypatch.setattr(mcp_mod, "connect_mcp_servers", connections)
    source = _ConfigSource(servers)
    registry = ToolRegistry()
    provider = MCPProvider(source(), registry, server_loader=source)
    await provider.connect()
    return provider, registry, source, connections


def _capture_logs() -> tuple[int, list[Any]]:
    """Capture records emitted by the gateway runtime module only."""
    records: list[Any] = []
    sink = gateway_runtime.logger.add(
        lambda message: records.append(message.record),
        level="DEBUG",
        filter=lambda record: record["name"] == gateway_runtime.__name__,
    )
    return sink, records


def _handler(agent: _FakeAgent, provider: MCPProvider, config_path: Path):
    config_path.write_text("{}", encoding="utf-8")
    return _config_change_handler(agent, provider, config_path)


@pytest.mark.asyncio
async def test_config_change_adds_server_and_registers_its_tools(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    provider, registry, source, connections = await _live_runtime(monkeypatch, {})
    agent = _FakeAgent()
    on_change = _handler(agent, provider, tmp_path / "config.json")

    source.servers["browserbase"] = _stdio_server()
    await on_change()

    assert agent.invalidations == 1
    assert registry.has("mcp_browserbase_navigate")
    assert registry.has("mcp_browserbase_screenshot")
    assert provider.connected_server_names == {"browserbase"}
    assert [sorted(attempt) for attempt in connections.attempts] == [["browserbase"]]


@pytest.mark.asyncio
async def test_config_change_removes_server_and_unregisters_its_tools(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    provider, registry, source, connections = await _live_runtime(
        monkeypatch, {"browserbase": _stdio_server()}
    )
    assert registry.has("mcp_browserbase_navigate")
    on_change = _handler(_FakeAgent(), provider, tmp_path / "config.json")

    del source.servers["browserbase"]
    await on_change()

    assert not registry.has("mcp_browserbase_navigate")
    assert not registry.has("mcp_browserbase_screenshot")
    assert provider.connected_server_names == set()
    assert connections.closed == ["browserbase"]


@pytest.mark.asyncio
async def test_enabled_tools_change_reconnects_and_updates_tool_set(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    provider, registry, source, connections = await _live_runtime(
        monkeypatch, {"browserbase": _stdio_server()}
    )
    assert registry.has("mcp_browserbase_screenshot")
    on_change = _handler(_FakeAgent(), provider, tmp_path / "config.json")

    source.servers["browserbase"] = _stdio_server(["navigate"])
    await on_change()

    assert registry.has("mcp_browserbase_navigate")
    assert not registry.has("mcp_browserbase_screenshot")
    assert connections.closed == ["browserbase"]
    assert connections.attempts[-1]["browserbase"].enabled_tools == ["navigate"]
    assert provider.connected_server_names == {"browserbase"}
    assert provider.has_pending_config_changes() is False


@pytest.mark.asyncio
async def test_unchanged_mcp_config_does_not_reload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    provider, registry, source, connections = await _live_runtime(
        monkeypatch, {"browserbase": _stdio_server()}
    )
    reload_calls = 0

    async def _reload() -> dict[str, Any]:
        nonlocal reload_calls
        reload_calls += 1
        raise AssertionError("reload() must not run for an unchanged mcpServers section")

    monkeypatch.setattr(provider, "reload", _reload)
    agent = _FakeAgent()
    on_change = _handler(agent, provider, tmp_path / "config.json")

    # A model/preset edit rewrites config.json without touching mcpServers.
    await on_change()

    assert agent.invalidations == 1
    assert source.loads == 2  # construction + the pending-change check
    assert reload_calls == 0
    assert len(connections.attempts) == 1
    assert connections.closed == []
    assert registry.has("mcp_browserbase_navigate")


@pytest.mark.asyncio
async def test_reload_failure_keeps_unchanged_tools_logs_error_and_readiness_recovers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    provider, registry, source, connections = await _live_runtime(
        monkeypatch, {"browserbase": _stdio_server()}
    )
    on_change = _handler(_FakeAgent(), provider, tmp_path / "config.json")
    connections.error = RuntimeError("linkedin-mcp exploded")
    sink, records = _capture_logs()
    try:
        source.servers["linkedin"] = MCPServerConfig(type="stdio", command="linkedin-mcp")
        await on_change()
    finally:
        gateway_runtime.logger.remove(sink)

    # The unchanged server is never touched; the new one is recorded as failed.
    assert registry.has("mcp_browserbase_navigate")
    assert registry.has("mcp_browserbase_screenshot")
    assert not registry.has("mcp_linkedin_navigate")
    assert provider.connected_server_names == {"browserbase"}
    assert connections.closed == []
    assert provider.runtime_status() == {"browserbase": "connected", "linkedin": "failed"}
    errors = [r for r in records if r["level"].name == "ERROR"]
    assert len(errors) == 1
    assert "MCP hot reload failed" in errors[0]["message"]
    assert errors[0]["exception"] is not None

    # reload() already applied the new config, so the readiness hook (not the
    # next config write) is what brings the failed server up.
    assert provider.has_pending_config_changes() is False
    connections.error = None
    await provider.connect()

    assert registry.has("mcp_linkedin_navigate")
    assert provider.connected_server_names == {"browserbase", "linkedin"}


@pytest.mark.asyncio
async def test_reload_timeout_is_bounded_and_logged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider, registry, source, connections = await _live_runtime(
        monkeypatch, {"browserbase": _stdio_server()}
    )
    connections.gate = asyncio.Event()  # never set: the new server hangs on connect
    sink, records = _capture_logs()
    try:
        source.servers["linkedin"] = MCPServerConfig(type="stdio", command="linkedin-mcp")
        result = await asyncio.wait_for(
            _hot_reload_mcp_servers(provider, timeout_s=0.05), timeout=2.0
        )
    finally:
        gateway_runtime.logger.remove(sink)

    assert result is None
    assert registry.has("mcp_browserbase_navigate")
    assert provider.connected_server_names == {"browserbase"}
    assert provider.runtime_status()["linkedin"] == "failed"
    warnings = [r for r in records if r["level"].name == "WARNING"]
    assert len(warnings) == 1
    assert "timed out" in warnings[0]["message"]


@pytest.mark.asyncio
async def test_spurious_cancellation_from_reload_does_not_escape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider, registry, source, connections = await _live_runtime(
        monkeypatch, {"browserbase": _stdio_server()}
    )
    connections.error = asyncio.CancelledError()
    sink, records = _capture_logs()
    try:
        source.servers["linkedin"] = MCPServerConfig(type="stdio", command="linkedin-mcp")
        result = await asyncio.create_task(_hot_reload_mcp_servers(provider))
    finally:
        gateway_runtime.logger.remove(sink)

    assert result is None
    assert registry.has("mcp_browserbase_navigate")
    warnings = [r for r in records if r["level"].name == "WARNING"]
    assert len(warnings) == 1
    assert "cancelled" in warnings[0]["message"]


@pytest.mark.asyncio
async def test_real_cancellation_propagates_from_reload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider, _registry, source, connections = await _live_runtime(
        monkeypatch, {"browserbase": _stdio_server()}
    )
    connections.gate = asyncio.Event()
    source.servers["linkedin"] = MCPServerConfig(type="stdio", command="linkedin-mcp")
    task = asyncio.create_task(_hot_reload_mcp_servers(provider))
    while len(connections.attempts) < 2:
        await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_missing_config_file_keeps_tools_and_skips_reload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    provider, registry, source, connections = await _live_runtime(
        monkeypatch, {"browserbase": _stdio_server()}
    )
    # A missing file loads as environment defaults, i.e. an empty mcpServers.
    source.servers = {}
    agent = _FakeAgent()
    on_change = _config_change_handler(agent, provider, tmp_path / "config.json")
    sink, records = _capture_logs()
    try:
        await on_change()
    finally:
        gateway_runtime.logger.remove(sink)

    assert agent.invalidations == 1
    assert registry.has("mcp_browserbase_navigate")
    assert provider.connected_server_names == {"browserbase"}
    assert connections.closed == []
    assert source.loads == 1
    warnings = [r for r in records if r["level"].name == "WARNING"]
    assert len(warnings) == 1
    assert "is missing" in warnings[0]["message"]


@pytest.mark.asyncio
async def test_reload_during_shutdown_is_logged_at_info(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider, _registry, source, _connections = await _live_runtime(
        monkeypatch, {"browserbase": _stdio_server()}
    )
    await provider.aclose()
    sink, records = _capture_logs()
    try:
        source.servers["linkedin"] = MCPServerConfig(type="stdio", command="linkedin-mcp")
        result = await _hot_reload_mcp_servers(provider)
    finally:
        gateway_runtime.logger.remove(sink)

    assert result is not None
    assert result["requires_restart"] is True
    assert [r["level"].name for r in records] == ["INFO"]


@pytest.mark.asyncio
async def test_readiness_connect_waits_for_in_flight_reload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    provider, registry, source, connections = await _live_runtime(
        monkeypatch, {"browserbase": _stdio_server()}
    )
    on_change = _handler(_FakeAgent(), provider, tmp_path / "config.json")
    connections.gate = asyncio.Event()
    source.servers["linkedin"] = MCPServerConfig(type="stdio", command="linkedin-mcp")
    reload_task = asyncio.create_task(on_change())
    while len(connections.attempts) < 2:
        await asyncio.sleep(0)

    # The pre-turn readiness hook blocks on the provider lock until the reload
    # has registered the new tool set.
    connect_task = asyncio.create_task(provider.connect())
    await asyncio.sleep(0.01)
    assert not connect_task.done()
    assert not registry.has("mcp_linkedin_navigate")

    connections.gate.set()
    await asyncio.wait_for(reload_task, timeout=2.0)
    await asyncio.wait_for(connect_task, timeout=2.0)

    assert registry.has("mcp_linkedin_navigate")
    assert provider.connected_server_names == {"browserbase", "linkedin"}
    assert len(connections.attempts) == 2


@pytest.mark.asyncio
async def test_unreadable_config_logs_warning_without_reloading(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    provider, registry, source, connections = await _live_runtime(
        monkeypatch, {"browserbase": _stdio_server()}
    )

    async def _reload() -> dict[str, Any]:
        raise AssertionError("reload() must not run when config cannot be read")

    monkeypatch.setattr(provider, "reload", _reload)
    agent = _FakeAgent()
    on_change = _handler(agent, provider, tmp_path / "config.json")
    sink, records = _capture_logs()
    try:
        source.error = ValueError("config.json: expecting value")
        await on_change()
    finally:
        gateway_runtime.logger.remove(sink)

    assert agent.invalidations == 1
    assert registry.has("mcp_browserbase_navigate")
    assert connections.closed == []
    warnings = [r for r in records if r["level"].name == "WARNING"]
    assert len(warnings) == 1
    assert "could not be read" in warnings[0]["message"]
    assert "expecting value" in warnings[0]["message"]


@pytest.mark.asyncio
async def test_partial_reload_logs_warning_and_returns_result(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    provider, registry, source, connections = await _live_runtime(
        monkeypatch, {"browserbase": _stdio_server()}
    )
    connections.refuse.add("linkedin")
    sink, records = _capture_logs()
    try:
        source.servers["linkedin"] = MCPServerConfig(type="stdio", command="linkedin-mcp")
        result = await _hot_reload_mcp_servers(provider)
    finally:
        gateway_runtime.logger.remove(sink)

    assert result is not None
    assert result["ok"] is False
    assert result["added"] == ["linkedin"]
    assert result["failed"] == ["linkedin"]
    assert registry.has("mcp_browserbase_navigate")
    assert provider.runtime_status() == {"browserbase": "connected", "linkedin": "failed"}
    warnings = [r for r in records if r["level"].name == "WARNING"]
    assert any("did not fully apply" in r["message"] for r in warnings)


@pytest.mark.asyncio
async def test_hot_reload_returns_none_when_nothing_changed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    provider, _registry, _source, connections = await _live_runtime(
        monkeypatch, {"browserbase": _stdio_server()}
    )

    assert await _hot_reload_mcp_servers(provider) is None
    assert len(connections.attempts) == 1
