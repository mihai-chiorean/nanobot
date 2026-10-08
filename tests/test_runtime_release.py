"""Tests for nanobot.runtime_release (TP-03)."""

from __future__ import annotations

from pathlib import Path

import pytest

from nanobot import runtime_release
from nanobot.runtime_release import apply_release_env, resolve_release_id


def _write_release_file(root: Path, text: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "RELEASE_ID").write_text(text, encoding="utf-8")


def test_env_var_wins_over_file(tmp_path: Path) -> None:
    _write_release_file(tmp_path, "from-file")
    env = {"ZIGGY_RELEASE": "from-env"}
    assert resolve_release_id(env=env, root=tmp_path) == "from-env"


def test_file_first_line_used_when_env_unset(tmp_path: Path) -> None:
    _write_release_file(tmp_path, "  ziggy-main-ca6317de  \nsecond line\n")
    assert resolve_release_id(env={}, root=tmp_path) == "ziggy-main-ca6317de"


def test_missing_file_is_unknown(tmp_path: Path) -> None:
    assert resolve_release_id(env={}, root=tmp_path) == "unknown"


@pytest.mark.parametrize("bad", ["bad id", "x" * 70, "-leading-dash", "", "sp ace", ".dot-first"])
def test_malformed_value_is_unknown(tmp_path: Path, bad: str) -> None:
    _write_release_file(tmp_path, bad)
    assert resolve_release_id(env={}, root=tmp_path) == "unknown"
    assert resolve_release_id(env={"ZIGGY_RELEASE": bad}, root=tmp_path) == "unknown"


@pytest.mark.parametrize("good", ["x", "Ziggy-1.2_3+build", "a" * 64])
def test_valid_charset_values_pass_through(tmp_path: Path, good: str) -> None:
    assert resolve_release_id(env={"ZIGGY_RELEASE": good}, root=tmp_path) == good


def test_release_id_is_cached_once_per_process(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime_release, "_cached_release_id", None)
    monkeypatch.setenv("ZIGGY_RELEASE", "first")
    assert runtime_release.release_id() == "first"
    monkeypatch.setenv("ZIGGY_RELEASE", "second")
    assert runtime_release.release_id() == "first"


def test_apply_keeps_existing_langfuse_release(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime_release, "release_id", lambda: "rel-7")
    env = {"LANGFUSE_RELEASE": "kept"}
    assert apply_release_env(env=env) == "rel-7"
    assert env["LANGFUSE_RELEASE"] == "kept"


def test_apply_sets_both_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime_release, "release_id", lambda: "x")
    env = {"OTEL_RESOURCE_ATTRIBUTES": "a=b"}
    assert apply_release_env(env=env) == "x"
    assert env["LANGFUSE_RELEASE"] == "x"
    assert env["OTEL_RESOURCE_ATTRIBUTES"] == "a=b,service.version=x"


def test_apply_sets_service_version_when_otel_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime_release, "release_id", lambda: "x")
    env: dict[str, str] = {}
    assert apply_release_env(env=env) == "x"
    assert env["OTEL_RESOURCE_ATTRIBUTES"] == "service.version=x"


def test_apply_does_not_add_a_second_service_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime_release, "release_id", lambda: "x")
    env = {"OTEL_RESOURCE_ATTRIBUTES": "a=b,service.version=other"}
    apply_release_env(env=env)
    assert env["OTEL_RESOURCE_ATTRIBUTES"] == "a=b,service.version=other"
