from __future__ import annotations

import subprocess
import sys
import textwrap
import tomllib
import warnings
from pathlib import Path


def _pyproject_version() -> str:
    repo_root = Path(__file__).resolve().parents[1]
    return tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "version"
    ]


def test_source_checkout_import_uses_pyproject_version_without_metadata() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    expected = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "version"
    ]
    script = textwrap.dedent(
        f"""
        import sys
        import types

        sys.path.insert(0, {str(repo_root)!r})
        fake = types.ModuleType("nanobot.nanobot")
        fake.Nanobot = object
        fake.RunResult = object
        sys.modules["nanobot.nanobot"] = fake

        import nanobot

        print(nanobot.__version__)
        """
    )

    proc = subprocess.run(
        [sys.executable, "-S", "-c", script],
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == expected


def test_executing_tree_wins_over_stale_dist_metadata(monkeypatch) -> None:
    # The deployed-runtime bug (MIT-1032/MIT-1011): dist metadata in the venv
    # described a different build than the package actually being imported.
    import nanobot

    monkeypatch.setattr(nanobot, "_dist_version", lambda: "9.9.9")
    with warnings.catch_warnings(record=True, action="always") as caught:
        assert nanobot._resolve_version() == _pyproject_version()
    messages = [str(w.message) for w in caught]
    assert any(
        "9.9.9" in m and _pyproject_version() in m for m in messages
    ), messages
    assert any(w.category is RuntimeWarning for w in caught)


def test_matching_dist_metadata_is_silent(monkeypatch) -> None:
    import nanobot

    monkeypatch.setattr(nanobot, "_dist_version", lambda: _pyproject_version())
    with warnings.catch_warnings(record=True, action="always") as caught:
        assert nanobot._resolve_version() == _pyproject_version()
    assert not [w for w in caught if "dist metadata" in str(w.message)]


def test_metadata_only_install_reports_metadata(monkeypatch) -> None:
    import nanobot

    monkeypatch.setattr(nanobot, "_read_pyproject_version", lambda: None)
    monkeypatch.setattr(nanobot, "_dist_version", lambda: "1.2.3")
    assert nanobot._resolve_version() == "1.2.3"


def test_no_source_and_no_metadata_falls_back(monkeypatch) -> None:
    import nanobot

    monkeypatch.setattr(nanobot, "_read_pyproject_version", lambda: None)
    monkeypatch.setattr(nanobot, "_dist_version", lambda: None)
    assert nanobot._resolve_version() == "0.3.0"
