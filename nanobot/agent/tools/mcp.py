"""MCP client: connects to MCP servers and wraps their tools as native nanobot tools."""

import asyncio
import os
import re
import shutil
import time
from contextlib import AsyncExitStack, suppress
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin

import httpx
from loguru import logger

from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.registry import ToolRegistry

# Transient connection errors that warrant a single retry.
# These typically happen when an MCP server restarts or a network
# connection is interrupted between calls.
_TRANSIENT_EXC_NAMES: frozenset[str] = frozenset((
    "ClosedResourceError",
    "BrokenResourceError",
    "EndOfStream",
    "BrokenPipeError",
    "ConnectionResetError",
    "ConnectionRefusedError",
    "ConnectionAbortedError",
    "ConnectionError",
))

_WINDOWS_SHELL_LAUNCHERS: frozenset[str] = frozenset(("npx", "npm", "pnpm", "yarn", "bunx"))

# Characters allowed in tool names by model providers (Anthropic, OpenAI, etc.).
# Replace anything outside [a-zA-Z0-9_-] with underscore and collapse runs.
_SANITIZE_RE = re.compile(r"_+")


class _OAuthTokenStorage:
    """In-memory token storage for the MCP SDK's official OAuth provider."""

    def __init__(self) -> None:
        self._tokens = None
        self._client_info = None

    async def get_tokens(self):
        return self._tokens

    async def set_tokens(self, tokens) -> None:
        self._tokens = tokens

    async def get_client_info(self):
        return self._client_info

    async def set_client_info(self, client_info) -> None:
        self._client_info = client_info


def _read_oauth_client_secret(secret_file: str) -> str:
    """Read the OAuth secret without exposing its contents to logs or errors."""
    secret = Path(secret_file).read_text(encoding="utf-8").strip()
    if not secret:
        raise ValueError("OAuth client secret file is empty")
    return secret


def _build_official_oauth_provider(config, server_url: str):
    """Build the SDK provider when the pinned SDK exposes it."""
    # The SDK extension currently has no token-endpoint parameter and performs
    # its first grant against the inferred root /token path. Use the local
    # provider whenever config declares a different endpoint.
    if config.token_url != urljoin(server_url, "/token"):
        return None
    try:
        from mcp.client.auth.extensions.client_credentials import ClientCredentialsOAuthProvider
    except ImportError:
        return None

    secret = _read_oauth_client_secret(config.client_secret_file)
    try:
        return ClientCredentialsOAuthProvider(
            server_url=server_url,
            storage=_OAuthTokenStorage(),
            client_id=config.client_id,
            client_secret=secret,
            scopes=" ".join(config.scopes) or None,
        )
    except TypeError:
        return None


class OAuthClientCredentialsAuth(httpx.Auth):
    """Async OAuth 2.0 client-credentials auth for MCP HTTP transports."""

    _REFRESH_WINDOW_SECONDS = 60.0

    def __init__(
        self,
        config,
        resource_url: str,
        *,
        token_client_factory: Callable[[], httpx.AsyncClient] | None = None,
    ) -> None:
        self._token_url = config.token_url
        self._client_id = config.client_id
        self._client_secret_file = config.client_secret_file
        self._scopes = tuple(config.scopes)
        self._resource_url = resource_url
        self._token_client_factory = token_client_factory or httpx.AsyncClient
        self._access_token: str | None = None
        self._token_valid_until = 0.0
        self._refresh_lock = asyncio.Lock()

    async def _get_access_token(self, rejected_token: str | None = None) -> str:
        """Return a cached token or serialize a refresh through the token endpoint."""
        async with self._refresh_lock:
            now = time.monotonic()
            if (
                self._access_token
                and self._access_token != rejected_token
                and now < self._token_valid_until
            ):
                return self._access_token

            if rejected_token is not None and self._access_token == rejected_token:
                self._access_token = None
                self._token_valid_until = 0.0

            secret = _read_oauth_client_secret(self._client_secret_file)

            form = {
                "grant_type": "client_credentials",
                "resource": self._resource_url,
            }
            if self._scopes:
                form["scope"] = " ".join(self._scopes)

            async with self._token_client_factory() as client:
                response = await client.post(
                    self._token_url,
                    data=form,
                    auth=httpx.BasicAuth(self._client_id, secret),
                )
                response.raise_for_status()
                payload = response.json()

            if not isinstance(payload, dict):
                raise ValueError("OAuth token response was not a JSON object")
            access_token = payload.get("access_token")
            if not isinstance(access_token, str) or not access_token:
                raise ValueError("OAuth token response did not contain an access_token")

            try:
                expires_in = float(payload.get("expires_in", 300))
            except (TypeError, ValueError) as exc:
                raise ValueError("OAuth token response contained an invalid expires_in") from exc

            self._access_token = access_token
            self._token_valid_until = time.monotonic() + max(
                0.0, expires_in - self._REFRESH_WINDOW_SECONDS
            )
            return access_token

    async def async_auth_flow(self, request):
        """Attach a bearer token and retry one time after an unauthorized response."""
        access_token = await self._get_access_token()
        request.headers["Authorization"] = f"Bearer {access_token}"
        response = yield request

        if response.status_code == 401:
            access_token = await self._get_access_token(rejected_token=access_token)
            request.headers["Authorization"] = f"Bearer {access_token}"
            yield request


