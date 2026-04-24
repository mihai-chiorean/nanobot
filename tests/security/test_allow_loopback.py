"""Tests for MIT-203 allow_loopback SSRF scope-reduction flag.

The flag relaxes the SSRF guard for 127.0.0.0/8 and ::1 only. RFC1918,
CGNAT, cloud-metadata, and IPv6 ULA/link-local addresses stay blocked
regardless, so a wrapper that flips this on (Ziggy, for local dev
servers) doesn't accidentally reopen AWS-metadata-style SSRF.
"""

from __future__ import annotations

import socket
from unittest.mock import patch

import pytest

from nanobot.agent.tools.shell import ExecTool
from nanobot.security.network import (
    configure_loopback_exception,
    contains_internal_url,
    validate_url_target,
)


def _fake_resolve(host: str, results: list[str]):
    def _resolver(hostname, port, family=0, type_=0):
        if hostname == host:
            return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (ip, 0)) for ip in results]
        raise socket.gaierror(f"cannot resolve {hostname}")
    return _resolver


# ---------------------------------------------------------------------------
# Module-level contract: per-call flag and module default
# ---------------------------------------------------------------------------


def test_loopback_blocked_by_default():
    with patch("nanobot.security.network.socket.getaddrinfo", _fake_resolve("localhost", ["127.0.0.1"])):
        ok, err = validate_url_target("http://localhost:9090/api")
    assert not ok
    assert "127.0.0.1" in err or "private" in err.lower()


def test_loopback_allowed_when_per_call_flag_set():
    with patch("nanobot.security.network.socket.getaddrinfo", _fake_resolve("localhost", ["127.0.0.1"])):
        ok, err = validate_url_target("http://localhost:9090/api", allow_loopback=True)
    assert ok, f"allow_loopback=True should let 127.0.0.1 through, got: {err}"


def test_ipv6_loopback_allowed_when_flag_set():
    def _resolver(hostname, port, family=0, type_=0):
        return [(socket.AF_INET6, socket.SOCK_STREAM, 0, "", ("::1", 0, 0, 0))]
    with patch("nanobot.security.network.socket.getaddrinfo", _resolver):
        ok, _ = validate_url_target("http://myhost/", allow_loopback=True)
    assert ok


# ---------------------------------------------------------------------------
# Scope reduction — everything else stays blocked
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ip,label", [
    ("169.254.169.254", "aws-metadata"),
    ("10.0.0.1", "rfc1918-10"),
    ("172.16.5.1", "rfc1918-172"),
    ("192.168.1.1", "rfc1918-192"),
    ("100.100.1.1", "cgnat"),
])
def test_loopback_flag_does_not_unblock_other_privates(ip: str, label: str):
    """allow_loopback=True must NOT unblock anything else."""
    with patch("nanobot.security.network.socket.getaddrinfo", _fake_resolve("evil.com", [ip])):
        ok, _ = validate_url_target("http://evil.com/", allow_loopback=True)
    assert not ok, f"{label} ({ip}) must stay blocked even with allow_loopback=True"


def test_public_ip_always_allowed_regardless_of_flag():
    with patch("nanobot.security.network.socket.getaddrinfo", _fake_resolve("example.com", ["93.184.216.34"])):
        ok, _ = validate_url_target("http://example.com/", allow_loopback=False)
    assert ok


# ---------------------------------------------------------------------------
# contains_internal_url — wired for shell command scanning
# ---------------------------------------------------------------------------


def test_contains_internal_url_blocks_localhost_by_default():
    with patch("nanobot.security.network.socket.getaddrinfo", _fake_resolve("localhost", ["127.0.0.1"])):
        assert contains_internal_url("curl http://localhost:3000/api/health")


def test_contains_internal_url_allows_localhost_when_flag_set():
    with patch("nanobot.security.network.socket.getaddrinfo", _fake_resolve("localhost", ["127.0.0.1"])):
        assert not contains_internal_url(
            "curl http://localhost:3000/api/health", allow_loopback=True
        )


def test_contains_internal_url_still_blocks_metadata_with_flag():
    with patch(
        "nanobot.security.network.socket.getaddrinfo",
        _fake_resolve("169.254.169.254", ["169.254.169.254"]),
    ):
        assert contains_internal_url(
            "curl http://169.254.169.254/computeMetadata/v1/", allow_loopback=True
        )


# ---------------------------------------------------------------------------
# Module-level default — configure_loopback_exception
# ---------------------------------------------------------------------------


def test_module_default_round_trip():
    try:
        configure_loopback_exception(True)
        with patch("nanobot.security.network.socket.getaddrinfo", _fake_resolve("localhost", ["127.0.0.1"])):
            # With None (unset) per-call arg, the module default applies.
            ok, _ = validate_url_target("http://localhost:9090/", allow_loopback=None)
        assert ok
    finally:
        configure_loopback_exception(False)

    with patch("nanobot.security.network.socket.getaddrinfo", _fake_resolve("localhost", ["127.0.0.1"])):
        ok, _ = validate_url_target("http://localhost:9090/", allow_loopback=None)
    assert not ok


def test_per_call_overrides_module_default():
    """A per-call False beats a module-level True, and vice versa."""
    try:
        configure_loopback_exception(True)
        with patch("nanobot.security.network.socket.getaddrinfo", _fake_resolve("localhost", ["127.0.0.1"])):
            ok, _ = validate_url_target("http://localhost/", allow_loopback=False)
        assert not ok
    finally:
        configure_loopback_exception(False)


# ---------------------------------------------------------------------------
# ExecTool integration — shell tool threads the flag through _guard_command
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exec_tool_blocks_localhost_by_default():
    tool = ExecTool()
    with patch("nanobot.security.network.socket.getaddrinfo", _fake_resolve("localhost", ["127.0.0.1"])):
        result = await tool.execute(command="curl http://localhost:3000/api/health")
    assert result.startswith("Error: Command blocked by safety guard")


@pytest.mark.asyncio
async def test_exec_tool_allows_localhost_when_configured():
    """With allow_loopback=True the prescreen lets curl-to-localhost through.

    We don't actually fetch anything — the command fails at the subprocess
    layer since we don't have a local server — but the key assertion is
    that the prescreen is no longer the reason it fails.
    """
    tool = ExecTool(allow_loopback=True)
    with patch("nanobot.security.network.socket.getaddrinfo", _fake_resolve("localhost", ["127.0.0.1"])):
        result = await tool.execute(command="echo dry-run http://localhost:3000/")
    assert not result.startswith("Error: Command blocked by safety guard")


@pytest.mark.asyncio
async def test_exec_tool_still_blocks_metadata_with_flag():
    tool = ExecTool(allow_loopback=True)
    with patch(
        "nanobot.security.network.socket.getaddrinfo",
        _fake_resolve("169.254.169.254", ["169.254.169.254"]),
    ):
        result = await tool.execute(
            command='curl http://169.254.169.254/computeMetadata/v1/',
        )
    assert result.startswith("Error: Command blocked by safety guard")
