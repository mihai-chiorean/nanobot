from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from nanobot import model_runtime


def test_request_switch_uses_explicit_executable(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = tmp_path / "switch-model"
    script.write_text("#!/bin/sh\n")
    script.chmod(0o700)
    monkeypatch.setenv("ZIGGY_MODEL_SWITCH_SCRIPT", str(script))
    monkeypatch.setenv("ZIGGY_ACTIVE_MODEL_STATE", str(tmp_path / "missing.json"))
    monkeypatch.setattr(model_runtime, "_switch_process", None)
    popen = MagicMock()
    monkeypatch.setattr(model_runtime.subprocess, "Popen", popen)

    status = model_runtime.request_switch("QWEN", force=True)

    assert status["status"] == "switching"
    assert status["requested_target"] == "qwen"
    assert popen.call_args.args[0] == [str(script), "qwen", "--force"]


def test_request_switch_uses_configured_log_path(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = tmp_path / "switch-model"
    script.write_text("#!/bin/sh\n")
    script.chmod(0o700)
    log_path = tmp_path / "logs" / "switch.log"
    monkeypatch.setenv("ZIGGY_MODEL_SWITCH_SCRIPT", str(script))
    monkeypatch.setenv("ZIGGY_MODEL_SWITCH_LOG", str(log_path))
    monkeypatch.setattr(model_runtime, "_switch_process", None)
    monkeypatch.setattr(model_runtime.subprocess, "Popen", MagicMock())

    model_runtime.request_switch("minimax")

    assert log_path.exists()
    assert model_runtime.subprocess.Popen.call_args.args[0] == [str(script), "minimax"]


@pytest.mark.parametrize("target", ["", "other", "qwen; rm -rf /"])
def test_request_switch_revalidates_target(target: str) -> None:
    with pytest.raises(ValueError, match="qwen or minimax"):
        model_runtime.request_switch(target)


def test_request_switch_has_no_host_specific_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ZIGGY_MODEL_SWITCH_SCRIPT", raising=False)

    with pytest.raises(
        model_runtime.ModelSwitchUnavailableError,
        match="not enabled",
    ):
        model_runtime.request_switch("qwen")


def test_request_switch_rejects_overlapping_process(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = tmp_path / "switch-model"
    script.write_text("#!/bin/sh\n")
    script.chmod(0o700)
    running = MagicMock()
    running.poll.return_value = None
    monkeypatch.setenv("ZIGGY_MODEL_SWITCH_SCRIPT", str(script))
    monkeypatch.setattr(model_runtime, "_switch_process", running)

    with pytest.raises(model_runtime.ModelSwitchInProgressError):
        model_runtime.request_switch("qwen")


def test_request_switch_rotates_bounded_log(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = tmp_path / "switch-model"
    script.write_text("#!/bin/sh\n")
    script.chmod(0o700)
    log_path = tmp_path / "switch.log"
    log_path.write_bytes(b"x" * model_runtime._MAX_SWITCH_LOG_BYTES)
    monkeypatch.setenv("ZIGGY_MODEL_SWITCH_SCRIPT", str(script))
    monkeypatch.setenv("ZIGGY_MODEL_SWITCH_LOG", str(log_path))
    monkeypatch.setattr(model_runtime, "_switch_process", None)
    monkeypatch.setattr(model_runtime.subprocess, "Popen", MagicMock())

    model_runtime.request_switch("qwen")

    assert log_path.with_name("switch.log.1").exists()
