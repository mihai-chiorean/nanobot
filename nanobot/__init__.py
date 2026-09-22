"""
nanobot - A lightweight AI agent framework
"""

import tomllib
import warnings
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .agent.tools.context import RequestContext
    from .bus.runtime_events import SessionTurnPersisted
    from .nanobot import (
        STREAM_EVENT_REASONING_COMPLETED,
        STREAM_EVENT_REASONING_DELTA,
        STREAM_EVENT_RUN_COMPLETED,
        STREAM_EVENT_RUN_FAILED,
        STREAM_EVENT_RUN_STARTED,
        STREAM_EVENT_TEXT_COMPLETED,
        STREAM_EVENT_TEXT_DELTA,
        STREAM_EVENT_TOOL_COMPLETED,
        STREAM_EVENT_TOOL_FAILED,
        STREAM_EVENT_TOOL_STARTED,
        STREAM_EVENT_TYPES,
        LLMUsage,
        Nanobot,
        RunResult,
        RunStream,
        SessionInfo,
        SessionSnapshot,
        StreamEvent,
        StreamEventType,
    )
    from .runtime_context import RuntimeContextBlock, RuntimeContextProvider


def _read_pyproject_version() -> str | None:
    """Read the version declared by the source tree that owns this package."""
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    if not pyproject.exists():
        return None
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    return data.get("project", {}).get("version")


def _dist_version() -> str | None:
    try:
        return _pkg_version("nanobot-ai")
    except PackageNotFoundError:
        return None


def _resolve_version() -> str:
    # The version of the tree being imported is the version of the code that
    # executes. Dist metadata can describe a different install from the one
    # actually running: `python -m nanobot` puts the current directory first on
    # sys.path, so a release snapshot's package beats the path-appending
    # editable .pth -- __version__ used to report the venv's metadata while
    # the snapshot's code executed (MIT-1011 / MIT-1032).
    source_version = _read_pyproject_version()
    dist = _dist_version()
    if source_version is not None:
        if dist is not None and dist != source_version:
            warnings.warn(
                f"nanobot dist metadata reports {dist} but the executing tree at "
                f"{Path(__file__).resolve().parent.parent} declares {source_version}; "
                "reporting the executing tree (MIT-1032)",
                RuntimeWarning,
                stacklevel=2,
            )
        return source_version
    if dist is not None:
        return dist
    # Source checkouts without pyproject and installs without dist-info.
    return "0.3.0"


__version__ = _resolve_version()
__logo__ = "🐈"

_LAZY_EXPORTS = {
    "Nanobot": ".nanobot",
    "LLMUsage": ".nanobot",
    "RunStream": ".nanobot",
    "RunResult": ".nanobot",
    "RequestContext": ".agent.tools.context",
    "RuntimeContextBlock": ".runtime_context",
    "RuntimeContextProvider": ".runtime_context",
    "SessionInfo": ".nanobot",
    "SessionSnapshot": ".nanobot",
    "STREAM_EVENT_REASONING_COMPLETED": ".nanobot",
    "STREAM_EVENT_REASONING_DELTA": ".nanobot",
    "STREAM_EVENT_RUN_COMPLETED": ".nanobot",
    "STREAM_EVENT_RUN_FAILED": ".nanobot",
    "STREAM_EVENT_RUN_STARTED": ".nanobot",
    "STREAM_EVENT_TEXT_COMPLETED": ".nanobot",
    "STREAM_EVENT_TEXT_DELTA": ".nanobot",
    "STREAM_EVENT_TOOL_COMPLETED": ".nanobot",
    "STREAM_EVENT_TOOL_FAILED": ".nanobot",
    "STREAM_EVENT_TOOL_STARTED": ".nanobot",
    "STREAM_EVENT_TYPES": ".nanobot",
    "StreamEvent": ".nanobot",
    "StreamEventType": ".nanobot",
    "SessionTurnPersisted": ".bus.runtime_events",
}


def __getattr__(name: str) -> Any:
    module_path = _LAZY_EXPORTS.get(name)
    if module_path is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module
    mod = import_module(module_path, __name__)
    val = getattr(mod, name)
    globals()[name] = val
    return val


__all__ = [
    "Nanobot",
    "LLMUsage",
    "RunResult",
    "RequestContext",
    "RuntimeContextBlock",
    "RuntimeContextProvider",
    "RunStream",
    "SessionInfo",
    "SessionSnapshot",
    "STREAM_EVENT_REASONING_COMPLETED",
    "STREAM_EVENT_REASONING_DELTA",
    "STREAM_EVENT_RUN_COMPLETED",
    "STREAM_EVENT_RUN_FAILED",
    "STREAM_EVENT_RUN_STARTED",
    "STREAM_EVENT_TEXT_COMPLETED",
    "STREAM_EVENT_TEXT_DELTA",
    "STREAM_EVENT_TOOL_COMPLETED",
    "STREAM_EVENT_TOOL_FAILED",
    "STREAM_EVENT_TOOL_STARTED",
    "STREAM_EVENT_TYPES",
    "StreamEvent",
    "StreamEventType",
    "SessionTurnPersisted",
]
