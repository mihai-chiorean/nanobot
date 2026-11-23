"""Session context-window lookup that sizes upload payloads (MIT-1967 review)."""

from types import SimpleNamespace

from nanobot.agent.loop import AgentLoop
from nanobot.session.model_selection import SESSION_MODEL_PRESET_METADATA_KEY


class _Resolver:
    def __init__(self) -> None:
        self._default = SimpleNamespace(context_window_tokens=200_000)
        self._presets = {"big": SimpleNamespace(context_window_tokens=262_144)}

    def admit(self):
        return self._default

    def resolve_preset(self, name: str):
        return self._presets[name]


def _loop(record):
    return SimpleNamespace(
        sessions=SimpleNamespace(read_session_metadata=lambda _key: record),
        runtime_resolver=_Resolver(),
    )


def test_preset_less_session_gets_the_default_window() -> None:
    assert AgentLoop.session_context_window_tokens(_loop({"metadata": {}}), "websocket:c") == 200_000
    assert AgentLoop.session_context_window_tokens(_loop(None), "websocket:c") == 200_000
    assert AgentLoop.session_context_window_tokens(_loop("junk"), "websocket:c") == 200_000


def test_selected_preset_reports_its_own_window() -> None:
    record = {"metadata": {SESSION_MODEL_PRESET_METADATA_KEY: "big"}}
    assert AgentLoop.session_context_window_tokens(_loop(record), "websocket:c") == 262_144


def test_removed_preset_falls_back_to_the_default_window() -> None:
    record = {"metadata": {SESSION_MODEL_PRESET_METADATA_KEY: "removed-model"}}
    assert AgentLoop.session_context_window_tokens(_loop(record), "websocket:c") == 200_000
