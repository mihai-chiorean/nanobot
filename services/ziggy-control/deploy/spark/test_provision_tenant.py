import importlib.util
import json
import sys
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).with_name("provision_tenant.py")
SPEC = importlib.util.spec_from_file_location("provision_tenant", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

BOOTSTRAP_SECRET = "bootstrap-secret-012345678901234567890"
MCP_CLIENT_ID = "mcp_owner_runtime"


def test_tenant_config_isolates_state_and_scrubs_nonlocal_credentials(tmp_path: Path):
    source = {
        "agents": {"defaults": {"workspace": "/owner", "model": "custom/qwen"}},
        "gateway": {"host": "0.0.0.0", "port": 18790, "heartbeat": {"enabled": True}},
        "channels": {
            "sendProgress": True,
            "discord": {"enabled": True, "token": "owner-secret"},
            "websocket": {
                "enabled": True,
                "authIssuer": "https://clerk.test",
                "authJwksUrl": "https://clerk.test/.well-known/jwks.json",
                "authAuthorizedParties": ["https://chat.example.com"],
            },
        },
        "tools": {
            "restrictToWorkspace": False,
            "mcpServers": {"owner": {"url": "http://owner"}},
            "exec": {"enable": True},
            "rag": {"enable": True},
        },
        "providers": {
            "custom": {"apiBase": "http://127.0.0.1:8001/v1", "apiKey": "local-placeholder"},
            "openai": {"apiBase": "https://api.openai.com/v1", "apiKey": "owner-cloud-key"},
        },
    }
    root = tmp_path / "tenant"

    generated = MODULE.tenant_config(
        source,
        root,
        "Tester@Example.com",
        18800,
        "100.86.74.94",
        18802,
        BOOTSTRAP_SECRET,
        enable_gmail_mcp=True,
        gmail_mcp_client_id=MCP_CLIENT_ID,
    )

    assert generated["agents"]["defaults"]["workspace"] == str(root / "workspace")
    assert generated["gateway"] == {
        "host": "127.0.0.1",
        "port": 18800,
        "heartbeat": {"enabled": False},
    }
    assert "discord" not in generated["channels"]
    assert generated["channels"]["websocket"]["authAllowedEmails"] == ["tester@example.com"]
    assert generated["channels"]["websocket"]["tokenIssuePath"] == "/auth/token"
    assert generated["channels"]["websocket"]["tokenIssueSecret"] == BOOTSTRAP_SECRET
    assert generated["channels"]["websocket"]["pingIntervalS"] is None
    assert generated["tools"]["restrictToWorkspace"] is True
    assert generated["tools"]["exec"]["enable"] is False
    assert generated["tools"]["rag"] == {"enable": False}
    assert generated["tools"]["mcpServers"] == {
        "ziggy_gmail": {
            "type": "streamableHttp",
            "url": "https://127.0.0.1:8790/mcp",
            "oauthClientCredentials": {
                "tokenUrl": "https://127.0.0.1:8790/oauth/token",
                "clientId": MCP_CLIENT_ID,
                "clientSecretFile": "${ZIGGY_MCP_CLIENT_SECRET_FILE}",
                "scopes": ["gmail.status", "gmail.search", "gmail.read"],
            },
            "enabledTools": [
                "gmail_connection_status",
                "gmail_search",
                "gmail_get_message",
            ],
            "toolTimeout": 30,
        }
    }
    assert generated["providers"]["custom"]["apiKey"] == "local-placeholder"
    assert generated["providers"]["openai"] == {}
    assert source["channels"]["discord"]["token"] == "owner-secret"


def test_tenant_config_rejects_incomplete_clerk_auth(tmp_path: Path):
    source = {
        "channels": {"websocket": {"enabled": True, "authIssuer": "https://clerk.test"}},
    }

    try:
        MODULE.tenant_config(
            source,
            tmp_path / "tenant",
            "tester@example.com",
            18800,
            "100.86.74.94",
            18802,
            BOOTSTRAP_SECRET,
        )
    except ValueError as exc:
        assert "authJwksUrl" in str(exc)
    else:
        raise AssertionError("incomplete Clerk auth must be rejected")


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8790/mcp",
        "http://10.0.0.2:8790/mcp",
        "http://127.0.0.1:99999/mcp",
        "http://user:password@127.0.0.1:8790/mcp",
        "http://127.0.0.1:8790/other",
        "http://127.0.0.1:8790/mcp?tenant=other",
    ],
)
def test_tenant_config_rejects_unsafe_connector_mcp_url(tmp_path: Path, url: str):
    with pytest.raises(ValueError, match="connector MCP URL"):
        MODULE.tenant_config(
            {
                "channels": {
                    "websocket": {
                        "authIssuer": "https://clerk.test",
                        "authJwksUrl": "https://clerk.test/.well-known/jwks.json",
                        "authAuthorizedParties": ["https://chat.example.com"],
                    }
                }
            },
            tmp_path / "tenant",
            "tester@example.com",
            18800,
            "100.86.74.94",
            18802,
            BOOTSTRAP_SECRET,
            url,
            True,
            MCP_CLIENT_ID,
        )


