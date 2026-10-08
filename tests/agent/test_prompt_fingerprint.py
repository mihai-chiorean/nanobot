"""TP-06: per-turn system-prompt section fingerprints and skill hashes.

Design: docs/design/turn-provenance.md §4. The fingerprint is returned,
never stored on the builder (one ContextBuilder serves concurrent turns),
and the prompt text stays byte-identical to ``build_system_prompt``.
"""

import asyncio
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.agent.context import ContextBuilder
from nanobot.agent.loop import AgentLoop
from nanobot.agent.skills import SkillsLoader, skill_file_sha
from nanobot.agent.turn_provenance import (
    CURRENT_TURN_PROVENANCE,
    TurnProvenance,
    note_tool_call,
)
from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import LLMResponse, ToolCallRequest

# ---------------------------------------------------------------------------
# Fixtures: a workspace with AGENTS.md, SOUL.md, USER.md, MEMORY.md and two
# workspace skills (foo always-on, bar on-demand).
# ---------------------------------------------------------------------------

MEMORY_TEXT = "Fact one: the deploy key lives in vault."


def _write_skill(base: Path, name: str, *, always: bool) -> Path:
    skill_dir = base / name
    skill_dir.mkdir(parents=True)
    front = f"name: {name}\ndescription: The {name} skill for tests.\n"
    if always:
        front += "metadata:\n  nanobot:\n    always: true\n"
    path = skill_dir / "SKILL.md"
    path.write_text(f"---\n{front}---\n# {name.capitalize()}\nBody of {name}.\n", encoding="utf-8")
    return path


def _make_workspace(tmp_path: Path, *, with_user: bool = True) -> Path:
    (tmp_path / "AGENTS.md").write_text(
        "# Project instructions\nDo the thing.\n", encoding="utf-8"
    )
    (tmp_path / "SOUL.md").write_text(
        "I am a custom soul, not the bundled template.\n", encoding="utf-8"
    )
    if with_user:
        (tmp_path / "USER.md").write_text(
            "User prefers terse answers.\n", encoding="utf-8"
        )
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    (memory_dir / "MEMORY.md").write_text(MEMORY_TEXT, encoding="utf-8")
    skills = tmp_path / "skills"
    _write_skill(skills, "foo", always=True)
    _write_skill(skills, "bar", always=False)
    return tmp_path


def _sections(builder: ContextBuilder, **kw) -> dict[str, dict]:
    _, sections = builder.build_system_prompt_with_fingerprint(**kw)
    return {entry["section"]: entry for entry in sections}


def _sha16(text: str) -> str:
    raw = text.encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Byte identity and entry shape
# ---------------------------------------------------------------------------


class TestByteIdentity:
    def test_prompt_text_identical_to_build_system_prompt(self, tmp_path):
        builder = ContextBuilder(workspace=_make_workspace(tmp_path))
        for kw in (
            {},
            {"channel": "websocket"},
            {"include_memory": False},
            {"shared_room": True},
        ):
            prompt, _sections = builder.build_system_prompt_with_fingerprint(**kw)
            assert prompt == builder.build_system_prompt(**kw)

    def test_sha_and_bytes_over_the_section_utf8(self, tmp_path):
        builder = ContextBuilder(workspace=_make_workspace(tmp_path))
        entry = _sections(builder)["memory"]
        expected_text = f"# Memory\n\n## Long-term Memory\n{MEMORY_TEXT}"
        assert entry["sha"] == _sha16(expected_text)
        assert entry["bytes"] == len(expected_text.encode("utf-8"))

    def test_section_keys_for_the_full_workspace(self, tmp_path):
        builder = ContextBuilder(workspace=_make_workspace(tmp_path), workflow_scheduling=True)
        keys = list(_sections(builder, channel="websocket"))
        assert keys == [
            "identity",
            "bootstrap.AGENTS.md",
            "bootstrap.SOUL.md",
            "bootstrap.USER.md",
            "workflow_intake",
            "tool_contract",
            "memory",
            "active_skills",
            "skills_summary",
        ]

    def test_project_section_only_for_a_foreign_workspace(self, tmp_path):
        builder = ContextBuilder(workspace=_make_workspace(tmp_path))
        assert "project" not in _sections(builder)
        other = tmp_path / "proj"
        other.mkdir()
        sections = _sections(builder, workspace=other)
        expected = (
            "# Current Project\n\n"
            f"Working directory: {other.expanduser().resolve()}\n"
            "Use it as the default root for project files and relative tool paths."
        )
        assert sections["project"]["sha"] == _sha16(expected)

    def test_bootstrap_parts_are_the_joined_string_pieces(self, tmp_path):
        builder = ContextBuilder(workspace=_make_workspace(tmp_path))
        parts = builder._load_bootstrap_parts()
        assert [name for name, _chunk in parts] == ["AGENTS.md", "SOUL.md", "USER.md"]
        assert builder._load_bootstrap_files() == "\n\n".join(
            chunk for _name, chunk in parts
        )
        sections = _sections(builder)
        for name, chunk in parts:
            assert sections[f"bootstrap.{name}"]["sha"] == _sha16(chunk)


