"""Ziggy-local (MIT-1857, OA-15): the ``work.finished`` push for hand-off tasks.

When a Work task flagged ``notify_on_finish`` (the OA-12 hand-off sets it)
reaches ``succeeded``, ``failed`` or ``interrupted``, the runtime tells the
tenant connector so exactly one push reaches the tester's own devices:
``POST {connectors}/runtime/work/finished`` with ``{task_id, title, status}``.

* Auth is the runtime bearer the MCP client already mints for the
  ziggy-connectors server (``mcp_client_credentials.py``); connectors takes
  the tenant from that bearer's claims, never from the body.
* A tester cancellation never reports here -- ``cancelled`` is not a push
  status and the loop only calls in for the three that are.
* Transport mirrors the MCP client's: the pinned-DNS/SSRF-guarded httpx
  stack and the same token client, so this call can reach exactly the
  endpoints the operator configured the connectors server to use.

``notify_finished`` never raises: a failed push is a log line, not a broken
turn (the result is still in Work).
"""

from __future__ import annotations

import asyncio
import urllib.parse
from typing import Any

import httpx
from loguru import logger

# The three endings that push (design §4 "The push"); a tester-made
# ``cancelled`` is deliberately absent.
PUSH_STATUSES = frozenset({"succeeded", "failed", "interrupted"})

FINISHED_PATH = "/runtime/work/finished"
# Per-attempt request timeout; the whole call still bounds itself by making
# exactly two attempts.
REQUEST_TIMEOUT = httpx.Timeout(5.0)
# One retry, after this delay, on a network error or a 5xx answer.
RETRY_DELAY_SECONDS = 30.0
_TOKEN_TIMEOUT = httpx.Timeout(10.0, connect=5.0)

# The MCP server entry whose URL and client-credentials identify the tenant
# connector. Same key the provisioner writes and the shared-room executor
# binds to; the websocket channel config may name a different one.
DEFAULT_CONNECTOR_SERVER = "ziggy-connectors"

# Reuse one client-credentials token fetch per configured server, matching
# the MCP client's token cache instead of re-minting per push.
_auth_cache: dict[tuple[str, ...], httpx.Auth] = {}


def _sleep(delay: float) -> Any:
    """Patchable indirection over ``asyncio.sleep`` (tests run the retry now)."""
    return asyncio.sleep(delay)


def _connector_server(cfg: Any) -> Any | None:
    """The configured connectors MCP server entry on *cfg*, or ``None``.

    *cfg* is anything shaped like the top-level ``Config`` (``tools.mcpServers``
    plus optional ``channels.websocket`` naming the connector server). The
    agent loop passes a view built from its ``tools_config``/``channels_config``.
    """
    tools = getattr(cfg, "tools", None)
    servers = getattr(tools, "mcp_servers", None) if tools is not None else None
    if not servers:
        return None
    name = ""
    channels = getattr(cfg, "channels", None)
    websocket = getattr(channels, "websocket", None) if channels is not None else None
    if isinstance(websocket, dict):
        name = str(
            websocket.get("sharedRoomConnectorServer")
            or websocket.get("shared_room_connector_server")
            or ""
        )
    else:
        name = str(getattr(websocket, "shared_room_connector_server", "") or "")
    if name and name in servers:
        return servers[name]
    return servers.get(DEFAULT_CONNECTOR_SERVER)


def _finished_url(server_url: str) -> str | None:
    """{scheme://host[:port]}/runtime/work/finished for the MCP server URL.

    The runtime-only listener serving ``/runtime/work/finished`` is the same
    origin the connectors MCP endpoint lives on.
    """
    try:
        parts = urllib.parse.urlsplit(server_url)
    except ValueError:
        return None
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        return None
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, FINISHED_PATH, "", ""))