def _sanitize_name(name: str) -> str:
    """Sanitize an MCP-derived name for model API compatibility."""
    return _SANITIZE_RE.sub("_", re.sub(r"[^a-zA-Z0-9_-]", "_", name))


def _is_transient(exc: BaseException) -> bool:
    """Check if an exception looks like a transient connection error."""
    return type(exc).__name__ in _TRANSIENT_EXC_NAMES


def _windows_command_basename(command: str) -> str:
    """Return the lowercase basename for a Windows command or path."""
    return command.replace("\\", "/").rsplit("/", maxsplit=1)[-1].lower()


def _normalize_windows_stdio_command(
    command: str,
    args: list[str] | None,
    env: dict[str, str] | None,
) -> tuple[str, list[str], dict[str, str] | None]:
    """Wrap Windows shell launchers so MCP stdio servers start reliably."""
    normalized_args = list(args or [])
    if os.name != "nt":
        return command, normalized_args, env

    basename = _windows_command_basename(command)
    if basename in {"cmd", "cmd.exe", "powershell", "powershell.exe", "pwsh", "pwsh.exe"}:
        return command, normalized_args, env

    if basename.endswith((".exe", ".com")):
        return command, normalized_args, env

    resolved = shutil.which(command, path=(env or {}).get("PATH")) or command
    resolved_basename = _windows_command_basename(resolved)
    should_wrap = (
        basename in _WINDOWS_SHELL_LAUNCHERS
        or basename.endswith((".cmd", ".bat"))
        or resolved_basename.endswith((".cmd", ".bat"))
    )
    if not should_wrap:
        return command, normalized_args, env

    comspec = (env or {}).get("COMSPEC") or os.environ.get("COMSPEC") or "cmd.exe"
    return comspec, ["/d", "/c", command, *normalized_args], env


def _extract_nullable_branch(options: Any) -> tuple[dict[str, Any], bool] | None:
    """Return the single non-null branch for nullable unions."""
    if not isinstance(options, list):
        return None

    non_null: list[dict[str, Any]] = []
    saw_null = False
    for option in options:
        if not isinstance(option, dict):
            return None
        if option.get("type") == "null":
            saw_null = True
            continue
        non_null.append(option)

    if saw_null and len(non_null) == 1:
        return non_null[0], True
    return None


def _normalize_schema_for_openai(schema: Any) -> dict[str, Any]:
    """Normalize only nullable JSON Schema patterns for tool definitions."""
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}}

    normalized = dict(schema)

    raw_type = normalized.get("type")
    if isinstance(raw_type, list):
        non_null = [item for item in raw_type if item != "null"]
        if "null" in raw_type and len(non_null) == 1:
            normalized["type"] = non_null[0]
            normalized["nullable"] = True

    for key in ("oneOf", "anyOf"):
        nullable_branch = _extract_nullable_branch(normalized.get(key))
        if nullable_branch is not None:
            branch, _ = nullable_branch
            merged = {k: v for k, v in normalized.items() if k != key}
            merged.update(branch)
            normalized = merged
            normalized["nullable"] = True
            break

    if "properties" in normalized and isinstance(normalized["properties"], dict):
        normalized["properties"] = {
            name: _normalize_schema_for_openai(prop) if isinstance(prop, dict) else prop
            for name, prop in normalized["properties"].items()
        }

    if "items" in normalized and isinstance(normalized["items"], dict):
        normalized["items"] = _normalize_schema_for_openai(normalized["items"])

    if normalized.get("type") != "object":
        return normalized

    normalized.setdefault("properties", {})
    normalized.setdefault("required", [])
    return normalized


