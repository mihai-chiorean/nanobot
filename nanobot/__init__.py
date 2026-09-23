"""
nanobot - A lightweight AI agent framework
"""

from importlib.metadata import PackageNotFoundError, version as _pkg_version
from pathlib import Path
import tomllib
import warnings


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
    return "0.1.5.post3"


__version__ = _resolve_version()
__logo__ = "🐈"

from nanobot.nanobot import Nanobot, RunResult

__all__ = ["Nanobot", "RunResult"]
