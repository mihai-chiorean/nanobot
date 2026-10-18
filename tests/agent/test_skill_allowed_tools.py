"""MIT-1827: skill-scoped tool lists (``allowed-tools``) enforced per turn.

A turn whose metadata carries ``allowed_tools`` is offered only those tools,
and a call to anything else is refused at the single registry funnel before
dispatch. The set comes from the skill's ``allowed-tools`` frontmatter
(Agent Skills spec) for scheduled runs; the field is advisory until a caller
stamps it onto the turn. Both this filter and the MIT-1449 read-only filter
apply to a turn, so the effective tool set is their intersection.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.agent.skills import SkillsLoader
from nanobot.agent.tools.allowed_tools import (
    ALLOWED_TOOLS_META_KEY,
    allowed_tools_denial_message,
    allowed_tools_for_turn,
)
from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.context import RequestContext, request_context
from nanobot.agent.tools.read_only import READ_ONLY_META_KEY
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.websocket.work_stream import WorkStreamHub
from nanobot.runtime_context import RUNTIME_CONTEXT_INPUT_META
from nanobot.work.store import WorkStore

CHAT_ID = "99999999-8888-7777-6666-555555555555"


def _schema_names(tools: list[dict[str, Any]] | None) -> set[str]:
    names: set[str] = set()
    for schema in tools or []:
        fn = schema.get("function") if isinstance(schema, dict) else None
        name = fn.get("name") if isinstance(fn, dict) else schema.get("name")
        if isinstance(name, str):
            names.add(name)
    return names


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
        return {
            "type": "object",
            "properties": {},
            "additionalProperties": True,
            "required": [],
        }

    @property
    def read_only(self) -> bool:
        return self._read_only

    async def execute(self, **kwargs: Any) -> str:
        self.calls.append(dict(kwargs))
        return f"{self._name} ran"


def _make_registry_with_spies() -> tuple[ToolRegistry, dict[str, _SpyTool]]:
    registry = ToolRegistry()
    spies: dict[str, _SpyTool] = {}
    for name, read_only in (
        ("read_file", True),
        ("web_search", True),
        ("message", False),
        ("exec", False),
        ("mcp_github_search_issues", True),
    ):
        spy = _SpyTool(name, read_only=read_only)
        registry.register(spy)
        spies[name] = spy
    return registry, spies


def _ctx(
    *,
    chat_id: str = "owner-chat",
    session_key: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> RequestContext:
    return RequestContext(
        channel="websocket",
        chat_id=chat_id,
        session_key=session_key or f"websocket:{chat_id}",
        metadata=dict(metadata or {}),
    )


def _make_loop(tmp_path: Path) -> AgentLoop:
    from nanobot.providers.base import GenerationSettings, LLMResponse, LLMUsage

    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = GenerationSettings()

    async def chat_stream_with_retry(**_kwargs: Any) -> LLMResponse:
        return LLMResponse(
            content="ok",
            tool_calls=[],
            usage=LLMUsage.reported(input_tokens=1, output_tokens=1),
        )

    provider.chat_stream_with_retry = chat_stream_with_retry
    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        model="test-model",
    )
    loop.auto_compact.prepare_session = MagicMock(  # type: ignore[method-assign]
        side_effect=lambda session, key: (session, None)
    )
    return loop


# --------------------------------------------------------------------------
# Registry enforcement
# --------------------------------------------------------------------------


def test_allowed_tools_turn_offers_only_the_listed_tools() -> None:
    registry, _spies = _make_registry_with_spies()
    with request_context(
        _ctx(metadata={ALLOWED_TOOLS_META_KEY: ["read_file", "message"]})
    ):
        names = _schema_names(registry.get_definitions())
    assert names == {"read_file", "message"}


def test_allowed_tools_turn_denies_everything_else_without_executing_it() -> None:
    registry, spies = _make_registry_with_spies()
    with request_context(
        _ctx(metadata={ALLOWED_TOOLS_META_KEY: ["read_file", "message"]})
    ):
        _tool, _params, refused = registry.prepare_call("exec", {"command": "ls"})
        assert refused is not None
        assert "exec" in str(refused)
        assert spies["exec"].calls == []

        _tool, _params, refused = registry.prepare_call(
            "web_search", {"query": "x"}
        )
        assert refused is not None
        assert spies["web_search"].calls == []

        _tool, _params, refused = registry.prepare_call(
            "read_file", {"path": "notes.md"}
        )
        assert refused is None


@pytest.mark.asyncio
async def test_execute_denial_is_reported_and_never_dispatches() -> None:
    registry, spies = _make_registry_with_spies()
    with request_context(
        _ctx(metadata={ALLOWED_TOOLS_META_KEY: ["read_file", "message"]})
    ):
        result = await registry.execute("exec", {"command": "rm -rf /"})
    assert result.is_error is True
    assert "exec" in str(result)
    assert spies["exec"].calls == []


def test_turn_without_the_key_keeps_the_full_tool_list() -> None:
    registry, _spies = _make_registry_with_spies()
    with request_context(_ctx(metadata={"work_mode": "background"})):
        names = _schema_names(registry.get_definitions())
    assert names == {
        "read_file",
        "web_search",
        "message",
        "exec",
        "mcp_github_search_issues",
    }


def test_names_match_mcp_tool_names_too() -> None:
    registry, _spies = _make_registry_with_spies()
    with request_context(
        _ctx(metadata={ALLOWED_TOOLS_META_KEY: ["mcp_github_search_issues"]})
    ):
        names = _schema_names(registry.get_definitions())
    assert names == {"mcp_github_search_issues"}


def test_allowed_set_and_read_only_compose_as_intersection() -> None:
    registry, _spies = _make_registry_with_spies()
    with request_context(
        _ctx(
            metadata={
                READ_ONLY_META_KEY: True,
                ALLOWED_TOOLS_META_KEY: ["read_file", "message", "exec"],
            }
        )
    ):
        names = _schema_names(registry.get_definitions())
    assert names == {"read_file"}

    # Only the allowed filter: message/exec are offered.
    with request_context(
        _ctx(metadata={ALLOWED_TOOLS_META_KEY: ["read_file", "message", "exec"]})
    ):
        names = _schema_names(registry.get_definitions())
    assert names == {"read_file", "message", "exec"}

    # Only the read-only filter: message/exec drop, web_search stays.
    with request_context(_ctx(metadata={READ_ONLY_META_KEY: True})):
        names = _schema_names(registry.get_definitions())
    assert names == {"read_file", "web_search", "mcp_github_search_issues"}


def test_denied_call_is_refused_even_when_read_only_would_allow_it() -> None:
    registry, spies = _make_registry_with_spies()
    with request_context(
        _ctx(metadata={ALLOWED_TOOLS_META_KEY: ["exec"]})
    ):
        # exec is not read-only, so the read-only filter is off here; the
        # allowlist alone decides: web_search is hidden/denied, exec allowed.
        _tool, _params, refused = registry.prepare_call("web_search", {"query": "x"})
        assert refused is not None
        assert spies["web_search"].calls == []
        _tool, _params, refused = registry.prepare_call("exec", {"command": "ls"})
        assert refused is None


def test_denial_message_is_actionable() -> None:
    message = allowed_tools_denial_message("exec")
    assert "exec" in message
    assert "not available" in message
    assert "tools" in message


def test_agentloop_binds_the_real_registry_filter(tmp_path: Path) -> None:
    """The production tool registry (all built-ins) narrows exactly to two."""
    loop = _make_loop(tmp_path)
    assert {"read_file", "message", "exec"} <= set(loop.tools.tool_names)
    with request_context(
        _ctx(metadata={ALLOWED_TOOLS_META_KEY: ["read_file", "message"]})
    ):
        names = _schema_names(loop.tools.get_definitions())
    assert names == {"read_file", "message"}

    with request_context(_ctx(metadata={})):
        full = _schema_names(loop.tools.get_definitions())
    assert {"read_file", "message", "exec", "write_file"} <= full


# --------------------------------------------------------------------------
# Metadata classification (mirror of read_only_turn posture)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("metadata", "expected"),
    [
        ({ALLOWED_TOOLS_META_KEY: ["read_file", "message"]}, {"read_file", "message"}),
        ({ALLOWED_TOOLS_META_KEY: ("read_file", "message")}, {"read_file", "message"}),
        ({ALLOWED_TOOLS_META_KEY: "read_file message"}, {"read_file", "message"}),
        ({ALLOWED_TOOLS_META_KEY: "read_file,message"}, {"read_file", "message"}),
        (
            {ALLOWED_TOOLS_META_KEY: " read_file ,  message "},
            {"read_file", "message"},
        ),
        ({ALLOWED_TOOLS_META_KEY: "read_file, message exec"}, {"read_file", "message", "exec"}),
        ({ALLOWED_TOOLS_META_KEY: []}, set()),
        ({ALLOWED_TOOLS_META_KEY: ["read_file", 7]}, set()),
        ({ALLOWED_TOOLS_META_KEY: 5}, set()),
        ({}, None),
        ({ALLOWED_TOOLS_META_KEY: None}, None),
        ({ALLOWED_TOOLS_META_KEY: ""}, None),
        ({ALLOWED_TOOLS_META_KEY: "   "}, None),
        ({"work_mode": "background"}, None),
        (None, None),
    ],
)
def test_allowed_tools_for_turn_classification(
    metadata: Any, expected: set[str] | None
) -> None:
    result = allowed_tools_for_turn(metadata)
    if expected is None:
        assert result is None
    else:
        assert result == frozenset(expected)


# --------------------------------------------------------------------------
# Skill frontmatter parsing
# --------------------------------------------------------------------------


def _write_skill(
    workspace: Path,
    name: str,
    *,
    frontmatter: list[str],
    body: str = "Do the thing.",
) -> None:
    skill_dir = workspace / "skills" / name
    skill_dir.mkdir(parents=True)
    lines = ["---", f"name: {name}", "description: A test skill.", *frontmatter]
    lines.extend(["---", "", body])
    (skill_dir / "SKILL.md").write_text("\n".join(lines), encoding="utf-8")


def _loader(tmp_path: Path) -> SkillsLoader:
    builtin = tmp_path / "builtin"
    builtin.mkdir(exist_ok=True)
    return SkillsLoader(tmp_path, builtin_skills_dir=builtin)


def test_skill_allowed_tools_parses_space_separated(tmp_path: Path) -> None:
    _write_skill(
        tmp_path, "fare-watch", frontmatter=["allowed-tools: read_file message"]
    )
    assert _loader(tmp_path).skill_allowed_tools("fare-watch") == frozenset(
        {"read_file", "message"}
    )


def test_skill_allowed_tools_parses_comma_separated(tmp_path: Path) -> None:
    _write_skill(
        tmp_path,
        "fare-watch",
        frontmatter=["allowed-tools: read_file, message, mcp_github_search_issues"],
    )
    assert _loader(tmp_path).skill_allowed_tools("fare-watch") == frozenset(
        {"read_file", "message", "mcp_github_search_issues"}
    )


def test_skill_allowed_tools_parses_yaml_list(tmp_path: Path) -> None:
    _write_skill(
        tmp_path,
        "fare-watch",
        frontmatter=["allowed-tools:", "  - read_file", "  - message"],
    )
    assert _loader(tmp_path).skill_allowed_tools("fare-watch") == frozenset(
        {"read_file", "message"}
    )


def test_skill_allowed_tools_missing_field_and_unknown_skill(tmp_path: Path) -> None:
    _write_skill(tmp_path, "plain", frontmatter=[])
    loader = _loader(tmp_path)
    assert loader.skill_allowed_tools("plain") is None
    assert loader.skill_allowed_tools("does-not-exist") is None


def test_skill_exists(tmp_path: Path) -> None:
    _write_skill(tmp_path, "fare-watch", frontmatter=[])
    loader = _loader(tmp_path)
    assert loader.skill_exists("fare-watch") is True
    assert loader.skill_exists("no-such-skill-xyz") is False


def test_build_skill_turn_metadata_carries_filter_and_block(tmp_path: Path) -> None:
    _write_skill(
        tmp_path,
        "fare-watch",
        frontmatter=["allowed-tools: message read_file"],
        body="Watch the fares closely.",
    )
    metadata = _loader(tmp_path).build_skill_turn_metadata("fare-watch")
    assert metadata[ALLOWED_TOOLS_META_KEY] == ["message", "read_file"]
    [block] = metadata[RUNTIME_CONTEXT_INPUT_META]
    assert block.source == "explicit_skills"
    assert "Watch the fares closely." in block.content
    assert "fare-watch" in block.content


def test_build_skill_turn_metadata_without_field_injects_content_only(
    tmp_path: Path,
) -> None:
    _write_skill(tmp_path, "plain", frontmatter=[], body="Plain body.")
    metadata = _loader(tmp_path).build_skill_turn_metadata("plain")
    assert ALLOWED_TOOLS_META_KEY not in metadata
    [block] = metadata[RUNTIME_CONTEXT_INPUT_META]
    assert "Plain body." in block.content


# --------------------------------------------------------------------------
# work.create envelope
# --------------------------------------------------------------------------


class _Connection:
    remote_address = ("127.0.0.1", 41000)


class _Transport:
    name = "websocket"
    runtime_model_name = "test-model"

    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []
        self.attached: list[str] = []

    async def webui_send_event(self, _connection: Any, event: str, **fields: Any) -> None:
        self.frames.append({"event": event, **fields})

    async def webui_send_raw(self, _connection: Any, raw: str, *, label: str = "") -> None:
        self.frames.append(json.loads(raw))

    def webui_attach(self, _connection: Any, chat_id: str) -> None:
        self.attached.append(chat_id)

    def store_work_attachments(self, media: list[Any]) -> tuple[list[str], str | None]:
        return [], None

    def room_turn_metadata(self, _connection: Any, _chat_id: str) -> dict[str, Any]:
        return {}


class _Bus:
    def __init__(self) -> None:
        self.inbound: list[InboundMessage] = []

    async def publish_inbound(self, message: InboundMessage) -> None:
        self.inbound.append(message)


def _make_hub(store: WorkStore) -> tuple[WorkStreamHub, _Bus]:
    bus = _Bus()
    return WorkStreamHub(transport=_Transport(), store=store, bus=bus), bus


async def _create(
    hub: WorkStreamHub,
    **overrides: Any,
) -> None:
    envelope: dict[str, Any] = {
        "type": "work.create",
        "chat_id": CHAT_ID,
        "content": "watch the fares",
    }
    envelope.update(overrides)
    await hub.dispatch(_Connection(), "client-1", envelope)


@pytest.mark.asyncio
async def test_work_create_with_skill_scopes_the_run(tmp_path: Path) -> None:
    store = WorkStore(tmp_path)
    _write_skill(
        store.workspace,
        "fare-watch",
        frontmatter=["allowed-tools: read_file message"],
        body="Watch fares like this.",
    )
    hub, bus = _make_hub(store)
    await _create(hub, skill="fare-watch")

    created = [f for f in hub._transport.frames if f["event"] == "work.created"]
    assert created
    [inbound] = bus.inbound
    assert inbound.metadata[ALLOWED_TOOLS_META_KEY] == ["message", "read_file"]
    [block] = inbound.metadata[RUNTIME_CONTEXT_INPUT_META]
    assert block.source == "explicit_skills"
    assert "Watch fares like this." in block.content


@pytest.mark.asyncio
async def test_work_create_with_skill_composes_with_read_only(tmp_path: Path) -> None:
    store = WorkStore(tmp_path)
    _write_skill(
        store.workspace,
        "fare-watch",
        frontmatter=["allowed-tools: read_file message exec"],
    )
    hub, bus = _make_hub(store)
    await _create(hub, skill="fare-watch", read_only=True)
    [inbound] = bus.inbound
    assert inbound.metadata[READ_ONLY_META_KEY] is True
    assert inbound.metadata[ALLOWED_TOOLS_META_KEY] == [
        "exec",
        "message",
        "read_file",
    ]


@pytest.mark.asyncio
async def test_work_create_with_skill_without_field_is_unscoped(tmp_path: Path) -> None:
    store = WorkStore(tmp_path)
    _write_skill(store.workspace, "plain", frontmatter=[], body="Plain body.")
    hub, bus = _make_hub(store)
    await _create(hub, skill="plain")
    [inbound] = bus.inbound
    assert ALLOWED_TOOLS_META_KEY not in inbound.metadata
    [block] = inbound.metadata[RUNTIME_CONTEXT_INPUT_META]
    assert "Plain body." in block.content


@pytest.mark.asyncio
async def test_work_create_without_skill_is_unscoped(tmp_path: Path) -> None:
    hub, bus = _make_hub(WorkStore(tmp_path))
    await _create(hub)
    [inbound] = bus.inbound
    assert ALLOWED_TOOLS_META_KEY not in inbound.metadata
    assert RUNTIME_CONTEXT_INPUT_META not in inbound.metadata


@pytest.mark.asyncio
async def test_work_create_unknown_skill_is_rejected_with_no_turn(
    tmp_path: Path,
) -> None:
    hub, bus = _make_hub(WorkStore(tmp_path))
    await _create(hub, skill="no-such-skill-xyz")
    assert not bus.inbound
    assert not [f for f in hub._transport.frames if f["event"] == "work.created"]
    errors = [f for f in hub._transport.frames if f["event"] == "error"]
    assert errors
    assert errors[-1]["detail"] == "unknown skill"
    assert errors[-1]["skill"] == "no-such-skill-xyz"


@pytest.mark.parametrize("bad", [123, ["fare-watch"], "", "   "])
@pytest.mark.asyncio
async def test_work_create_invalid_skill_shape_is_rejected_with_no_turn(
    tmp_path: Path, bad: Any
) -> None:
    hub, bus = _make_hub(WorkStore(tmp_path))
    await _create(hub, skill=bad)
    assert not bus.inbound
    assert not [f for f in hub._transport.frames if f["event"] == "work.created"]
    errors = [f for f in hub._transport.frames if f["event"] == "error"]
    assert errors[-1]["detail"] == "invalid skill"
