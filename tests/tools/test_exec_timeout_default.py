"""Tests for MIT-203 ExecTool timeout default bump (60s → 180s).

We can't afford to actually sleep-wait 180s in CI, so these tests assert
the *structural* properties: default constant, schema default, and cap.
The behavioural timeout path is exercised elsewhere with short overrides.
"""

from __future__ import annotations

import inspect

from nanobot.agent.tools.shell import ExecTool
from nanobot.config.schema import ExecToolConfig


def test_exec_tool_default_timeout_is_180s():
    """The ExecTool constructor's ``timeout`` default is 180 seconds."""
    sig = inspect.signature(ExecTool.__init__)
    assert sig.parameters["timeout"].default == 180


def test_exec_tool_instance_timeout_applied():
    tool = ExecTool()
    assert tool.timeout == 180


def test_exec_tool_max_cap_unchanged():
    """Hard cap stays at 600s — a constructor override higher than that
    must still be clamped by _MAX_TIMEOUT in the runtime path."""
    assert ExecTool._MAX_TIMEOUT == 600


def test_exec_tool_schema_description_advertises_180():
    """LLMs reading the timeout parameter's description must see the
    real default (180) and cap (600) — the string is load-bearing
    for the model's planning of ``timeout=`` arguments."""
    tool = ExecTool()
    schema = tool.parameters["properties"]["timeout"]
    assert "180" in schema["description"]
    assert schema["maximum"] == 600


def test_exec_config_default_timeout_is_180s():
    """ExecToolConfig is where loader.py pulls the wired-in default;
    keep it aligned with the ExecTool constructor default."""
    assert ExecToolConfig().timeout == 180


def test_explicit_override_still_honoured():
    tool = ExecTool(timeout=30)
    assert tool.timeout == 30
