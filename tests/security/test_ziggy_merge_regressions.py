"""Ziggy-local (fork): regressions found reviewing the 2026-09 upstream merge.

Each test here pins a control that the merge either dropped or routed around.
They are grouped in one file so the next upstream merge has a single place to
look when these conflict.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from nanobot.agent.tools.apply_patch import ApplyPatchTool
from nanobot.agent.tools.filesystem import EditFileTool, SensitivePathError, WriteFileTool


# ---------------------------------------------------------------------------
# apply_patch is upstream-new and its own schema calls it the "default tool for
# code edits". It resolved writes with no sensitive-path check, so anything
# edit_file refused could simply be routed through it. write_file never had the
# check either. The guard now lives in _FsTool._resolve_write so every current
# and future write tool inherits it.
# ---------------------------------------------------------------------------

_SENSITIVE_TARGETS = [
    "~/.ssh/authorized_keys",
    "~/.ssh/id_rsa",
    "~/.aws/credentials",
    "secrets/.env",
    "certs/server.pem",
]


@pytest.mark.parametrize("target", _SENSITIVE_TARGETS)
async def test_apply_patch_cannot_write_sensitive_paths(tmp_path, target):
    result = await ApplyPatchTool(workspace=tmp_path).execute(
        edits=[{"path": target, "action": "add", "new_text": "pwned\n"}],
    )
    assert "blocked (sensitive path" in str(result), result


@pytest.mark.parametrize("target", _SENSITIVE_TARGETS)
async def test_write_file_cannot_write_sensitive_paths(tmp_path, target):
    result = await WriteFileTool(workspace=tmp_path).execute(path=target, content="pwned\n")
    assert "blocked (sensitive path" in str(result), result


@pytest.mark.parametrize("target", _SENSITIVE_TARGETS)
async def test_edit_file_cannot_write_sensitive_paths(tmp_path, target):
    result = await EditFileTool(workspace=tmp_path).execute(
        path=target, old_text="", new_text="pwned\n",
    )
    assert "blocked (sensitive path" in str(result), result


async def test_resolve_write_raises_on_a_symlink_to_a_sensitive_target(tmp_path):
    """The post-resolve pass must survive an innocent-looking name."""
    secret = tmp_path / "id_rsa"
    secret.write_text("KEY", encoding="utf-8")
    link = tmp_path / "notes.txt"
    link.symlink_to(secret)

    tool = WriteFileTool(workspace=tmp_path)
    with pytest.raises(SensitivePathError):
        tool._resolve_write(str(link))


async def test_ordinary_writes_still_work(tmp_path):
    """The guard must not become a blanket block."""
    result = await WriteFileTool(workspace=tmp_path).execute(
        path=str(tmp_path / "notes.md"), content="hello\n",
    )
    assert "Successfully wrote" in str(result), result
    assert (tmp_path / "notes.md").read_text(encoding="utf-8") == "hello\n"


# ---------------------------------------------------------------------------
# `ingest` was removed with the ChromaDB RAG store (MIT-1013). It was the only
# writer that store ever had, it was model-invoked, and nothing ever called it.
# These two tests used to pin its containment bound and its credential
# blocklist; what has to hold now is that the tool is gone and that the
# replacement never reads arbitrary paths on the model's say-so.
# ---------------------------------------------------------------------------


def test_ingest_tool_is_gone():
    import nanobot.agent.tools.recall as recall_module

    assert not hasattr(recall_module, "IngestTool")
    assert not hasattr(recall_module, "RAGStore")


def test_recall_tool_takes_no_path_argument():
    """Recall searches an index bound to this workspace; it opens no files."""
    from nanobot.agent.tools.recall import RecallTool

    properties = RecallTool.parameters.fget(RecallTool.__new__(RecallTool))["properties"]
    assert set(properties) == {"query", "scope", "limit"}


# ---------------------------------------------------------------------------
# validate_resolved_url is the redirect-target validator. It used to pass the
# loopback flag straight into _is_private, with no literal-host and no
# all-addresses requirement -- so a public URL that 302s to 127.0.0.1 was
# accepted. It now uses the same narrow gate as the forward path.
# ---------------------------------------------------------------------------


def _resolver(addresses):
    import socket

    def _fake(hostname, port, family=0, type_=0):
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (a, 0)) for a in addresses]

    return _fake


def test_redirect_to_loopback_from_a_public_host_stays_blocked():
    from nanobot.security.network import configure_loopback_exception, validate_resolved_url

    configure_loopback_exception(True)
    try:
        with patch("nanobot.security.network.socket.getaddrinfo", _resolver(["127.0.0.1"])):
            ok, err = validate_resolved_url("http://evil.example.com/")
        assert not ok, "DNS rebinding to loopback must not be accepted on redirect"
        assert "private" in err.lower()
    finally:
        configure_loopback_exception(False)


def test_redirect_to_a_literal_loopback_host_is_allowed_when_configured():
    from nanobot.security.network import configure_loopback_exception, validate_resolved_url

    configure_loopback_exception(True)
    try:
        with patch("nanobot.security.network.socket.getaddrinfo", _resolver(["127.0.0.1"])):
            ok, _ = validate_resolved_url("http://localhost:3000/")
        assert ok
    finally:
        configure_loopback_exception(False)


def test_redirect_to_metadata_stays_blocked_even_with_the_flag():
    from nanobot.security.network import configure_loopback_exception, validate_resolved_url

    configure_loopback_exception(True)
    try:
        ok, err = validate_resolved_url("http://169.254.169.254/computeMetadata/v1/")
        assert not ok
        assert "private" in err.lower()
    finally:
        configure_loopback_exception(False)