class MCPToolWrapper(Tool):
    """Wraps a single MCP server tool as a nanobot Tool."""

    def __init__(self, session, server_name: str, tool_def, tool_timeout: int = 30):
        self._session = session
        self._original_name = tool_def.name
        self._name = _sanitize_name(f"mcp_{server_name}_{tool_def.name}")
        self._description = tool_def.description or tool_def.name
        raw_schema = tool_def.inputSchema or {"type": "object", "properties": {}}
        self._parameters = _normalize_schema_for_openai(raw_schema)
        self._tool_timeout = tool_timeout

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._description

    @property
    def parameters(self) -> dict[str, Any]:
        return self._parameters

    async def execute(self, **kwargs: Any) -> str:
        from mcp import types

        for attempt in range(2):  # At most 1 retry
            try:
                result = await asyncio.wait_for(
                    self._session.call_tool(self._original_name, arguments=kwargs),
                    timeout=self._tool_timeout,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "MCP tool '{}' timed out after {}s", self._name, self._tool_timeout
                )
                return f"(MCP tool call timed out after {self._tool_timeout}s)"
            except asyncio.CancelledError:
                # MCP SDK's anyio cancel scopes can leak CancelledError on timeout/failure.
                # Re-raise only if our task was externally cancelled (e.g. /stop).
                task = asyncio.current_task()
                if task is not None and task.cancelling() > 0:
                    raise
                logger.warning("MCP tool '{}' was cancelled by server/SDK", self._name)
                return "(MCP tool call was cancelled)"
            except Exception as exc:
                if _is_transient(exc):
                    if attempt == 0:
                        logger.warning(
                            "MCP tool '{}' hit transient error ({}), retrying once...",
                            self._name,
                            type(exc).__name__,
                        )
                        await asyncio.sleep(1)  # Brief backoff before retry
                        continue
                    # Second transient failure — give up with retry-specific message
                    logger.error(
                        "MCP tool '{}' failed after retry: {}: {}",
                        self._name,
                        type(exc).__name__,
                        exc,
                    )
                    return f"(MCP tool call failed after retry: {type(exc).__name__})"
                logger.exception(
                    "MCP tool '{}' failed: {}: {}",
                    self._name,
                    type(exc).__name__,
                    exc,
                )
                return f"(MCP tool call failed: {type(exc).__name__})"
            else:
                # Success — extract result
                parts = []
                for block in result.content:
                    if isinstance(block, types.TextContent):
                        parts.append(block.text)
                    else:
                        parts.append(str(block))
                return "\n".join(parts) or "(no output)"

        return "(MCP tool call failed)"  # Unreachable, but satisfies type checkers


