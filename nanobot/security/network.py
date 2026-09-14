"""Network security utilities — SSRF protection and internal URL detection."""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from contextlib import contextmanager, suppress
from typing import Any, cast
from urllib.parse import urlparse
from urllib.request import getproxies, proxy_bypass

import httpx

_BLOCKED_NETWORKS = [
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),   # carrier-grade NAT
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),   # link-local / cloud metadata
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("::/128"),            # unspecified; may route to local host
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


def is_loopback_host(host: str) -> bool:
    """Return whether a bind target is explicitly limited to loopback."""
    normalized = host.strip().rstrip(".").lower()
    if normalized == "localhost":
        return True
    if normalized.startswith("[") and normalized.endswith("]"):
        normalized = normalized[1:-1]
    with suppress(ValueError):
        return ipaddress.ip_address(normalized).is_loopback
    return False


def configure_ssrf_whitelist(cidrs: list[str]) -> None:
    """Allow specific CIDR ranges to bypass SSRF blocking (e.g. Tailscale's 100.64.0.0/10)."""
    global _allowed_networks
    nets: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for cidr in cidrs:
        with suppress(ValueError):
            nets.append(ipaddress.ip_network(cidr, strict=False))
    _allowed_networks = nets


def configure_loopback_exception(allow: bool) -> None:
    """Ziggy-local (fork, MIT-203): set the module-level ``allow_loopback`` default.

    Called by the config loader at boot from ``tools.exec.allow_loopback``.
    When ``True``, ``127.0.0.0/8`` and ``::1/128`` stop being treated as
    internal for SSRF purposes. Every other private range — and crucially
    the cloud metadata service at 169.254.169.254 — stays blocked.

    Upstream's own narrow loopback allowance (a literal-loopback-host check
    gated on the WebUI full-access scope) is unchanged; this only supplies
    the default when a caller passes ``allow_loopback=None``.
    """
    global _loopback_allowed_default
    _loopback_allowed_default = bool(allow)


