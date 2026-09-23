"""Network security utilities — SSRF protection and internal URL detection."""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from contextlib import contextmanager, suppress
from collections.abc import Iterable
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
# Prefix of the rejection reason used when a hostname does not resolve.
# Kept as a constant because the exec guard keys off it to tell an
# unreachable-name refusal apart from a private-network refusal.
_UNRESOLVABLE_REASON_PREFIX = "Cannot resolve hostname:"
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


def _is_private(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    normalized = _normalize_addr(addr)
    if _allowed_networks and any(normalized in net for net in _allowed_networks):
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
            return False, f"{_UNRESOLVABLE_REASON_PREFIX} {hostname}", ()

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


URLOrigin = tuple[str, str, int]


def url_origin(url: str) -> URLOrigin | None:
    """Return ``(scheme, host, port)`` for an http(s) URL, with the default port filled in."""
    try:
        parts = urlparse(url)
        port = parts.port
    except ValueError:
        return None
    scheme = (parts.scheme or "").lower()
    host = (parts.hostname or "").rstrip(".").lower()
    if scheme not in ("http", "https") or not host:
        return None
    return scheme, host, port or (443 if scheme == "https" else 80)


def is_loopback_origin(origin: URLOrigin | None) -> bool:
    """True when the origin's host is a literal loopback host (not a DNS name)."""
    return origin is not None and is_loopback_host(origin[1])


class PinnedDNSAsyncTransport(httpx.AsyncBaseTransport):
    """HTTPX transport that pins each request to the IPs validated for its URL.

    Ziggy-local (MIT-1405): ``loopback_origins`` lets an operator-configured
    endpoint (an MCP server URL or its token URL) on a literal loopback host be
    reached at exactly that scheme/host/port. Every other request through the
    transport, including a redirect to another loopback port, keeps the full
    SSRF policy. The process-wide loopback default is not consulted or changed.
    """

    _resolver_lock = asyncio.Lock()

    def __init__(
        self,
        *,
        allow_loopback: bool = False,
        loopback_origins: Iterable[URLOrigin] = (),
        inner: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._allow_loopback = allow_loopback
        self._loopback_origins = frozenset(
            origin for origin in loopback_origins if is_loopback_origin(origin)
        )
        self._inner = inner or httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        allow_loopback = self._allow_loopback or (
            bool(self._loopback_origins) and url_origin(url) in self._loopback_origins
        )
        ok, error, resolved_ips = resolve_url_target(url, allow_loopback=allow_loopback)
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
        # Ziggy-local (fork): use the same narrow gate as resolve_url_target --
        # a literal loopback host whose every address is loopback. Passing the
        # flag straight into _is_private would accept a public URL that 302s to
        # 127.0.0.1, which is precisely the rebinding case upstream hardened
        # against on the forward path.
        if effective_loopback and _is_allowed_loopback_target(hostname, [addr]):
            return True, ""
        if _is_private(addr):
            return False, f"Redirect target is a private address: {addr}"
    except ValueError:
        # hostname is a domain name, resolve it
        try:
            infos = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        except socket.gaierror:
            return True, ""
        resolved: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
        for info in infos:
            try:
                resolved.append(ipaddress.ip_address(info[4][0]))
            except ValueError:
                continue
        if effective_loopback and _is_allowed_loopback_target(hostname, resolved):
            return True, ""
        for addr in resolved:
            if _is_private(addr):
                return False, f"Redirect target {hostname} resolves to private address {addr}"

    return True, ""


def find_internal_url(
    command: str,
    *,
    allow_loopback: bool | None = None,
) -> tuple[str, str] | None:
    """Return ``(url, reason)`` for the first URL in *command* that fails validation.

    Returns ``None`` when every URL in the command is an acceptable public
    target.  Callers should prefer this over :func:`contains_internal_url`:
    a bare boolean forces the guard to emit a generic message, which leaves
    the model unable to tell *which* URL of a compound command was refused
    or *why*.  Same defect, and same fix, as the filesystem-path guard in
    797a7bfc.
    """
    for m in _URL_RE.finditer(command):
        url = m.group(0)
        ok, error = validate_url_target(url, allow_loopback=allow_loopback)
        if not ok:
            return url, error
    return None


def is_unresolvable_reason(reason: str) -> bool:
    """Return whether a rejection reason is "the name does not resolve".

    This is deliberately distinguished from a private/internal target.  A
    hostname that does not resolve is still refused (the guard is
    fail-closed, which is what defeats split-horizon and rebinding tricks),
    but it is *not* evidence of an attempt to reach a private network -- it
    is usually a typo or an invented domain.  Reporting it as an SSRF
    boundary tells the caller to stop trying entirely, when the useful
    advice is "that host does not exist, use a real endpoint".
    """
    return reason.strip().lower().startswith(_UNRESOLVABLE_REASON_PREFIX.lower())


def contains_internal_url(command: str, *, allow_loopback: bool | None = None) -> bool:
    """Return True if the command string contains a URL targeting an internal/private address.

    Ziggy-local (fork): ``allow_loopback=None`` defers to the module default
    set by :func:`configure_loopback_exception`. Explicit True/False wins.

    Retained for callers that only need the boolean; :func:`find_internal_url`
    additionally reports which URL was rejected and why.
    """
    return find_internal_url(command, allow_loopback=allow_loopback) is not None


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
