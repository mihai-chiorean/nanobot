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
manifest_path="config/ziggy-repository-split.json"

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

source_commit=$(git -C "$source_dir" rev-parse --verify 'HEAD^{commit}')
git -C "$source_dir" cat-file -e "$source_commit:$manifest_path" 2>/dev/null || {
  printf 'error: manifest is not present in source commit %s: %s\n' \
    "$source_commit" "$manifest_path" >&2
  exit 1
}

mkdir -p "$destination"

python3 - "$source_dir" "$destination" "$source_commit" "$manifest_path" <<'PY'
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

source = Path(sys.argv[1]).resolve()
destination = Path(sys.argv[2]).resolve()
source_commit = sys.argv[3]
manifest_path = sys.argv[4]


def git_bytes(*args: str) -> bytes:
    return subprocess.check_output(["git", *args], cwd=source)


def git_text(*args: str) -> str:
    return git_bytes(*args).decode("utf-8").strip()


manifest_bytes = git_bytes("cat-file", "blob", f"{source_commit}:{manifest_path}")
manifest = json.loads(manifest_bytes.decode("utf-8"))

tree_entries: dict[str, tuple[str, str, str]] = {}
for record in git_bytes(
    "ls-tree", "-rz", "--full-tree", source_commit
).split(b"\0"):
    if not record:
        continue
    metadata, raw_path = record.split(b"\t", 1)
    mode, object_type, object_id = metadata.decode("ascii").split()
    path = raw_path.decode("utf-8")
    tree_entries[path] = (mode, object_type, object_id)

tracked = sorted(tree_entries)
tracked_set = set(tree_entries)
copied: set[str] = set()
rewrite_rules = [
    {
        **rule,
        "compiled_path_regex": re.compile(rule["path_regex"]),
        "matches": 0,
    }
    for rule in manifest.get("export_text_replacements", [])
]


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


def copy_blob(
    source_path: str,
    destination_path: Path,
    text_replacements: list[dict[str, str]] | None = None,
) -> None:
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    destination_key = destination_path.relative_to(destination).as_posix()
    if destination_key in copied:
        raise SystemExit(f"error: destination collision: {destination_key}")

    mode, object_type, object_id = tree_entries[source_path]
    if object_type != "blob":
        raise SystemExit(
            f"error: unsupported Git object in product export: "
            f"{source_path} ({object_type})"
        )
    content = git_bytes("cat-file", "blob", object_id)

    if mode == "120000":
        if text_replacements:
            raise SystemExit(
                f"error: text replacement cannot target a symlink: {source_path}"
            )
        destination_path.symlink_to(content.decode("utf-8"))
        copied.add(destination_key)
        return

    matching_rules = [
        rule
        for rule in rewrite_rules
        if rule["compiled_path_regex"].fullmatch(source_path)
    ]
    needs_text = bool(text_replacements) or bool(matching_rules)
    if needs_text:
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SystemExit(
                f"error: text transformation targeted non-UTF-8 blob: {source_path}"
            ) from exc

    if text_replacements:
        for replacement in text_replacements:
            old = replacement["from"]
            new = replacement["to"]
            count = text.count(old)
            if count != 1:
                raise SystemExit(
                    f"error: expected one text replacement in {source_path}: "
                    f"{old} (found {count})"
                )
            text = text.replace(old, new)

    for rule in matching_rules:
        count = text.count(rule["from"])
        if count:
            text = text.replace(rule["from"], rule["to"])
            rule["matches"] += count

    if needs_text:
        content = text.encode("utf-8")
    destination_path.write_bytes(content)
    destination_path.chmod(0o755 if mode == "100755" else 0o644)
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
        copy_blob(
            source_path_string,
            destination / target_for(source_path_string),
            entry.get("text_replacements"),
        )


for rule in rewrite_rules:
    minimum_matches = rule.get("minimum_matches", 1)
    if rule["matches"] < minimum_matches:
        raise SystemExit(
            f"error: export rewrite {rule['id']} matched {rule['matches']} values; "
            f"expected at least {minimum_matches}"
        )

source_repository = manifest["source"]["repository"]
manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()


