#!/usr/bin/env python3
"""Validate the product repository split contract and an exported tree."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path


CLASSIFICATIONS = {"upstreamable", "product-boundary", "temporary"}


def fail(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


def matches(path: str, prefix: str) -> bool:
    if path == prefix:
        return True
    if prefix.endswith("-"):
        return path.startswith(prefix)
    return path.startswith(prefix.rstrip("/") + "/")


def tracked_paths(repo: Path) -> set[str]:
    try:
        output = subprocess.check_output(
            ["git", "-C", str(repo), "ls-files", "-z"], stderr=subprocess.PIPE
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        fail(f"cannot list tracked source paths in {repo}: {exc}")
    return {item for item in output.decode("utf-8").split("\0") if item}


def load_manifest(root: Path) -> dict:
    manifest_path = root / "config/ziggy-repository-split.json"
    if not manifest_path.is_file():
        fail(f"manifest not found: {manifest_path}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        fail(f"manifest is not valid JSON: {exc}")
    if manifest.get("schema") != 1:
        fail("unsupported manifest schema")
    dependency = manifest.get("nanobot_dependency", {})
    commit = dependency.get("commit", "")
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        fail("Nanobot dependency must contain a 40-character commit pin")
    if not dependency.get("repository") or not dependency.get("ref"):
        fail("Nanobot dependency must contain repository and ref")
    patches = manifest.get("patches", [])
    orders = []
    patch_ids = set()
    for patch in patches:
        patch_id = patch.get("id")
        if not patch_id or patch_id in patch_ids:
            fail(f"patch id is missing or duplicated: {patch_id}")
        patch_ids.add(patch_id)
        if patch.get("classification") not in CLASSIFICATIONS:
            fail(f"invalid patch classification for {patch_id}")
        order = patch.get("removal_order")
        if not isinstance(order, int) or order < 0:
            fail(f"invalid removal order for {patch_id}")
        orders.append(order)
        if not patch.get("paths") or not patch.get("action"):
            fail(f"patch entry is incomplete: {patch_id}")
    if len(orders) != len(set(orders)):
        fail("patch removal_order values must be unique")
    if orders != sorted(orders):
        fail("patch entries must be listed in removal order")
    if not manifest.get("entries") or not manifest.get("generated"):
        fail("manifest must contain entries and generated files")
    return manifest


def validate_source(source: Path, manifest: dict) -> None:
    for entry in manifest["entries"]:
        if "source" in entry:
            present = (source / entry["source"]).exists()
            name = entry["source"]
        else:
            name = entry["source_prefix"]
            if name.endswith("-"):
                parent = source / name.rsplit("/", 1)[0]
                present = any(parent.glob(name.rsplit("/", 1)[1] + "*"))
            else:
                present = (source / name).exists()
        if entry.get("required") and not present:
            fail(f"required source path is missing: {name}")
    print(f"validated_source={source}")


def all_export_files(export: Path) -> list[Path]:
    files = []
    for path in export.rglob("*"):
        if path == export / ".git" or export / ".git" in path.parents:
            continue
        files.append(path)
    return files


def validate_export(export: Path, manifest: dict) -> None:
    if not export.is_dir():
        fail(f"export directory does not exist: {export}")

    for required in manifest["validation"]["required_output_paths"]:
        if not (export / required).exists():
            fail(f"required output path is missing: {required}")

    forbidden = set(manifest["validation"]["forbidden_output_prefixes"])
    top_level_allowed = set()
    for entry in manifest["entries"]:
        destination = entry.get("destination", entry.get("destination_prefix"))
        top_level_allowed.add(destination.split("/", 1)[0])
    for entry in manifest["generated"]:
        top_level_allowed.add(entry["destination"].split("/", 1)[0])

    filename_patterns = [
        re.compile(pattern)
        for pattern in manifest["validation"]["secret_filename_patterns"]
    ]
    for path in all_export_files(export):
        relative = path.relative_to(export).as_posix()
        top_level = relative.split("/", 1)[0]
        if top_level in forbidden:
            fail(f"forbidden fork or source path is present: {relative}")
        if top_level not in top_level_allowed:
            fail(f"path is outside the export allowlist: {relative}")
        if path.is_symlink():
            target = (path.parent / os.readlink(path)).resolve()
            if export not in target.parents and target != export:
                fail(f"export contains a symlink outside the tree: {relative}")
        if path.is_file() and any(pattern.search(relative) for pattern in filename_patterns):
            fail(f"possible secret path is present: {relative}")

    lock_path = export / ".ziggy/nanobot.lock.json"
    try:
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"cannot read Nanobot lock metadata: {exc}")
    dependency = manifest["nanobot_dependency"]
    for key in ("package", "repository", "ref", "commit"):
        if lock.get(key) != dependency[key]:
            fail(f"Nanobot lock does not match manifest field: {key}")

    metadata_path = export / ".ziggy/export-metadata.json"
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"cannot read export metadata: {exc}")
    if not re.fullmatch(r"[0-9a-f]{40}", metadata.get("source_commit", "")):
        fail("export metadata does not contain a source commit")
    if metadata.get("nanobot_dependency", {}).get("commit") != dependency["commit"]:
        fail("export metadata Nanobot pin does not match manifest")

    if (export / "nanobot").exists():
        fail("Nanobot bulk copy detected at export root")
    print(f"validated_export={export}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path)
    parser.add_argument("--export", type=Path)
    args = parser.parse_args()
    if args.source is None and args.export is None:
        parser.error("at least one of --source or --export is required")

    root_for_manifest = (args.source or args.export).resolve()
    manifest = load_manifest(root_for_manifest)
    if args.source is not None:
        validate_source(args.source.resolve(), manifest)
    if args.export is not None:
        validate_export(args.export.resolve(), manifest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
