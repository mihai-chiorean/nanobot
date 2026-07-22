import importlib.util
from pathlib import Path

MODULE_PATH = Path(__file__).with_name("provision_tenant.py")
SPEC = importlib.util.spec_from_file_location("provision_tenant", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_tenant_config_isolates_state_and_scrubs_nonlocal_credentials(tmp_path: Path):
    source = {
        "agents": {"defaults": {"workspace": "/owner", "model": "custom/qwen"}},
        "gateway": {"host": "0.0.0.0", "port": 18790, "heartbeat": {"enabled": True}},
        "channels": {
            "sendProgress": True,
            "discord": {"enabled": True, "token": "owner-secret"},
            "websocket": {"enabled": True, "authIssuer": "https://clerk.test"},
        },
        "tools": {"restrictToWorkspace": False, "mcpServers": {"owner": {"url": "http://owner"}}, "exec": {"enable": True}},
        "providers": {
            "custom": {"apiBase": "http://127.0.0.1:8001/v1", "apiKey": "local-placeholder"},
            "openai": {"apiBase": "https://api.openai.com/v1", "apiKey": "owner-cloud-key"},
        },
    }
    root = tmp_path / "tenant"

    generated = MODULE.tenant_config(source, root, "Tester@Example.com", 18800, "100.86.74.94", 18802)

    assert generated["agents"]["defaults"]["workspace"] == str(root / "workspace")
    assert generated["gateway"] == {"host": "127.0.0.1", "port": 18800, "heartbeat": {"enabled": False}}
    assert "discord" not in generated["channels"]
    assert generated["channels"]["websocket"]["authAllowedEmails"] == ["tester@example.com"]
    assert generated["channels"]["websocket"]["pingIntervalS"] is None
    assert generated["tools"]["restrictToWorkspace"] is True
    assert generated["tools"]["exec"]["enable"] is False
    assert generated["tools"]["mcpServers"] == {}
    assert generated["providers"]["custom"]["apiKey"] == "local-placeholder"
    assert generated["providers"]["openai"] == {}
    assert source["channels"]["discord"]["token"] == "owner-secret"