def generated(kind: str) -> str:
    if kind == "product_readme":
        return """# Ziggy Product

This repository is the product-only Ziggy tree. It is not independently deployable:
the required patched Nanobot runtime has no signed image digest or wheel, and its
source is intentionally not copied here. The exact source commit is recorded for
provenance, not represented as a self-contained runtime artifact.

## Layout

- ios/: SwiftUI client, Swift Testing targets, and the UI XCTest target.
- web/: branded PWA.
- services/ziggy-control/: authenticated product front door and tenant control plane.
- services/ziggy-connectors/: tenant connector and MCP OAuth service.
- services/ziggy-runtime-manager/: rootless per-tenant runtime supervisor.
- services/ziggy-work/: durable Work service.
- observability/: collector, dashboards, and privacy validation assets.
- deploy/, docs/runbooks/, and docs/research/: operational indexes and source docs.

## Nanobot dependency

The upstream baseline and effective source provenance are both in
.ziggy/nanobot.lock.json. Plain upstream v0.1.5.post3 must not be deployed by
itself. Independent deployment remains blocked until the lock points to a signed
image digest or wheel containing the required runtime patches. This repository
must never vendor the full Nanobot tree.

## Checks

    python3 scripts/validate-ziggy-product-split.py --export .
    scripts/scan-ziggy-product-secrets.sh .
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
        return """Ziggy Product Source License Notice

Copyright (c) 2026 Mihai Chiorean. All rights reserved.

No license is granted to copy, modify, distribute, sublicense, or sell the
Ziggy product source in this repository unless a separate written license says
otherwise.

