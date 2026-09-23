"""
nanobot - A lightweight AI agent framework
"""

from importlib.metadata import PackageNotFoundError, version as _pkg_version
from pathlib import Path
import tomllib


_DIST_NAME = "nanobot-ai"


def _normalized_dist_name(name: object) -> str:
    """Normalize a distribution name per PEP 503 (case- and separator-folded)."""
    if not isinstance(name, str):
        return ""
    folded = name.lower().replace("_", "-").replace(".", "-")
    return "-".join(part for part in folded.split("-") if part)


def _read_pyproject_version() -> str | None:
    """Read the version declared by the source tree that owns this package.

    Only a ``pyproject.toml`` whose ``[project].name`` is this distribution is
    trusted: the lookup walks two levels above the imported package, where a
    wheel-leaked or vendored copy of some other project's pyproject.toml can
    sit, and its version must never masquerade as ours (MIT-1032 review).
    """
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    if not pyproject.exists():
        return None
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    project = data.get("project")
    if not isinstance(project, dict):
        return None
    if _normalized_dist_name(project.get("name")) != _DIST_NAME:
        return None
    version = project.get("version")
    return version if isinstance(version, str) else None


def _dist_version() -> str | None:
    try:
        return _pkg_version(_DIST_NAME)
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
            # Operator-facing diagnostic on the project's logging channel (the
            # channel for exactly this stale-banner condition, MIT-1011). A
            # warnings.warn here would be escalated to an ImportError by
            # callers running with -W error -- turning `import nanobot` into a
            # failure on the deployed immutable-snapshot layout. Imported
            # lazily so dependency-free `-S` smoke imports keep working.
            from loguru import logger

            logger.warning(
                f"nanobot dist metadata reports {dist} but the executing tree at "
                f"{Path(__file__).resolve().parent.parent} declares {source_version}; "
                "reporting the executing tree (MIT-1032)"
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
