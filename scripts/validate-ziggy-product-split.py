#!/usr/bin/env python3
"""Validate the product repository split contract and an exported tree."""

from __future__ import annotations

import argparse
import hashlib
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
            [
                "git",
                "-C",
                str(repo),
                "ls-tree",
                "-rz",
                "--name-only",
                "--full-tree",
                "HEAD",
            ],
            stderr=subprocess.PIPE,
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
    if manifest.get("schema") != 3:
        fail("unsupported manifest schema")
    source = manifest.get("source", {})
    repository = source.get("repository", "")
    if not re.fullmatch(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\.git", repository):
        fail("source repository must be a canonical HTTPS GitHub URL")
    if source.get("revision_policy") != "exact-head-commit":
        fail("source revision policy must pin the exact HEAD commit")
    dependency = manifest.get("nanobot_dependency", {})
    baseline = dependency.get("upstream_baseline", {})
    commit = baseline.get("commit", "")
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        fail("Nanobot upstream baseline must contain a 40-character commit pin")
    if not baseline.get("package") or not baseline.get("repository") or not baseline.get("ref"):
        fail("Nanobot upstream baseline must contain package, repository, and ref")
    policy = dependency.get("effective_runtime_policy", {})
    if policy.get("pre_artifact_kind") != "source-export":
        fail("effective runtime must use source-export until an artifact pin exists")
    artifact = policy.get("artifact")
    if not isinstance(artifact, dict) or set(artifact) != {
        "image",
        "digest",
        "wheel",
        "signature",
    }:
        fail("effective runtime artifact policy must declare image, digest, wheel, and signature")
    if any(value is not None for value in artifact.values()):
        fail("source-export runtime policy cannot claim packaged artifact evidence")
    deployment = policy.get("independent_deployment", {})
    if deployment != {
        "deployable": False,
        "status": "blocked-no-signed-runtime-artifact",
        "required_evidence": "signed-image-digest-or-wheel",
    }:
        fail("source-export runtime must be blocked from independent deployment")
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
    rewrite_ids = set()
    for rewrite in manifest.get("export_text_replacements", []):
        rewrite_id = rewrite.get("id")
        if not rewrite_id or rewrite_id in rewrite_ids:
            fail(f"export text replacement id is missing or duplicated: {rewrite_id}")
        rewrite_ids.add(rewrite_id)
        try:
            re.compile(rewrite["path_regex"])
        except (KeyError, re.error) as exc:
            fail(f"invalid export text replacement {rewrite_id}: {exc}")
        if not rewrite.get("from") or not rewrite.get("to"):
            fail(f"export text replacement is incomplete: {rewrite_id}")
        minimum_matches = rewrite.get("minimum_matches", 1)
        if not isinstance(minimum_matches, int) or minimum_matches < 1:
            fail(f"export text replacement minimum is invalid: {rewrite_id}")
    for rule in manifest["validation"].get("required_output_text", []):
        if not rule.get("path") or not rule.get("contains"):
            fail("required output text rules must contain path and contains")
    pattern_ids = set()
    for pattern in manifest["validation"].get("secret_content_patterns", []):
        try:
            re.compile(pattern["regex"])
        except (KeyError, re.error) as exc:
            fail(f"invalid secret content pattern: {exc}")
        pattern_id = pattern.get("id")
        if not pattern_id or pattern_id in pattern_ids:
            fail(f"secret content pattern id is missing or duplicated: {pattern_id}")
        pattern_ids.add(pattern_id)
    allowlist_ids = set()
    for item in manifest["validation"].get("secret_content_allowlist", []):
        allowlist_id = item.get("id")
        if not allowlist_id or allowlist_id in allowlist_ids:
            fail(f"secret content allowlist id is missing or duplicated: {allowlist_id}")
        allowlist_ids.add(allowlist_id)
        if item.get("pattern_id") not in pattern_ids:
            fail(f"secret content allowlist references an unknown pattern: {allowlist_id}")
        try:
            re.compile(item["path_regex"])
            re.compile(item["match_regex"])
        except (KeyError, re.error) as exc:
            fail(f"invalid secret content allowlist entry {allowlist_id}: {exc}")
    for signature in manifest["validation"].get(
        "nanobot_source_tree_signatures", []
    ):
        if (
            not isinstance(signature, list)
            or len(signature) < 3
            or any(not isinstance(path, str) or not path for path in signature)
        ):
            fail("Nanobot source-tree signatures must contain at least three paths")
    return manifest


def validate_source(source: Path, manifest: dict) -> None:
    tracked = tracked_paths(source)
    for entry in manifest["entries"]:
        if "source" in entry:
            name = entry["source"]
            present = name in tracked
        else:
            name = entry["source_prefix"]
            present = any(matches(path, name) for path in tracked)
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


def path_is_exempt(relative: str, patterns: list[re.Pattern[str]]) -> bool:
    return any(pattern.search(relative) for pattern in patterns)


def validate_text_content(export: Path, manifest: dict, files: list[Path]) -> None:
    validation = manifest["validation"]
    max_file_bytes = validation["secret_content_max_file_bytes"]
    max_total_bytes = validation["secret_content_max_total_bytes"]
    content_patterns = [
        (item["id"], re.compile(item["regex"]))
        for item in validation["secret_content_patterns"]
    ]
    content_allowlist = [
        (
            item["pattern_id"],
            re.compile(item["path_regex"]),
            re.compile(item["match_regex"]),
        )
        for item in validation.get("secret_content_allowlist", [])
    ]
    exempt_patterns = [
        re.compile(pattern)
        for pattern in validation.get("secret_content_exempt_path_patterns", [])
    ]
    scanned_bytes = 0
    for path in files:
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(export).as_posix()
        if path_is_exempt(relative, exempt_patterns):
            continue
        size = path.stat().st_size
        with path.open("rb") as handle:
            data = handle.read(max_file_bytes + 1)
        if b"\0" in data[:8192]:
            continue
        if size > max_file_bytes:
            fail(f"text file exceeds bounded secret scan limit: {relative}")
        scanned_bytes += len(data)
        if scanned_bytes > max_total_bytes:
            fail("export exceeds bounded total secret scan limit")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        for pattern_id, pattern in content_patterns:
            for match in pattern.finditer(text):
                allowed = any(
                    allow_pattern_id == pattern_id
                    and path_pattern.fullmatch(relative)
                    and match_pattern.fullmatch(match.group(0))
                    for allow_pattern_id, path_pattern, match_pattern in content_allowlist
                )
                if allowed:
                    continue
                fail(f"possible secret content ({pattern_id}) is present: {relative}")


def validate_export_rewrites(export: Path, manifest: dict, files: list[Path]) -> None:
    for rewrite in manifest.get("export_text_replacements", []):
        path_pattern = re.compile(rewrite["path_regex"])
        replacement_count = 0
        for path in files:
            if not path.is_file() or path.is_symlink():
                continue
            relative = path.relative_to(export).as_posix()
            if not path_pattern.fullmatch(relative):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                fail(
                    f"export text replacement targets non-UTF-8 output: "
                    f"{rewrite['id']} ({relative})"
                )
            if rewrite["from"] in text:
                fail(
                    f"export text replacement left source identity behind: "
                    f"{rewrite['id']} ({relative})"
                )
            replacement_count += text.count(rewrite["to"])
        minimum_matches = rewrite.get("minimum_matches", 1)
        if replacement_count < minimum_matches:
            fail(
                f"export text replacement output is missing: {rewrite['id']} "
                f"(found {replacement_count}, expected at least {minimum_matches})"
            )


def validate_no_nanobot_source_tree(
    export: Path,
    manifest: dict,
    files: list[Path],
) -> None:
    relative_files = {
        path.relative_to(export).as_posix()
        for path in files
        if path.is_file() or path.is_symlink()
    }
    signatures = manifest["validation"].get("nanobot_source_tree_signatures", [])
    for signature in signatures:
        anchor = signature[0]
        for relative in relative_files:
            if relative == anchor:
                root = ""
            elif relative.endswith("/" + anchor):
                root = relative[: -(len(anchor) + 1)]
            else:
                continue
            candidate_paths = {
                f"{root}/{signature_path}" if root else signature_path
                for signature_path in signature
            }
            if candidate_paths.issubset(relative_files):
                display_root = root or "."
                fail(f"Nanobot source tree detected at: {display_root}")


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
    files = all_export_files(export)
    for path in files:
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

    validate_no_nanobot_source_tree(export, manifest, files)

    for rule in manifest["validation"].get("required_output_text", []):
        rule_path = export / rule["path"]
        if not rule_path.is_file():
            fail(f"required output text file is missing: {rule['path']}")
        content = rule_path.read_text(encoding="utf-8")
        if rule["contains"] not in content:
            fail(f"required output text is missing from {rule['path']}: {rule['contains']}")
        if rule.get("absent") and rule["absent"] in content:
            fail(f"forbidden output text is present in {rule['path']}: {rule['absent']}")

    validate_text_content(export, manifest, files)
    validate_export_rewrites(export, manifest, files)

    lock_path = export / ".ziggy/nanobot.lock.json"
    try:
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"cannot read Nanobot lock metadata: {exc}")
    dependency = manifest["nanobot_dependency"]
    if lock.get("schema") != 3:
        fail("Nanobot lock has an unsupported schema")
    baseline = dependency["upstream_baseline"]
    if lock.get("upstream_baseline") != baseline:
        fail("Nanobot lock upstream baseline does not match the manifest")

    metadata_path = export / ".ziggy/export-metadata.json"
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"cannot read export metadata: {exc}")
    if not re.fullmatch(r"[0-9a-f]{40}", metadata.get("source_commit", "")):
        fail("export metadata does not contain a source commit")
    canonical_repository = manifest["source"]["repository"]
    if metadata.get("source_repository") != canonical_repository:
        fail("export metadata does not contain the canonical source repository")
    if "source_ref" in metadata:
        fail("export metadata must not depend on a local branch name")
    if metadata.get("schema") != 3:
        fail("export metadata has an unsupported schema")
    manifest_sha256 = hashlib.sha256(
        (export / "config/ziggy-repository-split.json").read_bytes()
    ).hexdigest()
    if metadata.get("manifest_sha256") != manifest_sha256:
        fail("export metadata manifest digest does not match the exported manifest")
    effective = lock.get("effective_runtime", {})
    expected_effective = {
        "kind": dependency["effective_runtime_policy"]["pre_artifact_kind"],
        "repository": canonical_repository,
        "commit": metadata["source_commit"],
        "artifact": dependency["effective_runtime_policy"]["artifact"],
        "independent_deployment": dependency["effective_runtime_policy"][
            "independent_deployment"
        ],
    }
    if effective != expected_effective:
        fail("Nanobot lock effective runtime does not match export source metadata")
    metadata_dependency = metadata.get("nanobot_dependency", {})
    if metadata_dependency.get("upstream_baseline") != baseline:
        fail("export metadata upstream baseline does not match the manifest")
    if metadata_dependency.get("effective_runtime") != expected_effective:
        fail("export metadata effective runtime does not match the lock")

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