def _normalize_addr(
    addr: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """Normalize IPv6-mapped IPv4 addresses to their IPv4 form.

    ``::ffff:127.0.0.1`` is semantically identical to ``127.0.0.1`` but
    Python's ipaddress treats it as an IPv6Address that matches neither
    ``127.0.0.0/8`` nor ``::1/128``.  Converting it to IPv4 ensures
    blocklist/allowlist checks work correctly.
    """
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return addr.ipv4_mapped
    return addr


def _is_private(
    addr: ipaddress.IPv4Address | ipaddress.IPv6Address,
    *,
    allow_loopback: bool = False,
) -> bool:
    normalized = _normalize_addr(addr)
    if _allowed_networks and any(normalized in net for net in _allowed_networks):
        return False
    # Ziggy-local (fork, MIT-203): loopback-only scope reduction. Cloud metadata,
    # RFC1918, CGNAT and IPv6 ULAs stay blocked.
    if allow_loopback and any(normalized in net for net in _LOOPBACK_NETWORKS):
        return False
    return any(normalized in net for net in _BLOCKED_NETWORKS)


def resolve_url_target(
    url: str,
    *,
    allow_loopback: bool | None = None,
    trust_remote_dns: bool = False,
) -> tuple[bool, str, tuple[str, ...]]:
    """Validate a URL is safe to fetch: scheme, hostname, and resolved IPs.

    ``allow_loopback`` is intentionally narrow: it only permits literal
    loopback hosts (localhost, 127.0.0.0/8, ::1) when every resolved address is
    loopback. It does not allow RFC1918, link-local, metadata, or public DNS
    names that happen to resolve to loopback.

    ``trust_remote_dns`` accepts ordinary hostnames unavailable to local DNS.
    This is only safe when a user-configured trusted proxy owns final DNS
    resolution and network egress. Localhost names and private/internal IP
    literals remain blocked.

    Returns (ok, error_message, resolved_ips).  When ok is True,
    resolved_ips contains the public IPs that were validated for this URL, or
    is empty when an unresolved hostname is delegated to a trusted proxy.
    """
    effective_loopback = _loopback_allowed_default if allow_loopback is None else allow_loopback
    try:
        p = urlparse(url)
    except Exception as e:
        return False, str(e), ()

    if p.scheme not in ("http", "https"):
        return False, f"Only http/https allowed, got '{p.scheme or 'none'}'", ()
    if not p.netloc:
        return False, "Missing domain", ()

    hostname = p.hostname
    if not hostname:
        return False, "Missing hostname", ()

    try:
        infos = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except socket.gaierror:
        if not trust_remote_dns:
            return False, f"Cannot resolve hostname: {hostname}", ()

        normalized_hostname = hostname.rstrip(".").lower()
        if normalized_hostname == "localhost" or normalized_hostname.endswith(".localhost"):
            return False, f"Blocked local/internal hostname: {hostname}", ()

        try:
            literal_addr = ipaddress.ip_address(normalized_hostname)
        except ValueError:
            return True, "", ()
        if _is_private(literal_addr):
            return False, f"Blocked private/internal address: {literal_addr}", ()
        return True, "", (str(_normalize_addr(literal_addr)),)

    addrs: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for info in infos:
        try:
            addr = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        addrs.append(addr)
    if effective_loopback and _is_allowed_loopback_target(hostname, addrs):
        return True, "", tuple(dict.fromkeys(str(_normalize_addr(addr)) for addr in addrs))
    for addr in addrs:
        if _is_private(addr):
            return False, f"Blocked: {hostname} resolves to private/internal address {addr}", ()

    return True, "", tuple(dict.fromkeys(str(_normalize_addr(addr)) for addr in addrs))


def validate_url_target(url: str, *, allow_loopback: bool | None = None) -> tuple[bool, str]:
    """Validate a URL is safe to fetch: scheme, hostname, and resolved IPs."""
    ok, error, _ = resolve_url_target(url, allow_loopback=allow_loopback)
    return ok, error


def env_proxy_applies_to_url(url: str) -> bool:
    """Return True when process proxy settings would proxy this URL."""
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False

    proxies = getproxies()
    proxy_url = proxies.get(parsed.scheme) or proxies.get("all")
    if not proxy_url:
        return False

    host = parsed.hostname
    if parsed.port is not None:
        host = f"[{host}]:{parsed.port}" if ":" in host else f"{host}:{parsed.port}"
    return not proxy_bypass(host)


def httpx_env_proxy_mounts() -> dict[str, httpx.AsyncBaseTransport | None]:
    """Build HTTPX proxy mounts while leaving direct routes to the base transport."""
    proxies = getproxies()
    mounts: dict[str, httpx.AsyncBaseTransport | None] = {}
    for scheme in ("http", "https", "all"):
        proxy_url = proxies.get(scheme)
        if proxy_url:
            if "://" not in proxy_url:
                proxy_url = f"http://{proxy_url}"
            mounts[f"{scheme}://"] = httpx.AsyncHTTPTransport(proxy=httpx.Proxy(proxy_url))

    if not mounts:
        return {}

    no_proxy = proxies.get("no", "")
    if no_proxy == "*":
        return {}
    for entry in no_proxy.split(","):
        pattern = _no_proxy_mount_pattern(entry.strip())
        if pattern:
            mounts[pattern] = None
    return mounts


def _no_proxy_mount_pattern(hostname: str) -> str | None:
    if not hostname:
        return None
    if "://" in hostname:
        return hostname

    unbracketed = hostname.strip("[]")
    with suppress(ValueError):
        addr = ipaddress.ip_address(unbracketed)
        return f"all://[{addr}]" if addr.version == 6 else f"all://{addr}"

    if hostname.lower() == "localhost":
        return "all://localhost"
    return f"all://*{hostname}"


@contextmanager
def pin_resolved_url_dns(url: str, resolved_ips: tuple[str, ...]):
    """Pin DNS lookups for the URL hostname to previously validated IPs.

    This temporarily overrides process-global resolver state. Do not use it
    directly across awaits unless the caller serializes access; prefer
    PinnedDNSAsyncTransport for HTTP requests.
    """
    try:
        hostname = urlparse(url).hostname
    except Exception:
        hostname = None
    if not hostname or not resolved_ips:
        yield
        return

    pinned_host = hostname.rstrip(".").lower()
    original_getaddrinfo = socket.getaddrinfo

    def _getaddrinfo(
        host: Any,
        port: Any,
        family: int = 0,
        type: int = 0,  # noqa: A002
        proto: int = 0,
        flags: int = 0,
    ) -> list[Any]:
        if str(host).rstrip(".").lower() != pinned_host:
            return original_getaddrinfo(host, port, family, type, proto, flags)
        infos: list[Any] = []
        for ip in resolved_ips:
            addr = ipaddress.ip_address(ip)
            addr_family = socket.AF_INET6 if addr.version == 6 else socket.AF_INET
            if family not in (0, socket.AF_UNSPEC, addr_family):
                continue
            sockaddr = (ip, port or 0, 0, 0) if addr_family == socket.AF_INET6 else (ip, port or 0)
            infos.append((addr_family, type or socket.SOCK_STREAM, proto, "", sockaddr))
        return infos

    socket.getaddrinfo = cast(Any, _getaddrinfo)
    try:
        yield
    finally:
        socket.getaddrinfo = original_getaddrinfo


class UnsafeURLRequestError(httpx.RequestError):
    """Raised when an outgoing request is rejected by URL safety validation."""


class PinnedDNSAsyncTransport(httpx.AsyncBaseTransport):
    """HTTPX transport that pins each request to the IPs validated for its URL."""

    _resolver_lock = asyncio.Lock()

    def __init__(
        self,
        *,
        allow_loopback: bool = False,
        inner: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._allow_loopback = allow_loopback
        self._inner = inner or httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        ok, error, resolved_ips = resolve_url_target(url, allow_loopback=self._allow_loopback)
        if not ok:
            raise UnsafeURLRequestError(error, request=request)
        async with self._resolver_lock:
            with pin_resolved_url_dns(url, resolved_ips):
                return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


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

    Ziggy-local (fork): ``allow_loopback=None`` defers to the module default
    set by :func:`configure_loopback_exception`. Explicit True/False wins.
    """
    for m in _URL_RE.finditer(command):
        url = m.group(0)
        ok, _ = validate_url_target(url, allow_loopback=allow_loopback)
        if not ok:
            return True
    return False


def _is_allowed_loopback_target(
    hostname: str,
    addrs: list[ipaddress.IPv4Address | ipaddress.IPv6Address],
) -> bool:
    if not addrs or not all(_normalize_addr(addr).is_loopback for addr in addrs):
        return False
    normalized = hostname.rstrip(".").lower()
    if normalized == "localhost":
        return True
    with suppress(ValueError):
        return ipaddress.ip_address(hostname).is_loopback
    return False