@pytest.mark.parametrize(
    "token_url",
    [
        "http://127.0.0.1:8790/oauth/token",
        "https://10.0.0.2:8790/oauth/token",
        "https://127.0.0.1:8790/other",
    ],
)
def test_gmail_mcp_server_rejects_unsafe_connector_token_url(token_url: str):
    with pytest.raises(ValueError, match="connector token URL"):
        MODULE.gmail_mcp_server(
            MCP_CLIENT_ID,
            "https://127.0.0.1:8790/mcp",
            token_url,
        )


def test_tenant_config_rejects_gmail_without_runtime_client(tmp_path: Path):
    with pytest.raises(ValueError, match="client ID"):
        MODULE.tenant_config(
            {
                "channels": {
                    "websocket": {
                        "authIssuer": "https://clerk.test",
                        "authJwksUrl": "https://clerk.test/.well-known/jwks.json",
                        "authAuthorizedParties": ["https://chat.example.com"],
                    }
                }
            },
            tmp_path / "tenant",
            "tester@example.com",
            18800,
            "100.86.74.94",
            18802,
            BOOTSTRAP_SECRET,
            enable_gmail_mcp=True,
        )


@pytest.mark.parametrize(
    "host",
    [
        "0.0.0.0",
        "::",
        "224.0.0.1",
        "ff02::1",
        "169.254.1.1",
        "fe80::1",
        "8.8.8.8",
        "2001:4860:4860::8888",
    ],
)
def test_tenant_config_rejects_unsafe_websocket_bind_hosts(tmp_path: Path, host: str):
    with pytest.raises(ValueError, match="WebSocket host"):
        MODULE.tenant_config(
            {
                "channels": {
                    "websocket": {
                        "authIssuer": "https://clerk.test",
                        "authJwksUrl": "https://clerk.test/.well-known/jwks.json",
                        "authAuthorizedParties": ["https://chat.example.com"],
                    }
                }
            },
            tmp_path / "tenant",
            "tester@example.com",
            18800,
            host,
            18802,
            BOOTSTRAP_SECRET,
        )


@pytest.mark.parametrize(
    "host",
    ["127.0.0.1", "::1", "10.1.2.3", "172.16.4.5", "192.168.1.2", "fc00::1234", "100.64.0.10"],
)
def test_tenant_config_allows_private_websocket_bind_hosts(tmp_path: Path, host: str):
    generated = MODULE.tenant_config(
        {
            "channels": {
                "websocket": {
                    "authIssuer": "https://clerk.test",
                    "authJwksUrl": "https://clerk.test/.well-known/jwks.json",
                    "authAuthorizedParties": ["https://chat.example.com"],
                }
            }
        },
        tmp_path / "tenant",
        "tester@example.com",
        18800,
        host,
        18802,
        BOOTSTRAP_SECRET,
    )
    assert generated["channels"]["websocket"]["host"] == host


def test_read_bootstrap_secret_rejects_missing_file(tmp_path: Path):
    with pytest.raises(ValueError, match="cannot read bootstrap secret file"):
        MODULE.read_bootstrap_secret(tmp_path / "missing-secret")


def test_read_bootstrap_secret_rejects_short_material(tmp_path: Path):
    filename = tmp_path / "bootstrap-secret"
    filename.write_text(" too-short \n", encoding="utf-8")
    with pytest.raises(ValueError, match="at least 32 characters"):
        MODULE.read_bootstrap_secret(filename)


def test_main_writes_bootstrap_secret_to_config_but_not_stdout(tmp_path: Path, monkeypatch, capsys):
    source = tmp_path / "source.json"
    source.write_text(
        json.dumps(
            {
                "channels": {
                    "websocket": {
                        "authIssuer": "https://clerk.test",
                        "authJwksUrl": "https://clerk.test/.well-known/jwks.json",
                        "authAuthorizedParties": ["https://chat.example.com"],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    secret_file = tmp_path / "bootstrap-secret"
    secret_file.write_text(f"  {BOOTSTRAP_SECRET}\n", encoding="utf-8")
    tenant_root = tmp_path / "tenant"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "provision_tenant.py",
            "--source-config",
            str(source),
            "--tenant-root",
            str(tenant_root),
            "--email",
            "tester@example.com",
            "--gateway-port",
            "18800",
            "--websocket-host",
            "100.64.0.10",
            "--websocket-port",
            "18802",
            "--bootstrap-secret-file",
            str(secret_file),
            "--enable-gmail-mcp",
            "--gmail-mcp-client-id",
            MCP_CLIENT_ID,
        ],
    )

    MODULE.main()

    output = capsys.readouterr().out
    assert str(tenant_root / "runtime" / "config.json") in output
    assert BOOTSTRAP_SECRET not in output
    generated = json.loads((tenant_root / "runtime" / "config.json").read_text(encoding="utf-8"))
    assert generated["channels"]["websocket"]["tokenIssueSecret"] == BOOTSTRAP_SECRET
    gmail = generated["tools"]["mcpServers"]["ziggy_gmail"]
    assert gmail["oauthClientCredentials"]["clientId"] == MCP_CLIENT_ID
    assert BOOTSTRAP_SECRET not in json.dumps(gmail)