# ---------------------------------------------------------------------------
# Section sensitivity
# ---------------------------------------------------------------------------


class TestSectionSensitivity:
    def test_changing_only_memory_changes_only_the_memory_sha(self, tmp_path):
        builder = ContextBuilder(workspace=_make_workspace(tmp_path))
        before = _sections(builder)
        (tmp_path / "memory" / "MEMORY.md").write_text(
            MEMORY_TEXT + "\nFact two: the owner likes tests.", encoding="utf-8"
        )
        after = _sections(builder)
        assert set(after) == set(before)
        changed = {key for key in before if before[key]["sha"] != after[key]["sha"]}
        assert changed == {"memory"}

    def test_missing_user_md_leaves_out_the_bootstrap_user_section(self, tmp_path):
        builder = ContextBuilder(workspace=_make_workspace(tmp_path, with_user=False))
        keys = list(_sections(builder))
        assert "bootstrap.USER.md" not in keys
        assert "bootstrap.AGENTS.md" in keys and "bootstrap.SOUL.md" in keys

    def test_shared_room_gives_exactly_the_shared_room_entry(self, tmp_path):
        builder = ContextBuilder(workspace=_make_workspace(tmp_path))
        prompt, sections = builder.build_system_prompt_with_fingerprint(shared_room=True)
        assert len(sections) == 1
        entry = sections[0]
        assert entry["section"] == "shared_room"
        assert prompt == ContextBuilder.SHARED_ROOM_SYSTEM_PROMPT
        assert entry["sha"] == _sha16(prompt)
        assert entry["bytes"] == len(prompt.encode("utf-8"))


# ---------------------------------------------------------------------------
# Skills: listed sha and loaded entries
# ---------------------------------------------------------------------------


