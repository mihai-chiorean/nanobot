"""Tests for the private-data rule in the tool contract."""

from __future__ import annotations

from pathlib import Path

from nanobot.agent.context import ContextBuilder

PRIVATE_DATA_RULE_PREFIX = "Tools that read the user's private accounts"


def _make_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    return workspace


def test_system_prompt_contains_private_data_rule(tmp_path) -> None:
    workspace = _make_workspace(tmp_path)
    builder = ContextBuilder(workspace)

    prompt = builder.build_system_prompt()

    assert "Tools that read the user's private accounts" in prompt
    assert "Never search the user's mail" in prompt


def test_rule_is_in_web_section(tmp_path) -> None:
    workspace = _make_workspace(tmp_path)
    builder = ContextBuilder(workspace)

    prompt = builder.build_system_prompt()

    web_section = prompt.index("## Web and External Information")
    rule_index = prompt.index(PRIVATE_DATA_RULE_PREFIX)
    messaging_section = prompt.index("## Messaging and Media")

    assert web_section < rule_index < messaging_section
