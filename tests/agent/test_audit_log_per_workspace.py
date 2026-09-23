"""Each tenant's tool audit log lives in its own workspace (MIT-1401).

Production (``feat/shared-rooms``, ``nanobot/agent/loop.py:444``) builds
``AuditLogger(self.workspace / "audit.jsonl")``. On 0.3.0 the loop called
``AuditLogger()`` with no path, which defaults to
``$HOME/.nanobot/workspace/audit.jsonl``. On the Spark every tenant runs as the
same user with the same ``HOME``, so every tenant's tool-call rows landed in the
owner's workspace, readable by the owner agent's file tools.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.bus.queue import MessageBus


def _loop(workspace: Path) -> AgentLoop:
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = SimpleNamespace(
        max_tokens=1024, temperature=0.1, reasoning_effort=None,
    )
    return AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=workspace,
        model="test-model",
    )


def _rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


@pytest.mark.asyncio
async def test_each_workspace_gets_its_own_audit_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    (home / ".nanobot" / "workspace").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    tenant_a = tmp_path / "tenant-a"
    tenant_b = tmp_path / "tenant-b"
    tenant_a.mkdir()
    tenant_b.mkdir()

    loop_a = _loop(tenant_a)
    loop_b = _loop(tenant_b)
    await loop_a.tools.execute(
        "list_dir", {"path": str(tenant_a)}, session_id="websocket:a", channel="websocket",
    )
    await loop_b.tools.execute(
        "list_dir", {"path": str(tenant_b)}, session_id="websocket:b", channel="websocket",
    )

    owner_log = home / ".nanobot" / "workspace" / "audit.jsonl"
    assert not owner_log.exists(), (
        "tenant tool calls landed in the owner's workspace: "
        + owner_log.read_text()
    )
    rows_a = _rows(tenant_a / "audit.jsonl")
    rows_b = _rows(tenant_b / "audit.jsonl")
    assert [(r["tool_name"], r["session_id"]) for r in rows_a] == [("list_dir", "websocket:a")]
    assert [(r["tool_name"], r["session_id"]) for r in rows_b] == [("list_dir", "websocket:b")]


@pytest.mark.asyncio
async def test_an_injected_registry_is_audited_into_the_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gateway passes its own ``tool_registry``; it must be audited too."""
    from nanobot.agent.tools.registry import ToolRegistry

    home = tmp_path / "home"
    (home / ".nanobot" / "workspace").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    workspace = tmp_path / "tenant"
    workspace.mkdir()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = SimpleNamespace(
        max_tokens=1024, temperature=0.1, reasoning_effort=None,
    )
    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=workspace,
        model="test-model",
        tool_registry=ToolRegistry(),
    )

    await loop.tools.execute(
        "list_dir", {"path": str(workspace)}, session_id="websocket:t", channel="websocket",
    )

    assert not (home / ".nanobot" / "workspace" / "audit.jsonl").exists()
    rows = _rows(workspace / "audit.jsonl")
    assert [(r["tool_name"], r["session_id"]) for r in rows] == [("list_dir", "websocket:t")]


def test_deleting_a_tenant_session_never_touches_the_legacy_global_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Optional residue fix: ``~/.nanobot/sessions`` is the default workspace's."""
    from nanobot.session.manager import SessionManager

    home = tmp_path / "home"
    (home / ".nanobot" / "sessions").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    tenant = SessionManager(tmp_path / "tenant", sessions_root=tmp_path / "sessions")
    key = "websocket:shared-name"
    session = tenant.get_or_create(key)
    session.add_message("user", "tenant text")
    tenant.save(session)
    owner_file = tenant._get_legacy_session_path(key)
    owner_file.parent.mkdir(parents=True, exist_ok=True)
    owner_file.write_text('{"owner": true}\n')

    assert tenant.delete_session(key)

    assert owner_file.exists()


def test_the_default_workspace_still_cleans_its_legacy_session_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nanobot.session.manager import SessionManager

    home = tmp_path / "home"
    (home / ".nanobot" / "sessions").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    owner = SessionManager(
        home / ".nanobot" / "workspace", sessions_root=tmp_path / "sessions",
    )
    key = "websocket:owner-chat"
    session = owner.get_or_create(key)
    session.add_message("user", "owner text")
    owner.save(session)
    legacy_file = owner._get_legacy_session_path(key)
    legacy_file.parent.mkdir(parents=True, exist_ok=True)
    legacy_file.write_text('{"owner": true}\n')

    assert owner.delete_session(key)

    assert not legacy_file.exists()
