"""Cross-suite test infrastructure."""

from __future__ import annotations

import os
import ssl
import sys
from collections.abc import Iterator
from pathlib import Path

import certifi
import pytest
from loguru import logger


@pytest.fixture(autouse=True)
def _isolate_nanobot_log_activation() -> Iterator[None]:
    """Keep CLI log settings from leaking into later tests in the same process."""
    logger.enable("nanobot")
    try:
        yield
    finally:
        logger.enable("nanobot")


@pytest.fixture(autouse=True)
def _isolate_home_dir(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """Point HOME at a per-test temp dir so the suite never touches the real ~/.nanobot.

    MIT-1473: running the suite on a host with a live ``~/.nanobot`` wrote
    test rows into the owner's real ``audit.jsonl`` (on the ``cli``, ``api``,
    ``telegram`` and ``feishu`` channels) and left ~20 stray test namespace
    dirs under ``~/.nanobot/sessions``. Every one of those paths -- the
    default config path (``nanobot.config.loader.get_config_path``), the
    default workspace (``nanobot.config.paths.get_workspace_path`` /
    ``nanobot.utils.helpers``), the ``AuditLogger`` fallback log path
    (``nanobot.agent.tools.audit``), and the CLI history path -- falls back
    to ``Path.home() / ".nanobot"`` when nothing more specific is
    configured. ``Path.home()`` resolves via the ``HOME`` environment
    variable on POSIX, so redirecting ``HOME`` here covers every one of
    those fallbacks in one place instead of patching each call site.

    A test that needs a specific ``HOME`` (for example to assert on
    ``Path.home()`` directly) can still call
    ``monkeypatch.setenv("HOME", ...)`` itself -- that overrides this
    fixture's value for the rest of that test, and monkeypatch unwinds both
    in reverse order at teardown.

    Uses ``tmp_path_factory.mktemp`` rather than deriving a path from
    ``tmp_path`` for two reasons: it never nests under a test's own
    ``tmp_path`` (some tests assert that dir holds nothing they didn't put
    there), and its name is a fixed, generic basename plus a counter, not
    the test's nodeid -- a test named e.g. ``test_secrets_excluded`` would
    otherwise get a HOME path containing the substring "secrets", tripping
    up assertions that scan environment values for leaked secrets.
    """
    fake_home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(fake_home))
    yield


@pytest.fixture(autouse=True)
def _isolate_sessions_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Redirect session storage away from the real active config data directory.

    Session storage lives under the active runtime data root (outside the workspace,
    per ADR-0001), so without redirection tests would write into the real home.
    """
    runtime_root = tmp_path.parent / f"{tmp_path.name}-runtime-root"
    legacy_root = tmp_path.parent / f"{tmp_path.name}-legacy-sessions-root"

    def runtime_subdir(name: str) -> Path:
        path = runtime_root / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    monkeypatch.setattr(
        "nanobot.session.manager.get_runtime_subdir",
        runtime_subdir,
    )
    monkeypatch.setattr(
        "nanobot.session.manager.get_legacy_sessions_dir",
        lambda: legacy_root,
    )
    yield


@pytest.fixture(autouse=True)
def _isolate_pairing_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep channel pairing tests out of the user's active pairing store."""
    pairing_path = tmp_path / "pairing.json"
    monkeypatch.setattr(
        "nanobot.pairing.store._store_path",
        lambda: pairing_path,
    )


@pytest.fixture(scope="session", autouse=True)
def _use_windows_system_ca_for_default_http_clients() -> Iterator[None]:
    """Avoid reparsing certifi's CA bundle for every offline HTTP client.

    Loading certifi takes roughly 0.7 seconds per client on Windows. The test
    suite constructs hundreds of clients while mocking their I/O. System roots
    preserve certificate verification for accidental local requests; explicit
    ``cafile``, ``capath``, and ``cadata`` arguments still use the real loader.
    """
    if sys.platform != "win32":
        yield
        return

    original = ssl.create_default_context
    certifi_path = os.path.normcase(os.path.abspath(certifi.where()))

    def create_default_context(
        purpose: ssl.Purpose = ssl.Purpose.SERVER_AUTH,
        *,
        cafile: str | None = None,
        capath: str | None = None,
        cadata: str | bytes | None = None,
    ) -> ssl.SSLContext:
        requested_path = os.path.normcase(os.path.abspath(cafile)) if cafile else None
        if requested_path == certifi_path and capath is None and cadata is None:
            return original(purpose)
        return original(
            purpose,
            cafile=cafile,
            capath=capath,
            cadata=cadata,
        )

    ssl.create_default_context = create_default_context
    try:
        yield
    finally:
        ssl.create_default_context = original
