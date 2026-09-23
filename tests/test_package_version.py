from __future__ import annotations

import json
import shutil
import subprocess
import sys
import textwrap
import tomllib
import warnings
from pathlib import Path

from loguru import logger

_STALE_DIST_VERSION = "9.9.9"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _pyproject_version() -> str:
    return tomllib.loads((_repo_root() / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "version"
    ]


def test_source_checkout_import_uses_pyproject_version_without_metadata() -> None:
    repo_root = _repo_root()
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


def _build_snapshot_tree(root: Path, *, project_name: str, tree_version: str) -> Path:
    """A cwd-first import target: package copy + its own pyproject.toml.

    Mirrors the deployed layout that produced MIT-1032: the executing tree
    sits on sys.path ahead of a venv whose dist metadata claims a different
    version. The fake ``nanobot_ai-*.dist-info`` directory supplies the
    metadata that importlib discovers for the snapshot path itself.
    """
    shutil.copytree(
        _repo_root() / "nanobot",
        root / "nanobot",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "{project_name}"\nversion = "{tree_version}"\n',
        encoding="utf-8",
    )
    dist_info = root / f"nanobot_ai-{_STALE_DIST_VERSION}.dist-info"
    dist_info.mkdir()
    (dist_info / "METADATA").write_text(
        "Metadata-Version: 2.1\n"
        f"Name: nanobot-ai\nVersion: {_STALE_DIST_VERSION}\n",
        encoding="utf-8",
    )
    return root


_REPORT_SCRIPT = (
    "import json, nanobot\n"
    "print(json.dumps({"
    '"version": nanobot.__version__, '
    '"dist": nanobot._dist_version()'
    "}))\n"
)


def _import_report(root: Path, *flags: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, *flags, "-c", _REPORT_SCRIPT],
        cwd=str(root),
        capture_output=True,
        text=True,
        check=False,
    )


def test_cwd_first_snapshot_import_reports_tree_version_not_venv_metadata(tmp_path) -> None:
    # The shipped failure (MIT-1011/MIT-1032): `python -m nanobot` from a
    # release snapshot put cwd first on sys.path, so the snapshot's package
    # executed while __version__ reported the venv's dist metadata.
    root = _build_snapshot_tree(
        tmp_path / "snapshot", project_name="nanobot-ai", tree_version="0.1.5.post3"
    )

    proc = _import_report(root)

    assert proc.returncode == 0, proc.stderr
    reported = json.loads(proc.stdout)
    assert reported["version"] == "0.1.5.post3"
    # The stale metadata really is discoverable at the snapshot path -- the
    # assertion below fails if importlib ever stops finding the fake dist-info.
    assert reported["dist"] == _STALE_DIST_VERSION
    # The disagreement must be surfaced on the project's logging channel...
    assert f"reports {_STALE_DIST_VERSION}" in proc.stderr
    assert "reporting the executing tree" in proc.stderr


def test_stale_metadata_layout_imports_cleanly_under_w_error(tmp_path) -> None:
    # Operators and test suites run with -W error; a stale-banner diagnostic
    # must not turn `import nanobot` into a failure on the deployed layout
    # (MIT-1032 review). The warnings module stays untouched -- any warning
    # emitted during import would escalate to ImportError under -W error.
    root = _build_snapshot_tree(
        tmp_path / "snapshot", project_name="nanobot-ai", tree_version="0.1.5.post3"
    )

    proc = _import_report(root, "-W", "error")

    assert proc.returncode == 0, proc.stderr
    reported = json.loads(proc.stdout)
    assert reported["version"] == "0.1.5.post3"
    # The diagnostic still reached the operator despite the strict filter.
    assert f"reports {_STALE_DIST_VERSION}" in proc.stderr


def test_decoy_pyproject_in_snapshot_layout_falls_back_to_metadata(tmp_path) -> None:
    # End-to-end form of the round-2 repro: nanobot/ copied next to another
    # project's pyproject.toml (name=other-app, version=9.9.9 there) must
    # report the dist metadata, never the decoy's version.
    root = _build_snapshot_tree(
        tmp_path / "decoy", project_name="other-app", tree_version="4.4.4"
    )

    proc = _import_report(root, "-W", "error")

    assert proc.returncode == 0, proc.stderr
    reported = json.loads(proc.stdout)
    assert reported["dist"] == _STALE_DIST_VERSION
    assert reported["version"] == _STALE_DIST_VERSION
    assert "4.4.4" not in proc.stderr


