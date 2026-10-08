"""Resolve the Ziggy release id this runtime is running as (TP-03).

Releases on the Spark are directories such as ``releases/ziggy-main-ca6317de/runtime``, and
containers bind-mount that tree at ``/opt/ziggy/runtime``, so the directory name is lost.  The
release build (TP-04) writes a ``RELEASE_ID`` file at the root of the runtime tree;
``ZIGGY_RELEASE`` overrides it for host units and tests.  ``__version__`` cannot answer this —
it is upstream nanobot's.

The id feeds ``LANGFUSE_RELEASE`` and the OTel ``service.version`` resource attribute via
:func:`apply_release_env`, which the gateway entry point calls before the provider snapshot is
built.  Langfuse reads both when its client is first built (``get_client()``,
``nanobot/observability/langfuse.py``), not at import, so that is early enough.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, MutableMapping
from pathlib import Path

from loguru import logger

UNKNOWN_RELEASE = "unknown"

# The collector's service.version rule (observability/otelcol/beelink.yaml).
_RELEASE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")

_cached_release_id: str | None = None
_warned_unknown = False


def _runtime_root() -> Path:
    """The runtime tree root: the directory holding the ``nanobot`` package."""
    import nanobot

    return Path(nanobot.__file__).resolve().parent.parent


def _read_release_file(root: Path) -> str:
    try:
        content = (root / "RELEASE_ID").read_text(encoding="utf-8")
    except OSError:
        return ""
    lines = content.splitlines()
    return lines[0] if lines else ""


def resolve_release_id(env: Mapping[str, str] = os.environ, root: Path | None = None) -> str:
    """Return the release id: ``ZIGGY_RELEASE``, else the ``RELEASE_ID`` file, else ``unknown``.

    A candidate that does not match ``^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$`` is reported as
    ``"unknown"``; the fact is logged once per process at WARNING.
    """
    candidate = env.get("ZIGGY_RELEASE")
    if candidate is None:
        candidate = _read_release_file(_runtime_root() if root is None else root)
    value = candidate.strip()
    if _RELEASE_ID_RE.match(value):
        return value
    global _warned_unknown
    if not _warned_unknown:
        _warned_unknown = True
        logger.warning(
            "nanobot release id unknown; set ZIGGY_RELEASE or write a RELEASE_ID file at the "
            "runtime tree root (TP-03)"
        )
    return UNKNOWN_RELEASE


def release_id() -> str:
    """The process-wide release id, resolved once and cached."""
    global _cached_release_id
    if _cached_release_id is None:
        _cached_release_id = resolve_release_id()
    return _cached_release_id


def apply_release_env(env: MutableMapping[str, str] = os.environ) -> str:
    """Export the release id where Langfuse and OTel pick it up; return it.

    Sets ``LANGFUSE_RELEASE`` only if unset, and appends ``service.version=<id>`` to
    ``OTEL_RESOURCE_ATTRIBUTES`` only when that key has no ``service.version=`` entry, keeping
    existing entries.  Call before the first ``langfuse.get_client()``.
    """
    value = release_id()
    if "LANGFUSE_RELEASE" not in env:
        env["LANGFUSE_RELEASE"] = value
    existing = env.get("OTEL_RESOURCE_ATTRIBUTES", "")
    has_service_version = any(
        entry.split("=", 1)[0].strip() == "service.version"
        for entry in existing.split(",")
        if entry.strip()
    )
    if not has_service_version:
        env["OTEL_RESOURCE_ATTRIBUTES"] = (
            f"{existing},service.version={value}" if existing else f"service.version={value}"
        )
    return value


__all__ = ["UNKNOWN_RELEASE", "apply_release_env", "release_id", "resolve_release_id"]