class MCPResourceWrapper(Tool):
    """Wraps an MCP resource URI as a read-only nanobot Tool."""

    def __init__(self, session, server_name: str, resource_def, resource_timeout: int = 30):
        self._session = session
        self._uri = resource_def.uri
        self._name = _sanitize_name(f"mcp_{server_name}_resource_{resource_def.name}")
        desc = resource_def.description or resource_def.name
        self._description = f"[MCP Resource] {desc}\nURI: {self._uri}"
        self._parameters: dict[str, Any] = {
            "type": "object",
            "properties": {},
            "required": [],
        }
        self._resource_timeout = resource_timeout

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._description

    @property
    def parameters(self) -> dict[str, Any]:
        return self._parameters

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, **kwargs: Any) -> str:
        from mcp import types

        for attempt in range(2):
            try:
                result = await asyncio.wait_for(
                    self._session.read_resource(self._uri),
                    timeout=self._resource_timeout,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "MCP resource '{}' timed out after {}s", self._name, self._resource_timeout
                )
                return f"(MCP resource read timed out after {self._resource_timeout}s)"
            except asyncio.CancelledError:
                task = asyncio.current_task()
                if task is not None and task.cancelling() > 0:
                    raise
                logger.warning("MCP resource '{}' was cancelled by server/SDK", self._name)
                return "(MCP resource read was cancelled)"
            except Exception as exc:
                if _is_transient(exc):
                    if attempt == 0:
                        logger.warning(
                            "MCP resource '{}' hit transient error ({}), retrying once...",
                            self._name,
                            type(exc).__name__,
                        )
                        await asyncio.sleep(1)
                        continue
                    logger.error(
                        "MCP resource '{}' failed after retry: {}: {}",
                        self._name,
                        type(exc).__name__,
                        exc,
                    )
                    return f"(MCP resource read failed after retry: {type(exc).__name__})"
                logger.exception(
                    "MCP resource '{}' failed: {}: {}",
                    self._name,
                    type(exc).__name__,
                    exc,
                )
                return f"(MCP resource read failed: {type(exc).__name__})"
            else:
                parts: list[str] = []
                for block in result.contents:
                    if isinstance(block, types.TextResourceContents):
                        parts.append(block.text)
                    elif isinstance(block, types.BlobResourceContents):
                        parts.append(f"[Binary resource: {len(block.blob)} bytes]")
                    else:
                        parts.append(str(block))
                return "\n".join(parts) or "(no output)"

        return "(MCP resource read failed)"  # Unreachable


class MCPPromptWrapper(Tool):
    """Wraps an MCP prompt as a read-only nanobot Tool."""

    def __init__(self, session, server_name: str, prompt_def, prompt_timeout: int = 30):
        self._session = session
        self._prompt_name = prompt_def.name
        self._name = _sanitize_name(f"mcp_{server_name}_prompt_{prompt_def.name}")
        desc = prompt_def.description or prompt_def.name
        self._description = (
            f"[MCP Prompt] {desc}\n"
            "Returns a filled prompt template that can be used as a workflow guide."
        )
        self._prompt_timeout = prompt_timeout

        # Build parameters from prompt arguments
        properties: dict[str, Any] = {}
        required: list[str] = []
        for arg in prompt_def.arguments or []:
            prop: dict[str, Any] = {"type": "string"}
            if getattr(arg, "description", None):
                prop["description"] = arg.description
            properties[arg.name] = prop
            if arg.required:
                required.append(arg.name)
        self._parameters: dict[str, Any] = {
            "type": "object",
            "properties": properties,
            "required": required,
        }

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._description

    @property
    def parameters(self) -> dict[str, Any]:
        return self._parameters

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, **kwargs: Any) -> str:
        from mcp import types
        from mcp.shared.exceptions import McpError

        for attempt in range(2):
            try:
                result = await asyncio.wait_for(
                    self._session.get_prompt(self._prompt_name, arguments=kwargs),
                    timeout=self._prompt_timeout,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "MCP prompt '{}' timed out after {}s", self._name, self._prompt_timeout
                )
                return f"(MCP prompt call timed out after {self._prompt_timeout}s)"
            except asyncio.CancelledError:
                task = asyncio.current_task()
                if task is not None and task.cancelling() > 0:
                    raise
                logger.warning("MCP prompt '{}' was cancelled by server/SDK", self._name)
                return "(MCP prompt call was cancelled)"
            except McpError as exc:
                logger.error(
                    "MCP prompt '{}' failed: code={} message={}",
                    self._name,
                    exc.error.code,
                    exc.error.message,
                )
                return f"(MCP prompt call failed: {exc.error.message} [code {exc.error.code}])"
            except Exception as exc:
                if _is_transient(exc):
                    if attempt == 0:
                        logger.warning(
                            "MCP prompt '{}' hit transient error ({}), retrying once...",
                            self._name,
                            type(exc).__name__,
                        )
                        await asyncio.sleep(1)
                        continue
                    logger.error(
                        "MCP prompt '{}' failed after retry: {}: {}",
                        self._name,
                        type(exc).__name__,
                        exc,
                    )
                    return f"(MCP prompt call failed after retry: {type(exc).__name__})"
                logger.exception(
                    "MCP prompt '{}' failed: {}: {}",
                    self._name,
                    type(exc).__name__,
                    exc,
                )
                return f"(MCP prompt call failed: {type(exc).__name__})"
            else:
                parts: list[str] = []
                for message in result.messages:
                    content = message.content
                    if isinstance(content, types.TextContent):
                        parts.append(content.text)
                    elif isinstance(content, list):
                        for block in content:
                            if isinstance(block, types.TextContent):
                                parts.append(block.text)
                            else:
                                parts.append(str(block))
                    else:
                        parts.append(str(content))
                return "\n".join(parts) or "(no output)"

        return "(MCP prompt call failed)"  # Unreachable