Portions derived from Nanobot remain subject to Nanobot's MIT license. The
complete Nanobot license and original third-party notices are preserved in the
licenses/ directory. Other third-party dependencies remain subject to their
respective license terms.
"""
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
        ci = r"""name: Ziggy Product CI

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
      - name: Scan staged product tree for secrets
        run: scripts/scan-ziggy-product-secrets.sh .

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
      - name: Reject high-severity npm advisories
        run: npm audit --audit-level=high
      - name: Validate product tree after web build
        working-directory: .
        run: python3 scripts/validate-ziggy-product-split.py --export .
      - name: Assert web build boundary
        working-directory: .
        run: scripts/assert-ziggy-web-build.sh .

  swift:
    runs-on: macos-26
    env:
      ZIGGY_CI_CLERK_LIVE_SUFFIX: ci_release_validation
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
      - name: Test Release configuration validation
        run: ios/Scripts/test-release-configuration.sh
      - name: Build unsigned Release app
        run: |
          build_number=$((GITHUB_RUN_NUMBER + 1))
          xcodebuild \
            -project ios/Ziggy.xcodeproj \
            -scheme Ziggy \
            -configuration Release \
            -destination "$ZIGGY_SIMULATOR_DESTINATION" \
            CODE_SIGNING_ALLOWED=NO \
            CODE_SIGNING_REQUIRED=NO \
            CURRENT_PROJECT_VERSION="${build_number}" \
            ZIGGY_ARCHIVE_BUILD_NUMBER="${build_number}" \
            CLERK_PUBLISHABLE_KEY="pk_live_${ZIGGY_CI_CLERK_LIVE_SUFFIX}" \
            build

  go:
    runs-on: ubuntu-latest
    services:
      postgres:
        image: postgres:16
        env:
          POSTGRES_DB: ziggy_control_test
          POSTGRES_USER: postgres
          POSTGRES_PASSWORD: postgres
        ports:
          - 5432:5432
        options: >-
          --health-cmd "pg_isready -U postgres -d ziggy_control_test"
          --health-interval 5s
          --health-timeout 5s
          --health-retries 10
    env:
      POSTGRES_PASSWORD: postgres
    strategy:
      fail-fast: false
      matrix:
        include:
          - name: control
            path: services/ziggy-control
            command: ZIGGY_CONTROL_TEST_DATABASE_URL="postgresql://postgres:${POSTGRES_PASSWORD}@127.0.0.1:5432/ziggy_control_test?sslmode=disable" make verify linux-amd64 linux-arm64
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
          cache-dependency-path: services/*/go.sum
      - name: Verify @@{{ matrix.name }}
        working-directory: @@{{ matrix.path }}
        run: @@{{ matrix.command }}

  observability:
    name: Validate collector (@@{{ matrix.arch }})
    runs-on: @@{{ matrix.runner }}
    strategy:
      fail-fast: false
      matrix:
        include:
          - arch: amd64
            runner: ubuntu-24.04
          - arch: arm64
            runner: ubuntu-24.04-arm
    env:
      OTELCOL_VERSION: "0.147.0"
      GRAFANA_CLOUD_OTLP_ENDPOINT: https://example.invalid/otlp
      GRAFANA_CLOUD_INSTANCE_ID: "1"
      HOSTNAME: validation-host
    steps:
      - uses: actions/checkout@v4
      - name: Download and verify pinned collector
        working-directory: observability/otelcol
        run: |
          set -eu
          archive="otelcol-contrib_${OTELCOL_VERSION}_linux_@@{{ matrix.arch }}.tar.gz"
          curl --fail --show-error --silent --location \
            --proto '=https' --tlsv1.2 \
            --output "${archive}" \
            "https://github.com/open-telemetry/opentelemetry-collector-releases/releases/download/v${OTELCOL_VERSION}/${archive}"
          grep " ${archive}$" "otelcol-contrib-${OTELCOL_VERSION}-linux.sha256" | sha256sum --check
          tar -xzf "${archive}"
          ./otelcol-contrib --version | grep "${OTELCOL_VERSION}"
      - name: Create validation-only credentials
        run: |
          set -eu
          printf 'validation-only\n' > "${RUNNER_TEMP}/grafana-token"
          hash="$(openssl passwd -apr1 validation-password)"
          printf 'ziggy-control:%s\n' "${hash}" > "${RUNNER_TEMP}/otel-local-users.htpasswd"
      - name: Parse YAML
        run: |
          ruby -e 'require "yaml"; ARGV.each { |path| YAML.safe_load_file(path, aliases: true) }' \
            .github/workflows/ziggy-product.yml \
            observability/otelcol/beelink.yaml \
            observability/otelcol/spark.yaml \
            observability/otelcol/privacy-canary.yaml
      - name: Validate collector profiles
        env:
          GRAFANA_CLOUD_API_KEY_FILE: @@{{ runner.temp }}/grafana-token
          ZIGGY_OTLP_HTPASSWD_FILE: @@{{ runner.temp }}/otel-local-users.htpasswd
          PRIVACY_CANARY_ENDPOINT: 127.0.0.1:24318
          PRIVACY_CANARY_OUTPUT: @@{{ runner.temp }}/privacy-canary-output.json
          PRIVACY_CANARY_HTPASSWD_FILE: @@{{ runner.temp }}/otel-local-users.htpasswd
          PRIVACY_CANARY_UNREACHABLE_ENDPOINT: http://127.0.0.1:24319
          PRIVACY_CANARY_METRICS_PORT: 24320
        run: |
          collector=observability/otelcol/otelcol-contrib
          "${collector}" validate --config=observability/otelcol/beelink.yaml
          "${collector}" validate --config=observability/otelcol/spark.yaml
          "${collector}" validate --config=observability/otelcol/privacy-canary.yaml
      - name: Validate privacy boundary
        run: observability/otelcol/validate-privacy-canary observability/otelcol/otelcol-contrib
      - name: Validate shell scripts
        run: |
          sh -n \
            observability/otelcol/generate-otel-credentials \
            observability/otelcol/install-otelcol-contrib \
            observability/otelcol/validate-privacy-canary \
            observability/otelcol/ziggy-otelcol-deploy \
            observability/otelcol/ziggy-otelcol-start
"""
        return ci.replace("@@{{", chr(36) + "{{")
    if kind == "export_metadata":
        dependency = manifest["nanobot_dependency"]
        baseline = dependency["upstream_baseline"]
        effective_policy = dependency["effective_runtime_policy"]
        return json.dumps(
            {
                "schema": 3,
                "source_repository": source_repository,
                "source_commit": source_commit,
                "source_commit_date": git_text(
                    "show", "-s", "--format=%cI", source_commit
                ),
                "manifest_sha256": manifest_sha256,
                "nanobot_dependency": {
                    "upstream_baseline": baseline,
                    "effective_runtime": {
                        "kind": effective_policy["pre_artifact_kind"],
                        "repository": source_repository,
                        "commit": source_commit,
                        "artifact": effective_policy["artifact"],
                        "independent_deployment": effective_policy[
                            "independent_deployment"
                        ],
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
                "schema": 3,
                "upstream_baseline": baseline,
                "effective_runtime": {
                    "kind": effective_policy["pre_artifact_kind"],
                    "repository": source_repository,
                    "commit": source_commit,
                    "artifact": effective_policy["artifact"],
                    "independent_deployment": effective_policy[
                        "independent_deployment"
                    ],
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

python3 "$destination/scripts/validate-ziggy-product-split.py" --export "$destination"
printf 'validated_export=%s\n' "$destination"
