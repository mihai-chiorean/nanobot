"""Read and switch the appliance-local model runtime."""

from __future__ import annotations

import json
import os
import subprocess
import threading
from pathlib import Path
from typing import Any

from loguru import logger


class ModelSwitchUnavailableError(Exception):
    """The configured model switch executable isn't installed."""


class ModelSwitchInProgressError(Exception):
    """A model transition is already running in this appliance runtime."""


_MAX_SWITCH_LOG_BYTES = 5 * 1024 * 1024
_switch_guard = threading.Lock()
_switch_process: subprocess.Popen[bytes] | None = None


def _rotate_log(path: Path) -> None:
    try:
        if path.stat().st_size < _MAX_SWITCH_LOG_BYTES:
            return
    except FileNotFoundError:
        return
    rotated = path.with_name(f"{path.name}.1")
    rotated.unlink(missing_ok=True)
    path.replace(rotated)


def state_path() -> Path:
    configured = os.environ.get("ZIGGY_ACTIVE_MODEL_STATE")
    if configured:
        return Path(configured)
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return Path(runtime_dir) / "ziggy-active-model.json"


def read_status() -> dict[str, Any]:
    path = state_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("state_path", str(path))
            return data
    except FileNotFoundError:
        pass
    except Exception as exc:
        logger.debug("could not read Ziggy active model state: {}", exc)
    try:
        from nanobot.config.loader import load_config

        model = load_config().agents.defaults.model.strip()
    except Exception:
        model = ""
    return {
        "status": "unknown",
        "active_model": model or None,
        "engine": None,
        "backend": None,
        "backend_port": None,
        "proxy_port": 8001,
        "state_source": "nanobot_config_fallback",
        "state_path": str(path),
    }


def request_switch(target: str, *, force: bool = False) -> dict[str, Any]:
    global _switch_process

    target = target.strip().lower()
    if target not in {"qwen", "minimax"}:
        raise ValueError("target must be qwen or minimax")
    configured = os.environ.get("ZIGGY_MODEL_SWITCH_SCRIPT", "").strip()
    if not configured:
        raise ModelSwitchUnavailableError("model switching is not enabled for this runtime")
    script = Path(configured).expanduser()
    if not script.is_file() or not os.access(script, os.X_OK):
        raise ModelSwitchUnavailableError("configured model switch script is not executable")
    args = [str(script), target]
    if force:
        args.append("--force")
    configured_log = os.environ.get("ZIGGY_MODEL_SWITCH_LOG", "").strip()
    log_path = (
        Path(configured_log).expanduser()
        if configured_log
        else state_path().with_name("model-switch-http.log")
    )
    with _switch_guard:
        if _switch_process is not None and _switch_process.poll() is None:
            raise ModelSwitchInProgressError("a model switch is already in progress")
        log_path.parent.mkdir(parents=True, exist_ok=True)
        _rotate_log(log_path)
        with log_path.open("ab", buffering=0) as log:
            _switch_process = subprocess.Popen(
                args,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
            )
    status = read_status()
    status.update(
        {
            "requested_target": target,
            "status": "switching",
            "log_path": str(log_path),
        }
    )
    return status
