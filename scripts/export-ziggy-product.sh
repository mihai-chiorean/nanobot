#!/usr/bin/env bash
set -euo pipefail

usage() {
  printf 'Usage: %s --destination PATH [--source PATH]\n' "$0" >&2
  printf 'The destination must not already exist. The source worktree must be clean.\n' >&2
}

source_dir=""
destination=""
while (($#)); do
  case "$1" in
    --source)
      (($# >= 2)) || { usage; exit 2; }
      source_dir=$2
      shift 2
      ;;
    --destination)
      (($# >= 2)) || { usage; exit 2; }
      destination=$2
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      usage
      exit 2
      ;;
  esac
done

if [[ -z "$destination" ]]; then
  usage
  exit 2
fi

if [[ -z "$source_dir" ]]; then
  source_dir=$(git rev-parse --show-toplevel)
fi

source_dir=$(cd "$source_dir" && pwd -P)
destination=$(python3 -c 'import os, sys; print(os.path.abspath(sys.argv[1]))' "$destination")
manifest="$source_dir/config/ziggy-repository-split.json"

[[ -f "$manifest" ]] || { printf 'error: manifest not found: %s\n' "$manifest" >&2; exit 1; }
[[ -d "$source_dir/.git" || -f "$source_dir/.git" ]] || {
  printf 'error: source is not a Git worktree: %s\n' "$source_dir" >&2
  exit 1
}
[[ ! -e "$destination" ]] || {
  printf 'error: destination already exists; refusing to delete or overwrite: %s\n' "$destination" >&2
  exit 1
}

dirty=$(git -C "$source_dir" status --porcelain=v1 --untracked-files=all)
if [[ -n "$dirty" ]]; then
  printf 'error: source worktree is dirty; export requires a clean worktree\n%s\n' "$dirty" >&2
  exit 1
fi

mkdir -p "$destination"

python3 - "$source_dir" "$destination" "$manifest" <<'PY'
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

source = Path(sys.argv[1]).resolve()
destination = Path(sys.argv[2]).resolve()
manifest_path = Path(sys.argv[3]).resolve()
manifest = json.loads(manifest_path.read_text(encoding="utf-8"))


def run(*args: str) -> str:
    return subprocess.check_output(args, cwd=source, text=True).strip()


tracked = subprocess.check_output(
    ["git", "ls-files", "-z"], cwd=source
).decode("utf-8").split("\0")
tracked = [item for item in tracked if item]
tracked_set = set(tracked)
copied: set[str] = set()


def matches(path: str, prefix: str) -> bool:
    if path == prefix:
        return True
    if prefix.endswith("-"):
        return path.startswith(prefix)
    return path.startswith(prefix.rstrip("/") + "/")


def relative_path(path: str, prefix: str) -> str:
    if path == prefix:
        return ""
    if prefix.endswith("-"):
        parent = prefix.rsplit("/", 1)[0]
        return path[len(parent) + 1 :]
    return path[len(prefix.rstrip("/")) + 1 :]


def copy_file(
    source_path: Path,
    destination_path: Path,
    text_replacements: list[dict[str, str]] | None = None,
) -> None:
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    destination_key = destination_path.relative_to(destination).as_posix()
    if destination_key in copied:
        raise SystemExit(f"error: destination collision: {destination_key}")
    if text_replacements:
        content = source_path.read_text(encoding="utf-8")
        for replacement in text_replacements:
            old = replacement["from"]
            new = replacement["to"]
            count = content.count(old)
            if count != 1:
                raise SystemExit(
                    f"error: expected one text replacement in {source_path}: {old} (found {count})"
                )
            content = content.replace(old, new)
        destination_path.write_text(content, encoding="utf-8")
        shutil.copymode(source_path, destination_path)
    elif source_path.is_symlink():
        destination_path.symlink_to(os.readlink(source_path))
    else:
        shutil.copy2(source_path, destination_path)
    copied.add(destination_key)


for entry in manifest["entries"]:
    if "source" in entry:
        source_paths = [entry["source"]] if entry["source"] in tracked_set else []
        target_for = lambda _: entry["destination"]
    else:
        prefix = entry["source_prefix"]
        excluded = set(entry.get("exclude", []))
        source_paths = [
            path for path in tracked if matches(path, prefix) and path not in excluded
        ]

        def target_for(path: str, entry: dict = entry) -> str:
            if path == entry["source_prefix"]:
                return entry["destination_prefix"]
            return (
                f'{entry["destination_prefix"].rstrip("/")}/'
                f'{relative_path(path, entry["source_prefix"])}'
            )

    if entry.get("required") and not source_paths:
        source_name = entry.get("source", entry.get("source_prefix"))
        raise SystemExit(f"error: required allowlist path has no tracked files: {source_name}")
    for source_path_string in source_paths:
        copy_file(
            source / source_path_string,
            destination / target_for(source_path_string),
            entry.get("text_replacements"),
        )


source_commit = run("git", "rev-parse", "HEAD")
try:
    source_ref = run("git", "symbolic-ref", "--quiet", "--short", "HEAD")
except subprocess.CalledProcessError:
    source_ref = source_commit
try:
    source_remote = run("git", "config", "--get", "remote.origin.url")
except subprocess.CalledProcessError:
    source_remote = ""
manifest_sha256 = hashlib.sha256(manifest_path.read_bytes()).hexdigest()


def generated(kind: str) -> str:
    if kind == "product_readme":
        return """# Ziggy Product

This repository is the product-only Ziggy tree. The Nanobot upstream baseline is
pinned, but plain upstream is not the production runtime yet. Until the Ziggy
patch queue is removed or packaged, production uses the effective patched
runtime pinned in .ziggy/nanobot.lock.json; its source is not copied here.

## Layout

- ios/: SwiftUI client, Swift Testing targets, and the UI XCTest target.
- web/: branded PWA and its bridge extension.
- services/ziggy-control/: authenticated product front door and tenant control plane.
- services/ziggy-connectors/: tenant connector and MCP OAuth service.
- services/ziggy-runtime-manager/: rootless per-tenant runtime supervisor.
- services/ziggy-work/: durable Work service.
- observability/: collector, dashboards, and privacy validation assets.
- deploy/, docs/runbooks/, and docs/research/: operational indexes and source docs.

## Nanobot dependency

The upstream baseline and effective runtime pins are both in
.ziggy/nanobot.lock.json. Plain upstream v0.1.5.post3 must not be deployed by
itself: runtime patches that have not landed upstream are delivered as a
separately pinned runtime artifact or patch overlay; this repository must never
vendor the full Nanobot tree.

## Checks

    python3 scripts/validate-ziggy-product-split.py --export .
    cd web && npm ci && npm test && npm run lint && npm run build
    cd ../services/ziggy-control && make verify
    cd ../ziggy-connectors && make verify
    cd ../ziggy-runtime-manager && make verify
    cd ../ziggy-work && go test ./... && go test -race ./... && go vet ./... && go build ./...
    cd ../../web && npm audit --audit-level=high

The Swift and service CI mapping is generated at .github/workflows/ziggy-product.yml.
"""
    if kind == "product_gitignore":
        return """# Product build outputs and local configuration
.env
.env.*
!.env.example
web/node_modules/
web/dist/
web/coverage/
web/.vite/
*.tsbuildinfo
*.pyc
__pycache__/
.venv/
.pytest_cache/
.ruff_cache/
DerivedData/
*.xcuserstate
xcuserdata/
services/*/bin/
*.log
.DS_Store
.vscode/
.idea/
"""
    if kind == "license_index":
        return (source / "LICENSE").read_text(encoding="utf-8")
    if kind == "third_party_index":
        return """# Third-Party Notices

The Nanobot runtime dependency is licensed under MIT. The original Nanobot
license and third-party notice are preserved in licenses/. The PWA and Go
modules retain the notices required by their own lockfiles and package metadata.

This product repository does not redistribute the Nanobot source tree.
"""
    if kind == "deploy_index":
        return """# Deployment Assets

Deployment implementation stays beside its owning service:

- ../services/ziggy-control/deploy/
- ../services/ziggy-connectors/deploy/
- ../services/ziggy-runtime-manager/deploy/
- ../services/ziggy-work/deploy/
- ../observability/otelcol/

This index is deliberately not a second copy of those assets.
"""
    if kind == "runbooks_index":
        return """# Runbooks

The canonical runbooks are in ../docs/runbooks/.
"""
    if kind == "research_index":
        return """# Research

The canonical research notes are in ../docs/research/.
"""
    if kind == "product_ci":
        ci = """name: Ziggy Product CI

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

permissions:
  contents: read

jobs:
  validate-layout:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Validate product split
        run: python3 scripts/validate-ziggy-product-split.py --export .

  web:
    runs-on: ubuntu-latest
    defaults:
      run:
        working-directory: web
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: 22
          cache: npm
          cache-dependency-path: web/package-lock.json
      - run: npm ci
      - run: npm test
      - run: npm run lint
      - run: npm run build
      - name: Report npm audit findings
        run: npm audit --audit-level=high
        continue-on-error: true
      - name: Validate product tree after web build
        working-directory: .
        run: python3 scripts/validate-ziggy-product-split.py --export .
      - name: Assert web build boundary
        working-directory: .
        run: scripts/assert-ziggy-web-build.sh .

  swift:
    runs-on: macos-26
    steps:
      - uses: actions/checkout@v4
      - name: Select Xcode
        run: sudo xcode-select --switch /Applications/Xcode_26.6.app/Contents/Developer
      - name: Select an available iPhone simulator
        run: |
          set -euo pipefail
          device_id=$(xcrun simctl list devices available | awk -F '[()]' '/iPhone/ && $2 ~ /^[0-9A-F-]+$/ { print $2; exit }')
          test -n "$device_id"
          echo "ZIGGY_SIMULATOR_DESTINATION=platform=iOS Simulator,id=$device_id" >> "$GITHUB_ENV"
      - run: xcodebuild -project ios/Ziggy.xcodeproj -scheme Ziggy -destination "$ZIGGY_SIMULATOR_DESTINATION" CODE_SIGNING_ALLOWED=NO CODE_SIGNING_REQUIRED=NO build-for-testing
      - run: xcodebuild -project ios/Ziggy.xcodeproj -scheme Ziggy -destination "$ZIGGY_SIMULATOR_DESTINATION" CODE_SIGNING_ALLOWED=NO CODE_SIGNING_REQUIRED=NO test-without-building

  go:
    runs-on: ubuntu-latest
    strategy:
      fail-fast: false
      matrix:
        include:
          - name: control
            path: services/ziggy-control
            command: make verify linux-amd64 linux-arm64
          - name: connectors
            path: services/ziggy-connectors
            command: make verify linux-amd64 linux-arm64
          - name: runtime-manager
            path: services/ziggy-runtime-manager
            command: make verify
          - name: work
            path: services/ziggy-work
            command: go test ./... && go test -race ./... && go vet ./... && go build ./...
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-go@v5
        with:
          go-version: '1.25.x'
          check-latest: true
          cache-dependency-path: @@{{ matrix.path }}/go.sum
      - name: Verify @@{{ matrix.name }}
        working-directory: @@{{ matrix.path }}
        run: @@{{ matrix.command }}

  observability:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Validate shell assets
        run: |
          sh -n observability/otelcol/generate-otel-credentials
          sh -n observability/otelcol/install-otelcol-contrib
          sh -n observability/otelcol/validate-privacy-canary
          sh -n observability/otelcol/ziggy-otelcol-deploy
          sh -n observability/otelcol/ziggy-otelcol-start
"""
        return ci.replace("@@{{", chr(36) + "{{")
    if kind == "export_metadata":
        dependency = manifest["nanobot_dependency"]
        baseline = dependency["upstream_baseline"]
        effective_policy = dependency["effective_runtime_policy"]
        return json.dumps(
            {
                "schema": 2,
                "source_repository": source_remote,
                "source_ref": source_ref,
                "source_commit": source_commit,
                "source_commit_date": run("git", "show", "-s", "--format=%cI", "HEAD"),
                "manifest_sha256": manifest_sha256,
                "nanobot_dependency": {
                    "upstream_baseline": baseline,
                    "effective_runtime": {
                        "kind": effective_policy["pre_artifact_kind"],
                        "repository": source_remote,
                        "ref": source_ref,
                        "commit": source_commit,
                        "artifact": effective_policy["artifact"],
                    },
                    "update_rule": dependency["update_rule"],
                },
            },
            indent=2,
            sort_keys=True,
        ) + "\n"
    if kind == "nanobot_lock":
        dependency = manifest["nanobot_dependency"]
        baseline = dependency["upstream_baseline"]
        effective_policy = dependency["effective_runtime_policy"]
        return json.dumps(
            {
                "schema": 2,
                "upstream_baseline": baseline,
                "effective_runtime": {
                    "kind": effective_policy["pre_artifact_kind"],
                    "repository": source_remote,
                    "ref": source_ref,
                    "commit": source_commit,
                    "artifact": effective_policy["artifact"],
                },
                "update_rule": dependency["update_rule"],
            },
            indent=2,
            sort_keys=True,
        ) + "\n"
    raise SystemExit(f"error: unknown generated kind: {kind}")


for generated_entry in manifest["generated"]:
    output_path = destination / generated_entry["destination"]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_key = output_path.relative_to(destination).as_posix()
    if output_key in copied:
        raise SystemExit(f"error: generated destination collision: {output_key}")
    output_path.write_text(generated(generated_entry["kind"]), encoding="utf-8")
    copied.add(output_key)

print(f"source_commit={source_commit}")
print(f"files={len(copied)}")
print(f"destination={destination}")
PY

if ! git -C "$destination" init --initial-branch=main >/dev/null 2>&1; then
  git -C "$destination" init >/dev/null
  git -C "$destination" symbolic-ref HEAD refs/heads/main
fi

python3 "$source_dir/scripts/validate-ziggy-product-split.py" --export "$destination"
printf 'validated_export=%s\n' "$destination"