def _auth_for(server: Any, token_transport: httpx.AsyncBaseTransport | None) -> httpx.Auth:
    """The connectors runtime bearer, minted/reused through the MCP client helper."""
    from nanobot.agent.tools.mcp_client_credentials import OAuthClientCredentialsAuth

    oauth = server.oauth_client_credentials
    if token_transport is not None:
        # Test seam: drive the real token flow over a mock transport.
        return OAuthClientCredentialsAuth(
            oauth,
            server.url,
            token_client_factory=lambda: httpx.AsyncClient(
                transport=token_transport, timeout=_TOKEN_TIMEOUT
            ),
        )
    from nanobot.agent.tools.mcp import _client_credentials_auth

    key = (
        server.url,
        oauth.token_url,
        oauth.client_id,
        oauth.client_secret_file,
        ",".join(oauth.scopes),
        oauth.resource or "",
    )
    auth = _auth_cache.get(key)
    if auth is None:
        auth = _client_credentials_auth(server)
        _auth_cache[key] = auth
    return auth


def _push_client(
    server: Any, auth: httpx.Auth, transport: httpx.AsyncBaseTransport | None
) -> httpx.AsyncClient:
    """A POST client shaped like the MCP client's own httpx stack."""
    from nanobot.agent.tools.mcp import (
        _mcp_request_validator,
        _operator_loopback_origins,
        _pinned_transport_kwargs,
    )

    kwargs: dict[str, Any] = {
        "auth": auth,
        "timeout": REQUEST_TIMEOUT,
        "follow_redirects": False,
    }
    if transport is not None:
        # Test seam: mock transport replaces the pinned stack for this call.
        kwargs["transport"] = transport
    else:
        loopback_origins = _operator_loopback_origins(server)
        kwargs["event_hooks"] = {"request": [_mcp_request_validator(loopback_origins)]}
        kwargs.update(_pinned_transport_kwargs(loopback_origins))
    return httpx.AsyncClient(**kwargs)


async def notify_finished(
    cfg: Any,
    task_id: str,
    title: str,
    status: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    token_transport: httpx.AsyncBaseTransport | None = None,
) -> bool:
    """Tell the tenant connector that hand-off task *task_id* ended with *status*.

    Returns True on a 2xx, False on any other outcome (including missing
    configuration). Never raises: the caller schedules this as a background
    task and a lost push must not touch the turn or the gateway.
    """
    if status not in PUSH_STATUSES:
        logger.warning(
            "work.finished push skipped for task {} (error_class=invalid_status, status={!r})",
            task_id,
            status,
        )
        return False
    try:
        server = _connector_server(cfg)
        if server is None or not server.url or server.oauth_client_credentials is None:
            logger.warning(
                "work.finished push skipped for task {} (error_class=no_connectors_server): "
                "no tools.mcpServers entry with oauthClientCredentials",
                task_id,
            )
            return False
        url = _finished_url(server.url)
        if url is None:
            logger.warning(
                "work.finished push skipped for task {} (error_class=bad_server_url)",
                task_id,
            )
            return False
        auth = _auth_for(server, token_transport)
        body = {"task_id": task_id, "title": title, "status": status}
        for attempt in (0, 1):
            retry_reason: str | None = None
            try:
                async with _push_client(server, auth, transport) as client:
                    response = await client.post(url, json=body)
            except httpx.HTTPError as exc:
                retry_reason = f"network error ({type(exc).__name__})"
            else:
                if 200 <= response.status_code < 300:
                    return True
                if response.status_code >= 500:
                    retry_reason = f"HTTP {response.status_code}"
                else:
                    logger.warning(
                        "work.finished push rejected for task {} "
                        "(error_class=push_rejected, status={})",
                        task_id,
                        response.status_code,
                    )
                    return False
            if attempt == 0 and retry_reason is not None:
                logger.info(
                    "work.finished push for task {}: {}; retrying in {}s",
                    task_id,
                    retry_reason,
                    int(RETRY_DELAY_SECONDS),
                )
                await _sleep(RETRY_DELAY_SECONDS)
                continue
            logger.warning(
                "work.finished push failed for task {} (error_class={}, retry=given)",
                task_id,
                retry_reason or "unknown",
            )
            return False
        return False
    except Exception as exc:
        logger.warning(
            "work.finished push failed for task {} (error_class={})",
            task_id,
            type(exc).__name__,
        )
        return False