async def connect_mcp_servers(
    mcp_servers: dict, registry: ToolRegistry
) -> dict[str, AsyncExitStack]:
    """Connect to configured MCP servers and register their tools, resources, prompts.

    Returns a dict mapping server name -> its dedicated AsyncExitStack.
    Each server gets its own stack and runs in its own task to prevent
    cancel scope conflicts when multiple MCP servers are configured.
    """
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.sse import sse_client
    from mcp.client.stdio import stdio_client
    from mcp.client.streamable_http import streamable_http_client

    async def connect_single_server(name: str, cfg) -> tuple[str, AsyncExitStack | None]:
        server_stack = AsyncExitStack()
        await server_stack.__aenter__()

        try:
            oauth_auth = None
            oauth_provider = None
            oauth_config = cfg.oauth_client_credentials
            if oauth_config is not None:
                if any(header.lower() == "authorization" for header in (cfg.headers or {})):
                    raise ValueError(
                        "headers.Authorization cannot be configured with oauthClientCredentials"
                    )

            transport_type = cfg.type
            if not transport_type:
                if cfg.command:
                    transport_type = "stdio"
                elif cfg.url:
                    transport_type = (
                        "sse" if cfg.url.rstrip("/").endswith("/sse") else "streamableHttp"
                    )
                else:
                    logger.warning("MCP server '{}': no command or url configured, skipping", name)
                    await server_stack.aclose()
                    return name, None

            if oauth_config is not None:
                if transport_type in {"sse", "streamableHttp"}:
                    oauth_provider = _build_official_oauth_provider(oauth_config, cfg.url)
                if oauth_provider is None:
                    oauth_auth = OAuthClientCredentialsAuth(oauth_config, cfg.url)

            if transport_type == "stdio":
                command, args, env = _normalize_windows_stdio_command(
                    cfg.command,
                    cfg.args,
                    cfg.env or None,
                )
                params = StdioServerParameters(
                    command=command,
                    args=args,
                    env=env,
                )
                read, write = await server_stack.enter_async_context(stdio_client(params))
            elif transport_type == "sse":

                def httpx_client_factory(
                    headers: dict[str, str] | None = None,
                    timeout: httpx.Timeout | None = None,
                    auth: httpx.Auth | None = None,
                ) -> httpx.AsyncClient:
                    merged_headers = {
                        "Accept": "application/json, text/event-stream",
                        **(cfg.headers or {}),
                        **(headers or {}),
                    }
                    return httpx.AsyncClient(
                        headers=merged_headers or None,
                        follow_redirects=True,
                        timeout=timeout,
                        auth=auth,
                    )

                sse_kwargs = {"httpx_client_factory": httpx_client_factory}
                if oauth_provider is not None:
                    sse_kwargs["auth"] = oauth_provider
                elif oauth_auth is not None:
                    sse_kwargs["auth"] = oauth_auth
                read, write = await server_stack.enter_async_context(sse_client(cfg.url, **sse_kwargs))
            elif transport_type == "streamableHttp":
                http_client = await server_stack.enter_async_context(
                    httpx.AsyncClient(
                        headers=cfg.headers or None,
                        follow_redirects=True,
                        timeout=None,
                        auth=oauth_provider or oauth_auth,
                    )
                )
                read, write, _ = await server_stack.enter_async_context(
                    streamable_http_client(cfg.url, http_client=http_client)
                )
            else:
                logger.warning("MCP server '{}': unknown transport type '{}'", name, transport_type)
                await server_stack.aclose()
                return name, None

            session = await server_stack.enter_async_context(ClientSession(read, write))
            await session.initialize()

            tools = await session.list_tools()
            enabled_tools = set(cfg.enabled_tools)
            allow_all_tools = "*" in enabled_tools
            registered_count = 0
            matched_enabled_tools: set[str] = set()
            available_raw_names = [tool_def.name for tool_def in tools.tools]
            available_wrapped_names = [_sanitize_name(f"mcp_{name}_{tool_def.name}") for tool_def in tools.tools]
            for tool_def in tools.tools:
                wrapped_name = _sanitize_name(f"mcp_{name}_{tool_def.name}")
                if (
                    not allow_all_tools
                    and tool_def.name not in enabled_tools
                    and wrapped_name not in enabled_tools
                ):
                    logger.debug(
                        "MCP: skipping tool '{}' from server '{}' (not in enabledTools)",
                        wrapped_name,
                        name,
                    )
                    continue
                wrapper = MCPToolWrapper(session, name, tool_def, tool_timeout=cfg.tool_timeout)
                registry.register(wrapper)
                logger.debug("MCP: registered tool '{}' from server '{}'", wrapper.name, name)
                registered_count += 1
                if enabled_tools:
                    if tool_def.name in enabled_tools:
                        matched_enabled_tools.add(tool_def.name)
                    if wrapped_name in enabled_tools:
                        matched_enabled_tools.add(wrapped_name)

            if enabled_tools and not allow_all_tools:
                unmatched_enabled_tools = sorted(enabled_tools - matched_enabled_tools)
                if unmatched_enabled_tools:
                    logger.warning(
                        "MCP server '{}': enabledTools entries not found: {}. Available raw names: {}. "
                        "Available wrapped names: {}",
                        name,
                        ", ".join(unmatched_enabled_tools),
                        ", ".join(available_raw_names) or "(none)",
                        ", ".join(available_wrapped_names) or "(none)",
                    )

            try:
                resources_result = await session.list_resources()
                for resource in resources_result.resources:
                    wrapper = MCPResourceWrapper(
                        session, name, resource, resource_timeout=cfg.tool_timeout
                    )
                    registry.register(wrapper)
                    registered_count += 1
                    logger.debug(
                        "MCP: registered resource '{}' from server '{}'", wrapper.name, name
                    )
            except Exception as e:
                logger.debug("MCP server '{}': resources not supported or failed: {}", name, e)

            try:
                prompts_result = await session.list_prompts()
                for prompt in prompts_result.prompts:
                    wrapper = MCPPromptWrapper(
                        session, name, prompt, prompt_timeout=cfg.tool_timeout
                    )
                    registry.register(wrapper)
                    registered_count += 1
                    logger.debug("MCP: registered prompt '{}' from server '{}'", wrapper.name, name)
            except Exception as e:
                logger.debug("MCP server '{}': prompts not supported or failed: {}", name, e)

            logger.info(
                "MCP server '{}': connected, {} capabilities registered", name, registered_count
            )
            return name, server_stack

        except Exception as e:
            hint = ""
            text = str(e).lower()
            if any(
                marker in text
                for marker in (
                    "parse error",
                    "invalid json",
                    "unexpected token",
                    "jsonrpc",
                    "content-length",
                )
            ):
                hint = (
                    " Hint: this looks like stdio protocol pollution. Make sure the MCP server writes "
                    "only JSON-RPC to stdout and sends logs/debug output to stderr instead."
                )
            logger.error("MCP server '{}': failed to connect: {}{}", name, e, hint)
            with suppress(Exception):
                await server_stack.aclose()
            return name, None

    server_stacks: dict[str, AsyncExitStack] = {}

    tasks: list[asyncio.Task] = []
    for name, cfg in mcp_servers.items():
        task = asyncio.create_task(connect_single_server(name, cfg))
        tasks.append(task)

    results = await asyncio.gather(*tasks, return_exceptions=True)

    for i, result in enumerate(results):
        name = list(mcp_servers.keys())[i]
        if isinstance(result, BaseException):
            if not isinstance(result, asyncio.CancelledError):
                logger.error("MCP server '{}' connection task failed: {}", name, result)
        elif result is not None and result[1] is not None:
            server_stacks[result[0]] = result[1]

    return server_stacks
