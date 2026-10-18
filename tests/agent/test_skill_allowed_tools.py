"""SR-17 mechanism (prereq of SR-18): skill-scoped tool list for a turn.

A turn whose metadata carries an ``allowed_tools`` set (what a scheduled
run's skill stamps there) is offered only those tools, and a call to
anything else is refused at the registry funnel before dispatch -- the same
shape as the MIT-1449 read-only gate, and the two compose as an intersection.
``SkillsLoader.skill_allowed_tools`` parses the Agent Skills ``allowed-tools``
frontmatter (space/comma-separated string or list; missing means no filter).
"""

from __future__ import annotations

from typing import Any

import pytest

from nanobot.agent.skills import SkillsLoader, parse_skill_allowed_tools
from nanobot.agent.tools.allowed_tools import (
    ALLOWED_TOOLS_META_KEY,
    allowed_tools_denial_message,
    allowed_tools_for_turn,
)
from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.context import RequestContext, request_context
from nanobot.agent.tools.registry import ToolRegistry

ALL_SPIES = ("read_file", "message", "search_web", "exec", "mcp_gmail_send_draft")


class _SpyTool(Tool):
    def __init__(self, name: str, *, read_only: bool = False) -> None:
        self._name = name
        self._read_only = read_only
        self.calls: list[dict[str, Any]] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f"spy {self._name}"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "required": []}

    @property
    def read_only(self) -> bool:
        return self._read_only

    async def execute(self, **kwargs: Any) -> str:
        self.calls.append(dict(kwargs))
        return f"{self._name} ran"


def _registry() -> tuple[ToolRegistry, dict[str, _SpyTool]]:
    registry = ToolRegistry()
    spies: dict[str, _SpyTool] = {}
    for name in ALL_SPIES:
        spy = _SpyTool(name, read_only=name in ("read_file", "search_web"))
        registry.register(spy)
        spies[name] = spy
    return registry, spies


def _ctx(metadata: dict[str, Any]) -> RequestContext:
    return RequestContext(
        channel="websocket",
        chat_id="chat-1",
        session_key="cron:job-1",
        metadata=dict(metadata),
    )


def _names(registry: ToolRegistry) -> set[str]:
    return {
        schema["function"]["name"] if "function" in schema else schema["name"]
        for schema in registry.get_definitions()
    }


# ---------------------------------------------------------------------------
# Registry enforcement
# ---------------------------------------------------------------------------


def test_skill_allowlist_offers_only_the_named_tools() -> None:
    registry, _spies = _registry()
    with request_context(_ctx({ALLOWED_TOOLS_META_KEY: frozenset({"read_file", "message"})})):
        assert _names(registry) == {"read_file", "message"}


def test_mcp_tools_are_nameable_in_the_allowlist() -> None:
    registry, _spies = _registry()
    with request_context(_ctx({ALLOWED_TOOLS_META_KEY: ["mcp_gmail_send_draft"]})):
        assert _names(registry) == {"mcp_gmail_send_draft"}


def test_turn_without_an_allowlist_keeps_the_full_tool_list() -> None:
    registry, _spies = _registry()
    with request_context(_ctx({})):
        assert _names(registry) == set(ALL_SPIES)
    # An empty/None allowlist means "no filter", never "no tools".
    with request_context(_ctx({ALLOWED_TOOLS_META_KEY: []})):
        assert _names(registry) == set(ALL_SPIES)
    with request_context(_ctx({ALLOWED_TOOLS_META_KEY: None})):
        assert _names(registry) == set(ALL_SPIES)


@pytest.mark.asyncio
async def test_denied_tool_is_refused_and_never_executes() -> None:
    registry, spies = _registry()
    with request_context(_ctx({ALLOWED_TOOLS_META_KEY: frozenset({"read_file"})})):
        result = await registry.execute("exec", {"command": "rm -rf /"})
    assert result.is_error
    assert "exec" in str(result) and "allowed" in str(result).lower()
    assert spies["exec"].calls == []


@pytest.mark.asyncio
async def test_allowed_tool_runs_normally_under_the_allowlist() -> None:
    # Negative control: the filter must not break the calls it permits.
    registry, spies = _registry()
    with request_context(_ctx({ALLOWED_TOOLS_META_KEY: ["read_file", "message"]})):
        result = await registry.execute("read_file", {"path": "x"})
    assert not result.is_error
    assert spies["read_file"].calls == [{"path": "x"}]


def test_allowlist_composes_with_read_only_as_intersection() -> None:
    registry, _spies = _registry()
    with request_context(
        _ctx(
            {
                ALLOWED_TOOLS_META_KEY: frozenset({"read_file", "message"}),
                "read_only": True,
            }
        )
    ):
        # read_file is read-only and allowed; message is allowed but mutable.
        assert _names(registry) == {"read_file"}


def test_prepare_call_denies_before_parameter_coercion() -> None:
    registry, _spies = _registry()
    with request_context(_ctx({ALLOWED_TOOLS_META_KEY: frozenset({"read_file"})})):
        tool, _params, error = registry.prepare_call("exec", "not-a-dict")
    assert tool is not None
    assert error is not None and error.is_error
    assert allowed_tools_denial_message("exec").split(".")[0] in str(error)


# ---------------------------------------------------------------------------
# Skill metadata parsing
# ---------------------------------------------------------------------------


def _write_skill(root, name: str, frontmatter: str) -> None:
    skill_dir = root / "skills" / name
    skill_dir.mkdir(parents=True)
    body = f"---\nname: {name}\ndescription: a skill\n"
    if frontmatter:
        body += f"{frontmatter}\n"
    body += "---\nDo the thing.\n"
    (skill_dir / "SKILL.md").write_text(body, encoding="utf-8")


@pytest.mark.parametrize(
    ("frontmatter", "expected"),
    [
        ("allowed-tools: read_file message", {"read_file", "message"}),
        ("allowed-tools: read_file, message", {"read_file", "message"}),
        ("allowed-tools: [read_file, message]", {"read_file", "message"}),
        ("", None),
        ('allowed-tools: ""', None),
    ],
)
def test_skill_allowed_tools_parsed_from_frontmatter(
    tmp_path, frontmatter: str, expected: set[str] | None
) -> None:
    _write_skill(tmp_path, "fare-watch", frontmatter)
    loader = SkillsLoader(tmp_path)
    got = loader.skill_allowed_tools("fare-watch")
    assert (set(got) if got is not None else None) == expected


def test_skill_allowed_tools_unknown_skill_is_none(tmp_path) -> None:
    assert SkillsLoader(tmp_path).skill_allowed_tools("ghost") is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("a b", {"a", "b"}),
        ("a, b", {"a", "b"}),
        (["a", "b"], {"a", "b"}),
        (("a",), {"a"}),
        (None, None),
        ("", None),
        ([], None),
        (123, None),
        ({"a": 1}, None),
        ([1, 2], None),
    ],
)
def test_parse_skill_allowed_tools_shapes(raw: Any, expected: set[str] | None) -> None:
    got = parse_skill_allowed_tools(raw)
    assert (set(got) if got is not None else None) == expected


def test_allowed_tools_for_turn_accepts_runner_and_frontmatter_shapes() -> None:
    assert allowed_tools_for_turn({"allowed_tools": frozenset({"exec"})}) == frozenset({"exec"})
    assert allowed_tools_for_turn({"allowed_tools": "exec, read_file"}) == frozenset(
        {"exec", "read_file"}
    )
    assert allowed_tools_for_turn({}) is None
    assert allowed_tools_for_turn(None) is None
    assert allowed_tools_for_turn({"allowed_tools": {"exec": True}}) is None