def test_executing_tree_wins_over_stale_dist_metadata(monkeypatch) -> None:
    # The deployed-runtime bug (MIT-1032/MIT-1011): dist metadata in the venv
    # described a different build than the package actually being imported.
    import nanobot

    monkeypatch.setattr(nanobot, "_dist_version", lambda: _STALE_DIST_VERSION)
    messages: list[str] = []
    sink_id = logger.add(messages.append, level="WARNING", format="{message}")
    try:
        with warnings.catch_warnings(record=True, action="always") as caught:
            assert nanobot._resolve_version() == _pyproject_version()
    finally:
        logger.remove(sink_id)
    assert any(
        _STALE_DIST_VERSION in m and _pyproject_version() in m for m in messages
    ), messages
    # The stale-banner diagnostic is loguru-only: warnings.warn would escalate
    # to ImportError for callers running with -W error (MIT-1032 review).
    assert not [w for w in caught if issubclass(w.category, RuntimeWarning)], caught


def test_matching_dist_metadata_is_silent(monkeypatch) -> None:
    import nanobot

    monkeypatch.setattr(nanobot, "_dist_version", lambda: _pyproject_version())
    messages: list[str] = []
    sink_id = logger.add(messages.append, level="WARNING", format="{message}")
    try:
        with warnings.catch_warnings(record=True, action="always") as caught:
            assert nanobot._resolve_version() == _pyproject_version()
    finally:
        logger.remove(sink_id)
    assert not [w for w in caught if "dist metadata" in str(w.message)]
    assert not [m for m in messages if "dist metadata" in m]


def test_metadata_only_install_reports_metadata(monkeypatch) -> None:
    import nanobot

    monkeypatch.setattr(nanobot, "_read_pyproject_version", lambda: None)
    monkeypatch.setattr(nanobot, "_dist_version", lambda: "1.2.3")
    assert nanobot._resolve_version() == "1.2.3"


def test_no_source_and_no_metadata_falls_back(monkeypatch) -> None:
    import nanobot

    monkeypatch.setattr(nanobot, "_read_pyproject_version", lambda: None)
    monkeypatch.setattr(nanobot, "_dist_version", lambda: None)
    assert nanobot._resolve_version() == "0.1.5.post3"


def _fake_checkout(tmp_path, *, name: str | None, version: str) -> Path:
    """A tree shaped like a package importable from ``tmp_path``."""
    fake_root = tmp_path / "vendored"
    package = fake_root / "nanobot"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    project = "" if name is None else f'name = "{name}"\n'
    (fake_root / "pyproject.toml").write_text(
        f"[project]\n{project}version = \"{version}\"\n", encoding="utf-8"
    )
    return fake_root


def test_foreign_pyproject_beside_package_is_ignored(monkeypatch, tmp_path) -> None:
    # A pyproject.toml belonging to another project must never be read as
    # ours, whatever sits two levels above the imported package (MIT-1032
    # review): a wheel that leaked its own pyproject into site-packages, or
    # nanobot/ vendored directly under some other project's root.
    import nanobot

    fake_root = _fake_checkout(
        tmp_path, name="somebody-elses-project", version=_STALE_DIST_VERSION
    )
    monkeypatch.setattr(nanobot, "__file__", str(fake_root / "nanobot" / "__init__.py"))
    monkeypatch.setattr(nanobot, "_dist_version", lambda: "0.3.0")
    assert nanobot._resolve_version() == "0.3.0"


def test_pyproject_without_project_name_falls_back_to_metadata(monkeypatch, tmp_path) -> None:
    # PEP 621 permits backends to supply [project] name dynamically, so a
    # nameless pyproject.toml is not evidence of being our own tree and must
    # not out-rank the installed metadata.
    import nanobot

    fake_root = _fake_checkout(tmp_path, name=None, version=_STALE_DIST_VERSION)
    monkeypatch.setattr(nanobot, "__file__", str(fake_root / "nanobot" / "__init__.py"))
    monkeypatch.setattr(nanobot, "_dist_version", lambda: "0.3.0")
    assert nanobot._resolve_version() == "0.3.0"


def test_pep503_equivalent_package_name_is_trusted(monkeypatch, tmp_path) -> None:
    # Name identity is compared per PEP 503, not by string equality: an
    # underscore spelling of our own name is still ours and must keep the
    # source tree authoritative.
    import nanobot

    fake_root = _fake_checkout(tmp_path, name="nanobot_ai", version="7.7.7")
    monkeypatch.setattr(nanobot, "__file__", str(fake_root / "nanobot" / "__init__.py"))
    monkeypatch.setattr(nanobot, "_dist_version", lambda: None)
    assert nanobot._resolve_version() == "7.7.7"
