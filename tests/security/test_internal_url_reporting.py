"""Regression tests: a blocked URL must name *which* URL and *why*.

Production incident (owner session 2026-09-15 16:30:43, MIT-1011).  The model
tried ``curl https://serpapi.oss-accel.online-third-party-pages.com/search?...``
and the turn died with

    Error: Command blocked by safety guard (internal/private URL detected)

Two things were wrong with that outcome:

1.  The host is *not* internal.  It does not exist at all -- the registrable
    domain ``online-third-party-pages.com`` is NXDOMAIN, confirmed against both
    the host resolver and 8.8.8.8.  The guard was right to refuse it (it is
    fail-closed on unresolvable hostnames, which is what stops DNS-rebinding
    and split-horizon tricks), but the *message* claimed a private-network
    target and so the model was told it had hit a non-bypassable security
    boundary.  The correct feedback is "that hostname does not resolve", which
    the model can act on by picking a real endpoint.

2.  The message named neither the URL nor the reason, so with several URLs in
    one compound command the model could not tell which one was rejected --
    the same defect fixed for filesystem paths in 797a7bfc.

These tests pin the classifier's actual behaviour (public hosts allowed, every
private range still blocked) and the reporting contract.
"""

from __future__ import annotations

import socket
from unittest.mock import patch

import pytest

from nanobot.agent.tools.execution import (
    _classify_violation,
    _unresolvable_host_key,
    is_ssrf_violation,
    is_unresolvable_host,
)
from nanobot.agent.tools.shell import ExecTool
from nanobot.providers.base import ToolCallRequest
from nanobot.utils.runtime import (
    repeated_external_lookup_error,
    repeated_workspace_violation_error,
)
from nanobot.security.network import (
    contains_internal_url,
    find_internal_url,
    validate_url_target,
)

# The exact hostname from the incident, plus ordinary public hosts.
_INCIDENT_HOST = "serpapi.oss-accel.online-third-party-pages.com"

_PUBLIC_HOSTS = {
    "serpapi.com": ["162.159.142.21"],
    "example.com": ["93.184.216.34"],
    "api.openai.com": ["104.18.7.192"],
    # A public host whose name contains substrings a naive keyword blocklist
    # would trip on ("oss", "accel", ".online", "internal").
    "oss-accel.online.internal-docs.example.net": ["203.0.113.10"],
}

# host -> address, for destinations that must stay blocked.
_PRIVATE_HOSTS = {
    "rfc1918-a.example.com": "10.0.0.5",
    "rfc1918-b.example.com": "172.16.4.9",
    "rfc1918-c.example.com": "192.168.1.5",
    "cgnat.example.com": "100.64.0.7",
    "loopback.example.com": "127.0.0.1",
    "metadata.example.com": "169.254.169.254",
    "linklocal.example.com": "169.254.10.10",
    "ula.example.com": "fd00::1",
    "linklocal6.example.com": "fe80::1",
}


def _resolver(hostname, port=None, family=0, type_=0, *args, **kwargs):
    """Hermetic getaddrinfo: known hosts resolve, everything else is NXDOMAIN."""
    if hostname in _PUBLIC_HOSTS:
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", (ip, 0))
            for ip in _PUBLIC_HOSTS[hostname]
        ]
    if hostname in _PRIVATE_HOSTS:
        ip = _PRIVATE_HOSTS[hostname]
        fam = socket.AF_INET6 if ":" in ip else socket.AF_INET
        return [(fam, socket.SOCK_STREAM, 0, "", (ip, 0))]
    raise socket.gaierror(f"Name or service not known: {hostname}")


@pytest.fixture
def dns():
    with patch("nanobot.security.network.socket.getaddrinfo", _resolver):
        yield


# ---------------------------------------------------------------------------
# Classifier boundaries -- these must not regress while fixing the message.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("host", sorted(_PUBLIC_HOSTS))
def test_public_hostnames_are_allowed(dns, host: str) -> None:
    ok, error = validate_url_target(f"https://{host}/search?q=paris")
    assert ok, f"public host {host} must not be blocked (got {error!r})"
    assert not contains_internal_url(f"curl -s https://{host}/search?q=paris")


@pytest.mark.parametrize("host", sorted(_PRIVATE_HOSTS))
def test_private_destinations_stay_blocked(dns, host: str) -> None:
    url = f"http://{host}/"
    ok, error = validate_url_target(url)
    assert not ok, f"{host} ({_PRIVATE_HOSTS[host]}) must stay blocked"
    assert _PRIVATE_HOSTS[host] in error
    assert contains_internal_url(f"curl {url}")


@pytest.mark.parametrize(
    "literal",
    [
        "http://10.0.0.5/", "http://172.16.4.9/", "http://192.168.1.5/",
        "http://100.64.0.7/", "http://127.0.0.1/", "http://169.254.169.254/",
        "http://[fd00::1]/", "http://[fe80::1]/", "http://[::1]/",
    ],
)
def test_private_ip_literals_stay_blocked(dns, literal: str) -> None:
    ok, _ = validate_url_target(literal)
    assert not ok, f"{literal} must stay blocked"


def test_unresolvable_host_is_still_blocked(dns) -> None:
    """Fail-closed on NXDOMAIN is deliberate; do not relax it."""
    ok, _ = validate_url_target(f"https://{_INCIDENT_HOST}/search?q=paris")
    assert not ok


# ---------------------------------------------------------------------------
# Reporting contract -- these are the regressions being fixed.
# ---------------------------------------------------------------------------


def test_unresolvable_host_is_not_reported_as_internal(dns) -> None:
    """NXDOMAIN must not be described as a private/internal address."""
    _, error = validate_url_target(f"https://{_INCIDENT_HOST}/search")
    assert _INCIDENT_HOST in error
    assert "resolve" in error.lower()
    assert "private" not in error.lower()
    assert "internal" not in error.lower()