class TestSkillHashes:
    def test_editing_one_skill_md_changes_skills_listed_sha(self, tmp_path):
        loader = SkillsLoader(_make_workspace(tmp_path), builtin_skills_dir=tmp_path / "none")
        summary_before, sha_before = loader.build_skills_summary_with_sha(exclude={"foo"})
        assert summary_before and isinstance(sha_before, str) and len(sha_before) == 16
        bar = tmp_path / "skills" / "bar" / "SKILL.md"
        bar.write_text(
            bar.read_text(encoding="utf-8") + "\nAn extra line changes the bytes.\n",
            encoding="utf-8",
        )
        summary_after, sha_after = loader.build_skills_summary_with_sha(exclude={"foo"})
        assert summary_after == summary_before  # description-based text unchanged
        assert sha_after != sha_before

    def test_build_skills_summary_unchanged_and_equal_to_pair_first(self, tmp_path):
        loader = SkillsLoader(_make_workspace(tmp_path), builtin_skills_dir=tmp_path / "none")
        summary = loader.build_skills_summary(exclude={"foo"})
        assert "bar" in summary  # sanity: the fixture really listed a skill
        assert summary == loader.build_skills_summary_with_sha(exclude={"foo"})[0]

    def test_empty_listing_has_no_listed_sha(self, tmp_path):
        loader = SkillsLoader(_make_workspace(tmp_path), builtin_skills_dir=tmp_path / "none")
        summary, sha = loader.build_skills_summary_with_sha(exclude={"foo", "bar"})
        assert summary == ""
        assert sha is None

    def test_listed_sha_follows_sorted_name_line_pairs(self, tmp_path):
        loader = SkillsLoader(_make_workspace(tmp_path), builtin_skills_dir=tmp_path / "none")
        _summary, sha = loader.build_skills_summary_with_sha(exclude={"foo"})
        expected = _sha16(
            f"bar:{skill_file_sha(tmp_path / 'skills' / 'bar' / 'SKILL.md')}"
        )
        assert sha == expected

    def test_note_tool_call_records_skill_read_by_the_model(self, tmp_path):
        _make_workspace(tmp_path)
        record = TurnProvenance(turn_id="t", started_at="s", source="user")
        token = CURRENT_TURN_PROVENANCE.set(record)
        try:
            note_tool_call(
                "read_file", {"path": str(tmp_path / "skills" / "foo" / "SKILL.md")}
            )
            note_tool_call(
                "read_file", {"path": str(tmp_path / "skills" / "foo" / "SKILL.md")}
            )
        finally:
            CURRENT_TURN_PROVENANCE.reset(token)
        assert record.skills_loaded == [
            {
                "name": "foo",
                "sha": skill_file_sha(tmp_path / "skills" / "foo" / "SKILL.md"),
            }
        ]

    def test_note_tool_call_negative_controls(self, tmp_path):
        _make_workspace(tmp_path)
        record = TurnProvenance(turn_id="t", started_at="s", source="user")
        token = CURRENT_TURN_PROVENANCE.set(record)
        try:
            # exec cat of a real SKILL.md is not the model opening the file.
            note_tool_call("exec", {"command": f"cat {tmp_path}/skills/foo/SKILL.md"})
            note_tool_call("read_file", {"path": str(tmp_path / "memory" / "MEMORY.md")})
            note_tool_call("read_file", {"path": str(tmp_path / "skills" / "ghost" / "SKILL.md")})
            note_tool_call("read_file", {"path": 42})
            note_tool_call("read_file", "not-a-dict")
            note_tool_call("read_file", {"path": str(tmp_path / "notes" / "SKILL.md.bak")})
            # A SKILL.md whose parent dir sits on a non-existent path.
            note_tool_call("read_file", {"path": f"{tmp_path}/nope/deep/SKILL.md"})
        finally:
            CURRENT_TURN_PROVENANCE.reset(token)
        assert record.skills_loaded == []

    def test_note_tool_call_outside_a_turn_records_nothing(self, tmp_path):
        _make_workspace(tmp_path)
        # No bound record: must not raise and must not crash.
        note_tool_call("read_file", {"path": str(tmp_path / "skills" / "foo" / "SKILL.md")})

    def test_note_prompt_flags_only_a_differing_rebuild(self):
        record = TurnProvenance(turn_id="t", started_at="s", source="user")
        a = [{"section": "identity", "sha": "a" * 16, "bytes": 1}]
        record.note_prompt(a)
        record.note_prompt(list(a))
        assert record.prompt == a and record.prompt_rebuilt is False
        b = [{"section": "identity", "sha": "b" * 16, "bytes": 2}]
        record.note_prompt(b)
        assert record.prompt_rebuilt is True
        assert record.prompt == b  # the last one is kept

    def test_to_dict_shape(self):
        record = TurnProvenance(turn_id="t", started_at="s", source="user")
        payload = record.to_dict()
        assert payload["v"] == 1
        assert payload["turn_id"] == "t"
        assert "model" not in payload and "model_preset" not in payload  # None omitted
        assert payload["prompt"] == [] and payload["other_steps"] == 0


# ---------------------------------------------------------------------------
# Concurrency: one builder, many workspaces, no shared fingerprint state
# ---------------------------------------------------------------------------


