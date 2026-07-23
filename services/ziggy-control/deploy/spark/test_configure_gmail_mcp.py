import importlib.util
import json
import sys
from pathlib import Path

import pytest

DIRECTORY = Path(__file__).parent
sys.path.insert(0, str(DIRECTORY))
MODULE_PATH = DIRECTORY / "configure_gmail_mcp.py"
SPEC = importlib.util.spec_from_file_location("configure_gmail_mcp", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

CLIENT_ID = "mcp_owner_runtime"


def test_update_hardens_workspace_and_adds_only_managed_gmail_server():
    document = {
        "channels": {"websocket": {"tokenIssueSecret": "bootstrap-only-secret"}},
        "tools": {
            "web": {"enable": True},
            "mcpServers": {"other": {"url": "https://example.test/mcp"}},
        },
    }
    assert MODULE.update_config(document, CLIENT_ID) is True
    assert document["tools"]["restrictToWorkspace"] is True
    assert document["tools"]["web"] == {"enable": True}
    assert document["tools"]["mcpServers"]["other"] == {
        "url": "https://example.test/mcp"
    }
    assert document["tools"]["mcpServers"]["ziggy_gmail"] == {
        "type": "streamableHttp",
        "url": "https://127.0.0.1:8790/mcp",
        "oauthClientCredentials": {
            "tokenUrl": "https://127.0.0.1:8790/oauth/token",
            "clientId": CLIENT_ID,
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
    assert MODULE.update_config(document, CLIENT_ID) is False


def test_update_rejects_missing_runtime_client_id():
    with pytest.raises(ValueError, match="client ID"):
        MODULE.update_config({"channels": {"websocket": {}}, "tools": {}}, "")


def test_main_updates_atomically_without_printing_capability(tmp_path, monkeypatch, capsys):
    filename = tmp_path / "config.json"
    filename.write_text(
        json.dumps(
            {
                "channels": {"websocket": {"tokenIssueSecret": "bootstrap-only-secret"}},
                "tools": {"mcpServers": {}},
            }
        ),
        encoding="utf-8",
    )
    filename.chmod(0o600)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "configure_gmail_mcp.py",
            "--config",
            str(filename),
            "--client-id",
            CLIENT_ID,
        ],
    )
    MODULE.main()
    output = capsys.readouterr().out
    assert "updated" in output
    assert CLIENT_ID not in output
    assert filename.stat().st_mode & 0o777 == 0o600
    assert "ziggy_gmail" in json.loads(filename.read_text())["tools"]["mcpServers"]