def test_find_internal_url_names_the_offending_url(dns) -> None:
    """The scanner reports which URL failed and why, not just a boolean."""
    command = (
        "curl -s https://serpapi.com/search?q=a "
        "&& curl -s http://metadata.example.com/latest/meta-data/"
    )
    found = find_internal_url(command)
    assert found is not None
    url, reason = found
    assert url == "http://metadata.example.com/latest/meta-data/"
    assert "169.254.169.254" in reason
    assert find_internal_url("curl -s https://serpapi.com/search?q=a") is None


def test_guard_message_names_the_private_url_and_reason(dns, tmp_path) -> None:
    tool = ExecTool(working_dir=str(tmp_path))
    error = tool._guard_command(
        "curl -s http://metadata.example.com/latest/meta-data/", str(tmp_path)
    )
    assert error is not None
    # Existing callers/tests match on the parenthesised code -- keep it.
    assert "(internal/private URL detected)" in error
    assert "http://metadata.example.com/latest/meta-data/" in error
    assert "169.254.169.254" in error
    assert is_ssrf_violation(error), "a real private target stays a hard SSRF boundary"


def test_guard_message_for_unresolvable_host_is_actionable(dns, tmp_path) -> None:
    """The incident case: say the host does not resolve, not that it is internal."""
    tool = ExecTool(working_dir=str(tmp_path))
    command = f'curl -s "https://{_INCIDENT_HOST}/search?q=paris"'
    error = tool._guard_command(command, str(tmp_path))

    assert error is not None, "unresolvable host must still be blocked"
    assert _INCIDENT_HOST in error, "the model must be told which URL failed"
    assert "resolve" in error.lower()
    # It must NOT be mislabelled as a private-network target, and must not be
    # escalated to the non-bypassable SSRF boundary -- that dead-ends the model
    # on what is really a bad/typo'd hostname.
    assert "(internal/private URL detected)" not in error
    assert not is_ssrf_violation(error)
    # The positive half of the contract: the classifier must actually recognise
    # what ExecTool emits. Asserting only the negative lets a reworded guard
    # message silently stop being classified -- it would fall through to a plain
    # tool error with no throttling and no unresolvable_host breadcrumb.
    assert is_unresolvable_host(error)
    assert _unresolvable_host_key(error) == f"unresolvable:{_INCIDENT_HOST}", (
        "the throttle regex must survive the real message layout, separator included"
    )


def test_distinct_dead_hostnames_are_capped_in_aggregate(dns) -> None:
    """A model that invents a *new* bogus host each time must still be stopped.

    A purely per-host counter never fires in that case, which is the case the
    incident actually produced.
    """
    call = ToolCallRequest(id="c", name="exec", arguments={"command": "curl x"})
    counts: dict[str, int] = {}
    payloads = []
    for i in range(4):
        raw = (
            "Error: Command blocked by safety guard (unresolvable hostname): "
            f"https://guess{i}.example.invalid/ - Cannot resolve hostname: guess{i}.example.invalid"
        )
        handled = _classify_violation(
            raw_text=raw, soft_payload=raw,
            event={"name": "exec", "status": "error", "detail": ""},
            tool_call=call, workspace_violation_counts=counts,
        )
        assert handled is not None
        payloads.append(handled[0])

    assert all("Stop guessing URLs" not in p for p in payloads[:3])
    assert "Stop guessing URLs" in payloads[3], "aggregate budget must escalate"


def test_unresolvable_counter_does_not_disturb_other_budgets(dns) -> None:
    """The shared counts dict is namespaced; other throttles must be unaffected."""
    counts: dict[str, int] = {}
    raw = (
        "Error: Command blocked by safety guard (unresolvable hostname): "
        "https://x.invalid/ - Cannot resolve hostname: x.invalid"
    )
    for _ in range(3):
        _classify_violation(
            raw_text=raw, soft_payload=raw,
            event={"name": "exec", "status": "error", "detail": ""},
            tool_call=ToolCallRequest(id="c", name="exec", arguments={"command": "curl x"}),
            workspace_violation_counts=counts,
        )

    # Neither neighbouring throttle sees the unresolvable keys.
    assert repeated_external_lookup_error(
        "web_fetch", {"url": "https://example.com"}, counts) is None
    assert repeated_workspace_violation_error(
        "read_file", {"path": "/tmp/outside.md"}, counts) is None


def test_repeated_ssrf_blocks_are_bounded(dns) -> None:
    """The block never yields, but the probing must still be capped.

    Before the turn-abort was removed, the first SSRF hit ended the turn. With
    that circuit breaker gone, an injected model would otherwise get a fresh
    probe every iteration until max_iterations.
    """
    call = ToolCallRequest(id="c", name="exec", arguments={"command": "curl x"})
    counts: dict[str, int] = {}
    payloads = []
    for host in ("10.0.0.5", "192.168.1.5", "169.254.169.254", "172.16.4.9"):
        raw = (
            "Error: Command blocked by safety guard (internal/private URL detected): "
            f"http://{host}/ - Blocked: {host} resolves to private/internal address {host}"
        )
        handled = _classify_violation(
            raw_text=raw, soft_payload=raw,
            event={"name": "exec", "status": "error", "detail": ""},
            tool_call=call, workspace_violation_counts=counts,
        )
        assert handled is not None
        payloads.append(handled[0])

    assert all("refusing repeated attempts" not in p for p in payloads[:3])
    assert "refusing repeated attempts" in payloads[3]
    # Still classified as SSRF the whole way through -- never downgraded.
    assert all(is_ssrf_violation(p) for p in payloads)
