"""Guard: the system message plus tools array render identical bytes across builds.

vLLM's prefix cache covers the request body in template order, and in Qwen's
chat template the tools block follows the system text, so any byte change in
either one re-prefills the whole history. ``test_context_prompt_cache.py``
covers the system prompt alone; this covers what vLLM actually caches: the
system message plus the tools array, exactly as ``_build_kwargs`` sends them.

Design doc: docs/design/tool-selection-and-prompt-cache.md (ziggy repo), step 8a.
"""

from __future__ import annotations

import datetime as datetime_module
import json
from datetime import datetime as real_datetime
from pathlib import Path
from typing import Any

import pytest

from nanobot.agent.context import ContextBuilder
from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.providers.openai_compat_provider import OpenAICompatProvider


class _FakeDatetime(real_datetime):
    current = real_datetime(2026, 2, 24, 13, 59)

    @classmethod
    def now(cls, tz=None):  # type: ignore[override]
        return cls.current


BUILTIN_NAMES = ["edit_file", "read_file", "search_files", "write_file"]
MCP_NAMES = [
    "mcp_ziggy_calendar",
    "mcp_ziggy_contacts",
    "mcp_ziggy_gmail",
    "mcp_ziggy_maps",
]
ALL_NAMES = BUILTIN_NAMES + MCP_NAMES

_PROPERTY_QUERY = {"type": "string", "description": "text to search for"}
_PROPERTY_FILTER: dict[str, Any] = {
    "type": "object",
    "properties": {
        "domain": {"type": "string", "enum": ["web", "mail"]},
        "limit": {"type": "integer", "minimum": 1},
    },
    "required": ["domain"],
}
PROPERTIES_ORDER = ("query", "filter")
REVERSED_PROPERTIES_ORDER = ("filter", "query")


class _FakeTool(Tool):
    def __init__(
        self,
        name: str,
        properties_order: tuple[str, ...] = PROPERTIES_ORDER,
    ) -> None:
        self._name = name
        self._properties_order = properties_order

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f"{self._name} fake tool for cache-byte stability"

    @property
    def parameters(self) -> dict[str, Any]:
        fragments: dict[str, dict[str, Any]] = {
            "query": _PROPERTY_QUERY,
            "filter": _PROPERTY_FILTER,
        }
        properties = {key: fragments[key] for key in self._properties_order}
        return {"type": "object", "properties": properties, "required": ["query"]}

    async def execute(self, **kwargs: Any) -> Any:
        return kwargs


def _make_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    return workspace


def render(
    workspace: Path,
    registration_order: list[str],
    clock: real_datetime,
    monkeypatch: pytest.MonkeyPatch,
    *,
    properties_order: tuple[str, ...] = PROPERTIES_ORDER,
) -> bytes:
    """Build the cached prefix (system message + tools array) exactly as sent."""
    monkeypatch.setattr(datetime_module, "datetime", _FakeDatetime)
    _FakeDatetime.current = clock

    messages = ContextBuilder(workspace).build_messages(
        history=[],
        current_message="hi",
        channel="cli",
    )

    registry = ToolRegistry()
    for name in registration_order:
        registry.register(_FakeTool(name, properties_order=properties_order))

    provider = OpenAICompatProvider(
        api_key="x",
        api_base="http://127.0.0.1:1/v1",
        default_model="qwen-test",
    )
    kwargs = provider._build_kwargs(
        messages,
        registry.get_definitions(),
        None,
        512,
        0.7,
        "none",
        None,
    )
    return json.dumps(
        [kwargs["messages"][0], kwargs["tools"]],
        ensure_ascii=False,
    ).encode()


def test_prompt_and_tools_bytes_identical_across_builds(tmp_path, monkeypatch) -> None:
    """Same tools registered in reverse order, one clock-minute apart: same bytes."""
    workspace = _make_workspace(tmp_path)

    first = render(workspace, list(ALL_NAMES), real_datetime(2026, 2, 24, 13, 59), monkeypatch)
    second = render(
        workspace,
        list(reversed(ALL_NAMES)),
        real_datetime(2026, 2, 24, 14, 0),
        monkeypatch,
    )

    assert first == second


def test_mcp_tools_after_builtins_and_sorted(tmp_path, monkeypatch) -> None:
    """In the sent tools array, builtins come first, then mcp tools, each sorted."""
    workspace = _make_workspace(tmp_path)

    raw = render(workspace, list(reversed(ALL_NAMES)), real_datetime(2026, 2, 24, 13, 59), monkeypatch)
    _, tools = json.loads(raw)
    names = [tool["function"]["name"] for tool in tools]

    builtin_positions = [i for i, name in enumerate(names) if not name.startswith("mcp_")]
    mcp_positions = [i for i, name in enumerate(names) if name.startswith("mcp_")]

    assert set(names) == set(ALL_NAMES)
    assert builtin_positions and mcp_positions
    assert max(builtin_positions) < min(mcp_positions), "mcp tools must follow builtins"
    builtins = [name for name in names if not name.startswith("mcp_")]
    mcp = [name for name in names if name.startswith("mcp_")]
    assert builtins == sorted(builtins)
    assert mcp == sorted(mcp)


@pytest.mark.xfail(
    strict=True,
    reason=(
        "to_schema()/get_definitions pass the parameters dict through unchanged "
        "and json.dumps preserves dict insertion order, so the insertion order of "
        "a tool's 'properties' keys is part of the cached prefix bytes. Follow-up "
        "(filed in the MIT-1810 PR description): canonicalize parameter schemas "
        "(ordered/sorted normalization) in the registry before they reach the "
        "request body."
    ),
)
def test_parameter_dict_order_does_not_leak(tmp_path, monkeypatch) -> None:
    """Property insertion order must not be part of the cached bytes."""
    workspace = _make_workspace(tmp_path)

    ordered = render(
        workspace,
        ALL_NAMES,
        real_datetime(2026, 2, 24, 13, 59),
        monkeypatch,
        properties_order=PROPERTIES_ORDER,
    )
    reordered = render(
        workspace,
        ALL_NAMES,
        real_datetime(2026, 2, 24, 13, 59),
        monkeypatch,
        properties_order=REVERSED_PROPERTIES_ORDER,
    )

    assert ordered == reordered
