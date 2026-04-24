"""Network security utilities — SSRF protection and internal URL detection."""

from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import urlparse

_BLOCKED_NETWORKS = [
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),   # carrier-grade NAT
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),   # link-local / cloud metadata
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),          # unique local
    ipaddress.ip_network("fe80::/10"),         # link-local v6
]

# IPv4/IPv6 loopback only. When ``allow_loopback=True`` is passed through
# (or ``configure_loopback_exception(True)`` has been called), these
# ranges are treated as public. Cloud-metadata (169.254.0.0/16) stays
# blocked either way — the SSRF guard is scope-reduced, not disabled.
_LOOPBACK_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
]

_URL_RE = re.compile(r"https?://[^\s\"'`;|<>]+", re.IGNORECASE)

_allowed_networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
# Module-level default for ``allow_loopback``. Ziggy's config flips this to
# True at boot via :func:`configure_loopback_exception`; nanobot proper
# leaves it False. Per-call ``allow_loopback=`` arguments take precedence.
_loopback_allowed_default: bool = False


def configure_ssrf_whitelist(cidrs: list[str]) -> None:
    """Allow specific CIDR ranges to bypass SSRF blocking (e.g. Tailscale's 100.64.0.0/10)."""
    global _allowed_networks
    nets = []
    for cidr in cidrs:
        try:
            nets.append(ipaddress.ip_network(cidr, strict=False))
        except ValueError:
            pass
    _allowed_networks = nets


def configure_loopback_exception(allow: bool) -> None:
    """Set the module-level default for the ``allow_loopback`` flag.

    Called by the config loader at boot. When ``True``, ``127.0.0.0/8``
    and ``::1/128`` stop being treated as internal for SSRF purposes —
    but every other private range, and crucially the cloud metadata
    service at ``169.254.169.254``, remains blocked.
    """
    global _loopback_allowed_default
    _loopback_allowed_default = bool(allow)


def _is_private(
    addr: ipaddress.IPv4Address | ipaddress.IPv6Address,
    *,
    allow_loopback: bool = False,
) -> bool:
    if _allowed_networks and any(addr in net for net in _allowed_networks):
        return False
    if allow_loopback and any(addr in net for net in _LOOPBACK_NETWORKS):
        return False
    return any(addr in net for net in _BLOCKED_NETWORKS)


def validate_url_target(url: str, *, allow_loopback: bool | None = None) -> tuple[bool, str]:
    """Validate a URL is safe to fetch: scheme, hostname, and resolved IPs.

    Returns ``(ok, error_message)``. When ``ok`` is ``True``,
    ``error_message`` is empty.

    ``allow_loopback``:
        If ``True``, treat ``127.0.0.0/8`` / ``::1`` as public. If
        ``False``, keep them blocked. If ``None`` (the default), fall back
        to the module-level default set via
        :func:`configure_loopback_exception` (Ziggy flips this to ``True``
        at boot; upstream nanobot keeps it ``False``). Per-call ``True`` /
        ``False`` always wins over the module default.

        Cloud-metadata (169.254.0.0/16), RFC1918, CGNAT, and IPv6 ULAs
        stay blocked regardless — this flag only relaxes loopback.
    """
    effective_loopback = _loopback_allowed_default if allow_loopback is None else allow_loopback
    try:
        p = urlparse(url)
    except Exception as e:
        return False, str(e)

    if p.scheme not in ("http", "https"):
        return False, f"Only http/https allowed, got '{p.scheme or 'none'}'"
    if not p.netloc:
        return False, "Missing domain"

    hostname = p.hostname
    if not hostname:
        return False, "Missing hostname"

    try:
        infos = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except socket.gaierror:
        return False, f"Cannot resolve hostname: {hostname}"

    for info in infos:
        try:
            addr = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if _is_private(addr, allow_loopback=effective_loopback):
            return False, f"Blocked: {hostname} resolves to private/internal address {addr}"

    return True, ""


def validate_resolved_url(url: str, *, allow_loopback: bool | None = None) -> tuple[bool, str]:
    """Validate an already-fetched URL (e.g. after redirect). Only checks the IP, skips DNS."""
    effective_loopback = _loopback_allowed_default if allow_loopback is None else allow_loopback
    try:
        p = urlparse(url)
    except Exception:
        return True, ""

    hostname = p.hostname
    if not hostname:
        return True, ""

    try:
        addr = ipaddress.ip_address(hostname)
        if _is_private(addr, allow_loopback=effective_loopback):
            return False, f"Redirect target is a private address: {addr}"
    except ValueError:
        # hostname is a domain name, resolve it
        try:
            infos = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        except socket.gaierror:
            return True, ""
        for info in infos:
            try:
                addr = ipaddress.ip_address(info[4][0])
            except ValueError:
                continue
            if _is_private(addr, allow_loopback=effective_loopback):
                return False, f"Redirect target {hostname} resolves to private address {addr}"

    return True, ""


def contains_internal_url(command: str, *, allow_loopback: bool | None = None) -> bool:
    """Return True if the command string contains a URL targeting an internal/private address.

    ``allow_loopback`` is forwarded to :func:`validate_url_target`; see that
    docstring for the scope-reduction contract. Cloud-metadata, RFC1918,
    and CGNAT addresses stay blocked regardless.
    """
    for m in _URL_RE.finditer(command):
        url = m.group(0)
        ok, _ = validate_url_target(url, allow_loopback=allow_loopback)
        if not ok:
            return True
    return False