class TestConcurrentFingerprints:
    def test_two_threads_get_their_own_fingerprint(self, tmp_path):
        workspace = _make_workspace(tmp_path)
        builder = ContextBuilder(workspace=workspace)
        ws_a = tmp_path / "a"
        ws_b = tmp_path / "b"
        ws_a.mkdir()
        ws_b.mkdir()

        def build(root: Path):
            return builder.build_system_prompt_with_fingerprint(
                channel="websocket", workspace=root
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(build, ws_a)
            second = pool.submit(build, ws_b)
            prompt_a, sections_a = first.result()
            prompt_b, sections_b = second.result()

        solo_a = builder.build_system_prompt_with_fingerprint(
            channel="websocket", workspace=ws_a
        )
        solo_b = builder.build_system_prompt_with_fingerprint(
            channel="websocket", workspace=ws_b
        )
        assert (prompt_a, sections_a) == solo_a
        assert (prompt_b, sections_b) == solo_b
        assert prompt_a != prompt_b  # sanity: they really are different prompts
        project_a = next(e for e in sections_a if e["section"] == "project")
        project_b = next(e for e in sections_b if e["section"] == "project")
        assert project_a["sha"] != project_b["sha"]


# ---------------------------------------------------------------------------
# End-to-end: a real turn persists the fingerprint into provenance_v1
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_turn_records_prompt_fingerprint_and_skills(tmp_path):
    workspace = _make_workspace(tmp_path)
    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    skill_path = str(workspace / "skills" / "bar" / "SKILL.md")
    calls = iter(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="tc1", name="read_file", arguments={"path": skill_path}
                    )
                ],
            ),
            LLMResponse(
                content="Bar does the needful. SENTINEL-ANSWER-9182",
                tool_calls=[],
            ),
        ]
    )
    provider.chat_stream_with_retry = AsyncMock(side_effect=lambda *a, **kw: next(calls))
    loop = AgentLoop(bus=bus, provider=provider, workspace=workspace, model="test-model")

    await loop._dispatch_one(
        InboundMessage(
            channel="websocket",
            sender_id="u1",
            chat_id="chat1",
            content="load $bar and tell me about it",
            metadata={"webui_turn_id": "wire-turn-7"},
        ),
        asyncio.Queue(),
    )
    while bus.outbound_size > 0:
        await bus.consume_outbound()

    persisted = loop.sessions.read_session_file("websocket:chat1")
    assert persisted is not None
    entries = (persisted.get("metadata") or {}).get("provenance_v1")
    assert isinstance(entries, list) and len(entries) == 1
    entry = entries[0]

    assert entry["turn_id"] == "wire-turn-7"
    assert entry["source"] == "user"
    assert entry["answered"] is True
    assert entry["v"] == 1

    keys = [section["section"] for section in entry["prompt"]]
    assert keys == [
        "identity",
        "bootstrap.AGENTS.md",
        "bootstrap.SOUL.md",
        "bootstrap.USER.md",
        "tool_contract",
        "memory",
        "active_skills",
        "skills_summary",
    ]
    assert entry["prompt_rebuilt"] is False

    # Independent recomputation (not the builder's own hash): the memory
    # section hashes the exact block the prompt embeds.
    memory_entry = next(s for s in entry["prompt"] if s["section"] == "memory")
    expected_memory = f"# Memory\n\n## Long-term Memory\n{MEMORY_TEXT}"
    assert memory_entry["sha"] == _sha16(expected_memory)
    assert memory_entry["bytes"] == len(expected_memory.encode("utf-8"))

    assert isinstance(entry["skills_listed_sha"], str)
    assert len(entry["skills_listed_sha"]) == 16

    loaded = {item["name"]: item["sha"] for item in entry["skills_loaded"]}
    # always-on skill and the $name/read_file skill, de-duplicated to two.
    assert loaded["foo"] == skill_file_sha(workspace / "skills" / "foo" / "SKILL.md")
    assert loaded["bar"] == skill_file_sha(workspace / "skills" / "bar" / "SKILL.md")
    assert len(entry["skills_loaded"]) == 2

    # Provenance never retains the user's message or the answer text.
    assert "SENTINEL-ANSWER-9182" not in json.dumps(entry)
    assert "load $bar and tell me about it" not in json.dumps(entry)
