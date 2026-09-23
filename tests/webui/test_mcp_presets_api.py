from __future__ import annotations

import asyncio
import base64
import json
import threading
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from loguru import logger
from mcp.shared.auth import OAuthToken

from nanobot.agent.plugins import AGENT_PLUGIN_MCP_SCHEMA, AGENT_PLUGIN_SCHEMA
from nanobot.agent.tools.mcp_oauth import MCPOAuthStorage, mcp_oauth_has_credentials
from nanobot.config.loader import load_config, save_config
from nanobot.config.schema import Config
from nanobot.webui.mcp_presets_api import (
    McpPresetError,
    custom_mcp_action,
    mcp_presets_action,
    mcp_presets_payload,
    mcp_presets_settings_action,
    mcp_presets_test_action,
    normalize_mcp_preset_mentions,
)


def _use_config(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"agents": {"defaults": {"workspace": str(tmp_path / "workspace")}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr("nanobot.config.loader._current_config_path", config_path)


def _write_agent_plugin(workspace: Path) -> None:
    root = workspace / "plugins" / "desktop"
    root.mkdir(parents=True)
    for filename, payload in (
        (
            "plugin.json",
            {
                "$schema": AGENT_PLUGIN_SCHEMA,
                "name": "desktop",
                "description": "Control the local desktop.",
                "extensions": {
                    "dev.nanobot": {
                        "displayName": "Desktop Control",
                        "permissions": ["screen-recording"],
                    }
                },
            },
        ),
        (
            "mcp.json",
            {
                "$schema": AGENT_PLUGIN_MCP_SCHEMA,
                "mcpServers": {"desktop": {"type": "stdio", "command": "echo"}},
            },
        ),
    ):
        (root / filename).write_text(json.dumps(payload), encoding="utf-8")


def test_mcp_presets_payload_lists_supported_cards(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    _use_config(tmp_path, monkeypatch)

    payload = mcp_presets_payload()
    names = {preset["name"] for preset in payload["presets"]}

    assert {
        "browserbase",
        "playwright",
        "github",
        "figma",
        "context7",
        "firecrawl",
        "parallel-search",
        "exa",
        "microsoft-learn",
        "aws-docs",
        "brave-search",
        "postman",
        "xmind",
        "notion",
        "linear",
    }.issubset(names)
    browserbase = next(preset for preset in payload["presets"] if preset["name"] == "browserbase")
    assert browserbase["installed"] is False
    assert browserbase["install_supported"] is True
    assert browserbase["required_fields"][0]["configured"] is False
    assert "browserbaseApiKey" not in browserbase["connection_summary"]
    manifest = browserbase["manifest"]
    assert manifest["schema"] == "agent-app.v1"
    assert manifest["id"] == "browserbase"
    assert manifest["source"] == "mcp-preset"
    assert manifest["capabilities"][0]["type"] == "mcp"
    assert manifest["capabilities"][0]["transport"] == "streamableHttp"
    assert manifest["install"]["strategy"] == "config"
    assert manifest["remove"]["verification"] == ["config_absent"]
    assert manifest["trust"]["review_status"] == "builtin_preset"


def test_agent_plugin_reuses_mcp_catalog_and_runtime_action(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_config(tmp_path, monkeypatch)
    _write_agent_plugin(load_config().workspace_path)

    row = next(item for item in mcp_presets_payload()["presets"] if item["source"] == "agent-plugin")
    assert (row["name"], row["display_name"], row["requires"]) == (
        "plugin-desktop", "Desktop Control", "screen-recording"
    )
    assert row["installed"] and row["configured"] and not row["enabled"]

    async def reload() -> dict[str, object]:
        return {"ok": True, "message": "MCP reloaded.", "requires_restart": False}

    plugin_action = partial(
        mcp_presets_settings_action,
        query={"name": ["plugin-desktop"]},
    )
    enabled = asyncio.run(plugin_action("enable", reload_mcp=reload))
    enabled_row = next(item for item in enabled["presets"] if item["name"] == "plugin-desktop")
    assert (enabled_row["enabled"], enabled_row["status"], enabled["requires_restart"]) == (
        True, "enabled", False
    )

    disabled = asyncio.run(plugin_action("disable", reload_mcp=reload))
    disabled_row = next(item for item in disabled["presets"] if item["name"] == "plugin-desktop")
    assert (disabled_row["installed"], disabled_row["enabled"], disabled_row["status"]) == (
        True, False, "disabled"
    )

    with pytest.raises(McpPresetError, match="enable and disable"):
        asyncio.run(plugin_action("remove"))

    (load_config().workspace_path / "plugins" / "desktop" / "mcp.json").unlink()
    assert any(item["name"] == "plugin-desktop" for item in mcp_presets_payload()["presets"])

    config_path = tmp_path / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["tools"] = {"mcpServers": {"plugin-desktop": {"type": "stdio", "command": "echo"}}}
    config_path.write_text(json.dumps(config), encoding="utf-8")
    rows = [item for item in mcp_presets_payload()["presets"] if item["name"] == "plugin-desktop"]
    assert len(rows) == 1 and rows[0]["source"] == "custom"


@pytest.mark.asyncio
async def test_oauth_preset_is_one_click_configured_after_token_storage(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_config(tmp_path, monkeypatch)

    payload = mcp_presets_action("enable", {"name": ["xmind"]})

    row = next(item for item in payload["presets"] if item["name"] == "xmind")
    assert row["installed"] is True
    assert row["configured"] is False
    assert row["status"] == "authorization_required"
    assert row["transport"] == "streamableHttp"
    assert row["auth"] == "oauth"
    config = load_config()
    cfg = config.tools.mcp_servers["xmind"]
    assert cfg.type == "streamableHttp"
    assert cfg.auth == "oauth"
    assert cfg.url == "https://app.xmind.com/api/mcp"

    await MCPOAuthStorage("xmind", cfg.url).set_tokens(OAuthToken(access_token="secret"))
    connected = mcp_presets_payload()
    row = next(item for item in connected["presets"] if item["name"] == "xmind")
    assert row["configured"] is True
    assert row["status"] == "configured"

    failed = mcp_presets_payload(runtime_status={"xmind": "failed"})
    row = next(item for item in failed["presets"] if item["name"] == "xmind")
    assert row["configured"] is True
    assert row["status"] == "configured"
    assert row["runtime_status"] == "failed"
    assert "secret" not in str(row)

    healthy = mcp_presets_payload(runtime_status={"xmind": "connected"})
    row = next(item for item in healthy["presets"] if item["name"] == "xmind")
    assert row["runtime_status"] == "connected"

    await MCPOAuthStorage("xmind", cfg.url).clear_tokens()
    refresh_failed = mcp_presets_payload(runtime_status={"xmind": "failed"})
    row = next(item for item in refresh_failed["presets"] if item["name"] == "xmind")
    assert row["configured"] is False
    assert row["status"] == "authorization_required"
    assert row["runtime_status"] == "failed"

    stale_connected = mcp_presets_payload(runtime_status={"xmind": "connected"})
    row = next(item for item in stale_connected["presets"] if item["name"] == "xmind")
    assert "runtime_status" not in row

    mcp_presets_action("remove", {"name": ["xmind"]})
    assert await MCPOAuthStorage("xmind", cfg.url).get_tokens() is None


@pytest.mark.asyncio
async def test_settings_list_projects_runtime_snapshot_and_reconnects_custom_server(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_config(tmp_path, monkeypatch)
    custom_mcp_action(
        "custom",
        {
            "name": ["team-docs"],
            "transport": ["streamableHttp"],
            "url": ["https://mcp.example.com/mcp"],
        },
    )
    statuses = {"team-docs": "failed"}
    reload_calls = 0

    async def reload_mcp() -> dict[str, object]:
        nonlocal reload_calls
        reload_calls += 1
        statuses["team-docs"] = "connected"
        return {
            "ok": True,
            "connected": ["team-docs"],
            "failed": [],
            "requires_restart": False,
        }

    listed = await mcp_presets_settings_action(
        None,
        {},
        reload_mcp=reload_mcp,
        mcp_runtime_status=lambda: statuses,
    )
    row = next(item for item in listed["presets"] if item["name"] == "team-docs")
    assert row["configured"] is True
    assert row["runtime_status"] == "failed"
    assert reload_calls == 0

    reconnected = await mcp_presets_settings_action(
        "reconnect",
        {"name": ["team-docs"]},
        reload_mcp=reload_mcp,
        mcp_runtime_status=lambda: statuses,
    )
    row = next(item for item in reconnected["presets"] if item["name"] == "team-docs")
    assert row["runtime_status"] == "connected"
    assert reconnected["requires_restart"] is False
    assert reload_calls == 1


def test_enable_browserbase_writes_scrubbed_config_payload(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_config(tmp_path, monkeypatch)

    payload = mcp_presets_action(
        "enable",
        {
            "name": ["browserbase"],
            "browserbase_api_key": ["bb_live_secret"],
        },
    )

    assert payload["requires_restart"] is True
    assert payload["last_action"]["ok"] is True
    assert payload["last_action"]["installed"] is True
    assert payload["last_action"]["verification"] == ["config_present"]
    preset = next(row for row in payload["presets"] if row["name"] == "browserbase")
    assert preset["installed"] is True
    assert preset["configured"] is True
    assert "bb_live_secret" not in str(payload)
    config = load_config()
    assert "browserbaseApiKey=bb_live_secret" in config.tools.mcp_servers["browserbase"].url


def test_enable_requires_missing_secret(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    _use_config(tmp_path, monkeypatch)

    with pytest.raises(McpPresetError) as exc:
        mcp_presets_action("enable", {"name": ["browserbase"]})

    assert exc.value.status == 400
    assert "Browserbase API key" in exc.value.message


def test_enable_context7_optional_api_key_appends_arg(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_config(tmp_path, monkeypatch)

    payload = mcp_presets_action(
        "enable",
        {
            "name": ["context7"],
            "context7_api_key": ["ctx7_secret"],
        },
    )

    assert "ctx7_secret" not in str(payload)
    row = next(item for item in payload["presets"] if item["name"] == "context7")
    assert row["configured"] is True
    config = load_config()
    assert config.tools.mcp_servers["context7"].args == [
        "-y",
        "@upstash/context7-mcp@latest",
        "--api-key",
        "ctx7_secret",
    ]


def test_enable_stdio_preset_uses_config_scoped_cwd(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_config(tmp_path, monkeypatch)

    mcp_presets_action("enable", {"name": ["playwright"]})

    config = load_config()
    cwd = config.tools.mcp_servers["playwright"].cwd
    assert cwd == str(tmp_path / "mcp" / "playwright")
    assert (tmp_path / "mcp" / "playwright").is_dir()


def test_enable_no_auth_remote_presets_write_url(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    _use_config(tmp_path, monkeypatch)

    mcp_presets_action("enable", {"name": ["microsoft-learn"]})
    mcp_presets_action("enable", {"name": ["exa"]})
    mcp_presets_action("enable", {"name": ["firecrawl"]})
    mcp_presets_action("enable", {"name": ["parallel-search"]})

    config = load_config()
    assert config.tools.mcp_servers["microsoft-learn"].url == "https://learn.microsoft.com/api/mcp"
    assert config.tools.mcp_servers["exa"].url == "https://mcp.exa.ai/mcp"
    assert config.tools.mcp_servers["firecrawl"].url == "https://mcp.firecrawl.dev/v2/mcp"
    assert config.tools.mcp_servers["parallel-search"].url == "https://search.parallel.ai/mcp"


def test_firecrawl_preset_is_keyless(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    _use_config(tmp_path, monkeypatch)

    payload = mcp_presets_action("enable", {"name": ["firecrawl"]})

    row = next(item for item in payload["presets"] if item["name"] == "firecrawl")
    assert row["transport"] == "streamableHttp"
    assert row["requires"] == "Network access"
    assert row["required_fields"] == []
    assert row["configured"] is True
    assert "Keyless" in row["note"]
    config = load_config()
    assert config.tools.mcp_servers["firecrawl"].type == "streamableHttp"
    assert config.tools.mcp_servers["firecrawl"].url == "https://mcp.firecrawl.dev/v2/mcp"
    assert config.tools.mcp_servers["firecrawl"].env == {}


def test_parallel_search_preset_is_keyless_and_tool_limited(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_config(tmp_path, monkeypatch)

    payload = mcp_presets_action("enable", {"name": ["parallel-search"]})

    row = next(item for item in payload["presets"] if item["name"] == "parallel-search")
    assert row["transport"] == "streamableHttp"
    assert row["requires"] == "Network access"
    assert row["required_fields"] == []
    assert row["configured"] is True
    assert "no API key" in row["note"]
    config = load_config()
    server = config.tools.mcp_servers["parallel-search"]
    assert server.type == "streamableHttp"
    assert server.url == "https://search.parallel.ai/mcp"
    assert server.enabled_tools == ["web_search", "web_fetch"]
    assert server.headers == {}


def test_remove_mcp_preset_updates_config(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    _use_config(tmp_path, monkeypatch)
    mcp_presets_action("enable", {"name": ["playwright"]})
    managed_cwd = tmp_path / "mcp" / "playwright"
    (managed_cwd / "cache.txt").write_text("managed runtime data", encoding="utf-8")

    payload = mcp_presets_action("remove", {"name": ["playwright"]})

    assert payload["requires_restart"] is True
    assert payload["last_action"]["ok"] is True
    assert payload["last_action"]["removed"] is True
    assert payload["last_action"]["managed_paths_removed"] == ["runtime:mcp/playwright"]
    assert not managed_cwd.exists()
    config = load_config()
    assert "playwright" not in config.tools.mcp_servers


def test_remove_custom_mcp_server_preserves_user_cwd(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    _use_config(tmp_path, monkeypatch)
    user_cwd = tmp_path / "user-cwd"
    user_cwd.mkdir()
    custom_mcp_action(
        "custom",
        {
            "name": ["internal-docs"],
            "transport": ["stdio"],
            "command": ["node"],
            "args": ['["server.js"]'],
            "cwd": [str(user_cwd)],
        },
    )

    payload = mcp_presets_action("remove", {"name": ["internal-docs"]})

    assert payload["last_action"]["ok"] is True
    assert user_cwd.exists()
    config = load_config()
    assert "internal-docs" not in config.tools.mcp_servers


def test_test_mcp_preset_reports_missing_dependency(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_config(tmp_path, monkeypatch)
    mcp_presets_action("enable", {"name": ["playwright"]})
    monkeypatch.setattr("nanobot.webui.mcp_presets_api.shutil.which", lambda _command: None)

    payload = asyncio.run(mcp_presets_test_action({"name": ["playwright"]}))

    assert payload["last_action"]["ok"] is False
    assert "npx" in payload["last_action"]["message"]


def test_test_mcp_preset_connects_and_reports_tools(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_config(tmp_path, monkeypatch)
    mcp_presets_action("enable", {"name": ["playwright"]})

    class FakeStack:
        async def aclose(self) -> None:
            return None

    async def fake_connect(servers, registry):
        assert list(servers) == ["playwright"]

        class FakeTool:
            name = "mcp_playwright_browser_navigate"

            def to_schema(self):
                return {"name": self.name, "description": "", "parameters": {}}

        registry.register(FakeTool())
        return {"playwright": FakeStack()}

    monkeypatch.setattr("nanobot.agent.tools.mcp.connect_mcp_servers", fake_connect)

    payload = asyncio.run(mcp_presets_test_action({"name": ["playwright"]}))

    assert payload["last_action"]["ok"] is True
    assert payload["last_action"]["tool_count"] == 1
    assert payload["last_action"]["tool_names"] == ["mcp_playwright_browser_navigate"]


def test_test_mcp_preset_inspects_tools_outside_the_enabled_allowlist(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_config(tmp_path, monkeypatch)
    mcp_presets_action("enable", {"name": ["playwright"]})
    config = load_config()
    config.tools.mcp_servers["playwright"].enabled_tools = [
        "mcp_playwright_browser_navigate",
    ]
    save_config(config)

    class FakeStack:
        async def aclose(self) -> None:
            return None

    async def fake_connect(servers, registry):
        assert servers["playwright"].enabled_tools == ["*"]

        class FakeTool:
            def __init__(self, name: str) -> None:
                self.name = name

            def to_schema(self):
                return {"name": self.name, "description": "", "parameters": {}}

        for index in range(20):
            registry.register(FakeTool(f"mcp_playwright_tool_{index:02d}"))
        return {"playwright": FakeStack()}

    monkeypatch.setattr("nanobot.agent.tools.mcp.connect_mcp_servers", fake_connect)

    payload = asyncio.run(mcp_presets_test_action({"name": ["playwright"]}))

    assert payload["last_action"]["tool_count"] == 20
    assert len(payload["last_action"]["tool_names"]) == 20
    row = next(item for item in payload["presets"] if item["name"] == "playwright")
    assert row["enabled_tools"] == ["mcp_playwright_browser_navigate"]


def test_test_mcp_preset_scrubs_connection_errors(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_config(tmp_path, monkeypatch)
    mcp_presets_action(
        "enable",
        {
            "name": ["browserbase"],
            "browserbase_api_key": ["bb_live_secret"],
        },
    )

    async def fake_connect(_servers, _registry):
        raise RuntimeError("failed https://mcp.browserbase.com/mcp?browserbaseApiKey=bb_live_secret")

    monkeypatch.setattr("nanobot.agent.tools.mcp.connect_mcp_servers", fake_connect)

    payload = asyncio.run(mcp_presets_test_action({"name": ["browserbase"]}))

    assert payload["last_action"]["ok"] is False
    assert "bb_live_secret" not in str(payload)
    assert "<redacted>" in payload["last_action"]["error"]


def test_unknown_oauth_placeholder_is_not_enabled(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    _use_config(tmp_path, monkeypatch)

    with pytest.raises(McpPresetError) as exc:
        mcp_presets_action("enable", {"name": ["asana"]})

    assert exc.value.status == 404


def test_normalize_mcp_preset_mentions_keeps_known_presets_only() -> None:
    payload = normalize_mcp_preset_mentions([
        {
            "name": "browserbase",
            "display_name": "Browserbase",
            "transport": "streamableHttp",
            "configured": True,
            "logo_url": "https://example.invalid/logo.svg",
        },
        {"name": "totally-unknown"},
        "bad",
    ])

    assert payload == [{
        "name": "browserbase",
        "display_name": "Browserbase",
        "transport": "streamableHttp",
        "configured": True,
        "logo_url": "https://example.invalid/logo.svg",
    }]


def test_custom_mcp_server_writes_config_and_catalog_row(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_config(tmp_path, monkeypatch)

    payload = custom_mcp_action(
        "custom",
        {
            "name": ["internal-docs"],
            "transport": ["stdio"],
            "command": ["node"],
            "args": ['["server.js"]'],
            "env": ['{"DOCS_TOKEN":"docs-secret-value"}'],
            "tool_timeout": ["45"],
        },
    )

    assert payload["requires_restart"] is True
    row = next(item for item in payload["presets"] if item["name"] == "internal-docs")
    assert row["source"] == "custom"
    assert row["transport"] == "stdio"
    assert row["connection_summary"] == "node server.js"
    assert row["manifest"]["schema"] == "agent-app.v1"
    assert row["manifest"]["source"] == "mcp-custom"
    assert row["manifest"]["capabilities"][0]["command"] == "node"
    assert "server.js" not in str(row["manifest"])
    assert "docs-secret-value" not in str(payload)
    config = load_config()
    assert config.tools.mcp_servers["internal-docs"].args == ["server.js"]
    assert config.tools.mcp_servers["internal-docs"].env["DOCS_TOKEN"] == "docs-secret-value"


def test_import_mcp_config_and_tool_allowlist(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_config(tmp_path, monkeypatch)

    payload = custom_mcp_action(
        "import",
        {
            "config": [
                (
                    '{"mcpServers":{'
                    '"docs":{"command":"npx","args":["-y","docs-mcp"],"env":{"API_KEY":"config-secret-value"}},'
                    '"remote-docs":{"transport":"sse","url":"https://example.com/sse"}'
                    '}}'
                )
            ],
        },
    )

    assert payload["last_action"]["message"] == "Imported 2 MCP server(s)."
    config = load_config()
    assert config.tools.mcp_servers["docs"].command == "npx"
    assert config.tools.mcp_servers["docs"].args == ["-y", "docs-mcp"]
    assert config.tools.mcp_servers["remote-docs"].type == "sse"
    assert config.tools.mcp_servers["remote-docs"].url == "https://example.com/sse"
    assert config.tools.mcp_servers["docs"].env["API_KEY"] == "config-secret-value"
    assert "config-secret-value" not in str(payload)

    payload = custom_mcp_action(
        "tools",
        {
            "name": ["docs"],
            "enabled_tools": ['["mcp_docs_search"]'],
        },
    )

    row = next(item for item in payload["presets"] if item["name"] == "docs")
    assert row["enabled_tools"] == ["mcp_docs_search"]
    assert load_config().tools.mcp_servers["docs"].enabled_tools == ["mcp_docs_search"]

    payload = custom_mcp_action(
        "tools",
        {
            "name": ["docs"],
            "enabled_tools": ["[]"],
        },
    )

    row = next(item for item in payload["presets"] if item["name"] == "docs")
    assert row["enabled_tools"] == []
    assert load_config().tools.mcp_servers["docs"].enabled_tools == []


def test_import_recognizes_known_and_explicit_oauth_servers(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_config(tmp_path, monkeypatch)

    payload = custom_mcp_action(
        "import",
        {
            "config": [
                (
                    '{"mcpServers":{'
                    '"notion-work":{"url":"https://mcp.notion.com/mcp"},'
                    '"company-mcp":{"url":"https://mcp.example.com/mcp","auth":"oauth"},'
                    '"notion-pat":{"url":"https://mcp.notion.com/mcp",'
                    '"headers":{"Authorization":"Bearer secret"}}'
                    '}}'
                )
            ],
        },
    )

    config = load_config()
    assert config.tools.mcp_servers["notion-work"].auth == "oauth"
    assert config.tools.mcp_servers["company-mcp"].auth == "oauth"
    assert config.tools.mcp_servers["notion-pat"].auth is None
    rows = {row["name"]: row for row in payload["presets"]}
    assert rows["notion-work"]["status"] == "authorization_required"
    assert rows["company-mcp"]["status"] == "authorization_required"
    assert rows["notion-pat"]["status"] == "configured"
    assert "Bearer secret" not in str(payload)


@pytest.mark.asyncio
async def test_replacing_oauth_config_removes_its_stored_credentials(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_config(tmp_path, monkeypatch)
    server_url = "https://mcp.example.com/mcp"
    custom_mcp_action(
        "custom",
        {
            "name": ["company-mcp"],
            "transport": ["streamableHttp"],
            "url": [server_url],
            "auth": ["oauth"],
        },
    )
    await MCPOAuthStorage("company-mcp", server_url).set_tokens(
        OAuthToken(access_token="secret")
    )
    assert mcp_oauth_has_credentials("company-mcp", server_url)

    custom_mcp_action(
        "custom",
        {
            "name": ["company-mcp"],
            "transport": ["streamableHttp"],
            "url": [server_url],
        },
    )

    assert not mcp_oauth_has_credentials("company-mcp", server_url)


def test_normalize_mcp_preset_mentions_accepts_configured_custom_server(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_config(tmp_path, monkeypatch)
    custom_mcp_action(
        "custom",
        {
            "name": ["docs"],
            "transport": ["streamableHttp"],
            "url": ["https://example.com/mcp"],
        },
    )

    payload = normalize_mcp_preset_mentions([
        {"name": "docs", "display_name": "Docs", "transport": "streamableHttp"},
    ])

    assert payload == [{"name": "docs", "display_name": "Docs", "transport": "streamableHttp"}]


def test_normalize_mcp_mentions_uses_explicit_gateway_config(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    default_path = tmp_path / "default.json"
    config_path = tmp_path / "gateway.json"
    save_config(Config(), default_path)
    monkeypatch.setattr("nanobot.config.loader._current_config_path", default_path)
    custom_mcp_action(
        "custom",
        {
            "name": ["gateway-docs"],
            "transport": ["streamableHttp"],
            "url": ["https://example.com/mcp"],
        },
        config_path=config_path,
    )

    payload = normalize_mcp_preset_mentions(
        [{"name": "gateway-docs", "display_name": "Gateway docs"}],
        config_path=config_path,
    )

    assert payload == [{"name": "gateway-docs", "display_name": "Gateway docs"}]


# -- MIT-1423: "test connection" against the operator-configured loopback connector ----
#
# The tenant Gmail connector (provision_tenant.py, mirrored by _server_dict in
# tests/tools/test_mcp_oauth_client_credentials.py) is provisioned into
# ``tools.mcpServers`` with a loopback MCP URL plus a loopback OAuth token URL.
# PR #73 (MIT-1405) made the runtime accept those entries by marking them via
# ``_mark_operator_configured``; the WebUI "test connection" check copies the
# config and must carry the same marker, or the SSRF guard blocks the
# operator's own connector and the page reports "blocked" while the runtime
# connects fine. These tests drive the real connect path (SSRF guard, pinned
# DNS transport, and the client-credentials token exchange) against a live
# listener on 127.0.0.1 -- no transport mocks.


_GMAIL_CLIENT_ID = "rt_0123456789abcdef"
_GMAIL_CLIENT_SECRET = "s3cr3t-value-that-must-never-be-logged"


def _gmail_server_dict(port: int, secret_file: Path) -> dict[str, Any]:
    """The ``tools.mcpServers`` shape provision_tenant.py writes for Gmail."""
    base = f"http://127.0.0.1:{port}"
    return {
        "type": "streamableHttp",
        "url": f"{base}/mcp",
        "oauthClientCredentials": {
            "tokenUrl": f"{base}/token",
            "clientId": _GMAIL_CLIENT_ID,
            "clientSecretFile": str(secret_file),
            "scopes": ["gmail.read", "gmail.search"],
        },
        "enabledTools": ["gmail_search"],
        "toolTimeout": 240,
    }


def _write_gmail_operator_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, port: int
) -> Path:
    """Materialize the provisioned tenant config and point the loader at it."""
    secret_file = tmp_path / "mcp-client-secret"
    secret_file.write_text(_GMAIL_CLIENT_SECRET + "\n", encoding="utf-8")
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "agents": {"defaults": {"workspace": str(tmp_path / "workspace")}},
                "tools": {"mcpServers": {"gmail": _gmail_server_dict(port, secret_file)}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("nanobot.config.loader._current_config_path", config_path)
    return config_path


class _MCPProbeHandler(BaseHTTPRequestHandler):
    """A real MCP streamable-HTTP endpoint plus an OAuth client-credentials token endpoint.

    Requests the SSRF guard blocks never reach this listener, so recording
    every request that does arrive is what distinguishes "guard allowed it"
    from "guard blocked it". The handler keeps no logs and never echoes the
    client secret back in any response body.
    """

    def log_message(self, *_args: object) -> None:
        pass

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _reply(self, status: int, payload: dict[str, Any] | None) -> None:
        body = b"" if payload is None else json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        self.server.recorded.append(("GET", self.path, None))
        self._reply(405, None)

    def do_POST(self) -> None:  # noqa: N802
        raw = self._read_body()
        auth = self.headers.get("Authorization")
        self.server.recorded.append((self.command, self.path, auth))
        if self.path == "/token":
            self._handle_token(raw, auth)
            return
        if self.path != "/mcp":
            self._reply(404, None)
            return
        self._handle_mcp(raw)

    def _handle_token(self, raw: bytes, auth: str | None) -> None:
        # Accept only the HTTP Basic client authentication the real flow
        # produces (RFC 9707); a wrong or missing credential gets the 401 the
        # runtime expects -- the secret itself never appears in any response.
        if not auth or not auth.startswith("Basic "):
            self._reply(401, {"error": "invalid_client"})
            return
        try:
            identity = base64.b64decode(auth.removeprefix("Basic ").encode("ascii")).decode("utf-8")
            client_id, secret = identity.split(":", 1)
        except (ValueError, UnicodeDecodeError):
            self._reply(401, {"error": "invalid_client"})
            return
        if client_id != _GMAIL_CLIENT_ID or secret != _GMAIL_CLIENT_SECRET:
            self._reply(401, {"error": "invalid_client"})
            return
        self._reply(
            200,
            {
                "access_token": "mit-1423-token",
                "token_type": "Bearer",
                "expires_in": 3600,
            },
        )

    def _handle_mcp(self, raw: bytes) -> None:
        try:
            msg = json.loads(raw or b"{}")
        except ValueError:
            msg = {}
        method = msg.get("method")
        msg_id = msg.get("id")
        if method == "initialize":
            params = msg.get("params") or {}
            self.server.protocols.append(str(params.get("protocolVersion", "")))
            self._reply(
                200,
                {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "protocolVersion": params.get("protocolVersion", "2025-03-26"),
                        "capabilities": {},
                        "serverInfo": {"name": "mit-1423-probe", "version": "0"},
                    },
                },
            )
            return
        if method == "notifications/initialized":
            self._reply(202, None)
            return
        if method == "tools/list":
            self._reply(
                200,
                {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "tools": [
                            {
                                "name": "probe_tool",
                                "description": "Probe tool served by the live test listener",
                                "inputSchema": {"type": "object", "properties": {}},
                            }
                        ]
                    },
                },
            )
            return
        if method == "tools/call":
            self._reply(
                200,
                {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {"content": [{"type": "text", "text": "ok"}], "isError": False},
                },
            )
            return
        if method == "resources/list":
            self._reply(200, {"jsonrpc": "2.0", "id": msg_id, "result": {"resources": []}})
            return
        if method == "prompts/list":
            self._reply(200, {"jsonrpc": "2.0", "id": msg_id, "result": {"prompts": []}})
            return
        if msg_id is not None:
            self._reply(
                200,
                {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {"code": -32601, "message": f"unsupported method: {method}"},
                },
            )
        else:
            self._reply(202, None)


class _MCPProbeServer:
    """A real loopback HTTP server: records every request that reaches it."""

    def __init__(self) -> None:
        self.recorded: list[tuple[str, str, str | None]] = []
        self.protocols: list[str] = []
        owner = self

        class Handler(_MCPProbeHandler):
            server_owner = owner

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._httpd.daemon_threads = True
        # The per-request handler reaches the recording lists through the
        # server instance that ThreadingHTTPServer hands it.
        self._httpd.recorded = self.recorded
        self._httpd.protocols = self.protocols
        self.port = self._httpd.server_address[1]
        self.base_url = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def requests_for(self, method: str, path: str) -> list[tuple[str, str, str | None]]:
        return [item for item in self.recorded if item[0] == method and item[1] == path]

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)


@pytest.fixture
def gmail_probe_server() -> Any:
    server = _MCPProbeServer()
    try:
        yield server
    finally:
        server.stop()


def _capture_mcp_logs() -> tuple[list[str], int]:
    lines: list[str] = []
    sink_id = logger.add(lambda message: lines.append(str(message)), level="TRACE")
    return lines, sink_id


def test_connection_test_reports_operator_configured_loopback_server_reachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, gmail_probe_server: _MCPProbeServer
) -> None:
    # Acceptance 1: the operator-configured loopback connector the runtime
    # reaches fine must not be reported as blocked. Before the fix the copied
    # config lost the marker, the guard refused the URL before any socket was
    # opened, and this assertion fails with ok=False.
    config_path = _write_gmail_operator_config(tmp_path, monkeypatch, gmail_probe_server.port)
    lines, sink_id = _capture_mcp_logs()
    try:
        payload = asyncio.run(mcp_presets_test_action({"name": ["gmail"]}, config_path=config_path))
    finally:
        logger.remove(sink_id)

    last = payload["last_action"]
    assert last["ok"] is True, last
    assert last["tool_count"] == 1
    assert any(name.endswith("probe_tool") for name in last["tool_names"])

    # The handshake was a real MCP conversation, and the token endpoint was
    # hit with the client credentials the config named -- proof the flow used
    # the operator-configured URL rather than a mocked transport.
    assert gmail_probe_server.protocols, "initialize never reached the test listener"
    token_requests = gmail_probe_server.requests_for("POST", "/token")
    assert token_requests, "token endpoint never reached the test listener"
    authorization = token_requests[0][2]
    assert authorization is not None and authorization.startswith("Basic "), token_requests[0]
    decoded = base64.b64decode(authorization.removeprefix("Basic ").encode("ascii")).decode("utf-8")
    assert decoded == f"{_GMAIL_CLIENT_ID}:{_GMAIL_CLIENT_SECRET}"
    mcp_requests = gmail_probe_server.requests_for("POST", "/mcp")
    assert mcp_requests, "MCP endpoint never reached the test listener"
    bearer = next(item for item in mcp_requests if item[2] and item[2].startswith("Bearer "))
    assert bearer[2] == "Bearer mit-1423-token"

    # Acceptance 3 (this run): the client secret stays in the Authorization
    # header the listener observed and never reaches a log line or payload.
    joined = "\n".join(lines)
    assert _GMAIL_CLIENT_SECRET not in joined
    assert _GMAIL_CLIENT_SECRET not in json.dumps(payload, default=str)


def test_connection_test_blocks_unconfigured_preset_but_operator_entry_reaches_listener(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, gmail_probe_server: _MCPProbeServer
) -> None:
    # Acceptance 2: the same loopback URL submitted as a new, unconfigured
    # preset (nothing in ``tools.mcpServers``) still reports blocked. An
    # unconfigured name must never reach the connect path at all -- the
    # action refuses it before any socket is opened, and the listener stays
    # silent to prove it. Pre-fix this behaves the same as post-fix; it is
    # the regression face of the marker change, so it must keep holding.
    _use_config(tmp_path, monkeypatch)
    with pytest.raises(McpPresetError) as excinfo:
        asyncio.run(mcp_presets_test_action({"name": ["gmail"]}, config_path=None))
    assert excinfo.value.status == 404
    assert "not enabled" in str(excinfo.value).lower()
    assert gmail_probe_server.recorded == [], "unconfigured preset reached the listener"

    # Negative control in the same run: a server object that never went
    # through the operator code path -- exactly what a plugin or model-
    # influenced edit produces -- must still be refused, even though the
    # identical URL was allowlisted seconds earlier in the sibling test.
    # Trust is granted only by the operator marker, so sharing the URL
    # cannot widen the guard.
    from nanobot.agent.tools.mcp import connect_mcp_servers
    from nanobot.agent.tools.registry import ToolRegistry
    from nanobot.config.schema import MCPServerConfig

    secret_file = tmp_path / "mcp-client-secret"
    secret_file.write_text(_GMAIL_CLIENT_SECRET + "\n", encoding="utf-8")
    unmarked = MCPServerConfig.model_validate(
        _gmail_server_dict(gmail_probe_server.port, secret_file)
    )
    try:
        stacks = asyncio.run(connect_mcp_servers({"gmail": unmarked}, ToolRegistry()))
    except Exception as exc:  # a refused connect before any socket is equally fine
        print(f"unmarked connect raised as expected: {type(exc).__name__}")
        stacks = {}
    assert stacks == {} or stacks.get("gmail") is None, "unmarked config was not refused"
    assert gmail_probe_server.recorded == [], "unmarked config reached the listener"


def test_connection_test_never_logs_the_client_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, gmail_probe_server: _MCPProbeServer
) -> None:
    # Acceptance 3, run independently of the reachable-path check above:
    # the config file holds a real client secret and the token exchange runs
    # over it, so any log record or error payload emitted by the tested code
    # path must not contain it -- in either the raw or the base64 form the
    # Authorization header would carry.
    config_path = _write_gmail_operator_config(tmp_path, monkeypatch, gmail_probe_server.port)
    lines, sink_id = _capture_mcp_logs()
    try:
        payload = asyncio.run(mcp_presets_test_action({"name": ["gmail"]}, config_path=config_path))
    finally:
        logger.remove(sink_id)

    assert payload["last_action"]["ok"] is True, payload["last_action"]
    assert any(
        item[2] and item[2].startswith("Basic ")
        for item in gmail_probe_server.requests_for("POST", "/token")
    ), "token endpoint never saw the Basic credentials, so the scan below is vacuous"

    encoded = base64.b64encode(
        f"{_GMAIL_CLIENT_ID}:{_GMAIL_CLIENT_SECRET}".encode("utf-8")
    ).decode("ascii")
    joined = "\n".join(lines)
    assert joined, "the tested path logged nothing at all"
    assert _GMAIL_CLIENT_SECRET not in joined
    assert encoded not in joined
    assert _GMAIL_CLIENT_SECRET not in json.dumps(payload, default=str)
