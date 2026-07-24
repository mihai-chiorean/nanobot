#!/usr/bin/env bash
set -euo pipefail

verify_sha256() {
  local expected=$1
  local file=$2
  local output
  local actual

  if command -v sha256sum >/dev/null 2>&1; then
    output=$(sha256sum "$file")
  elif command -v shasum >/dev/null 2>&1; then
    output=$(shasum -a 256 "$file")
  else
    printf 'error: no SHA-256 implementation is available\n' >&2
    return 1
  fi
  actual=${output%%[[:space:]]*}
  if [[ ! "$actual" =~ ^[0-9a-fA-F]{64}$ ]]; then
    printf 'error: SHA-256 implementation returned an invalid digest\n' >&2
    return 1
  fi
  actual=$(printf '%s' "$actual" | tr '[:upper:]' '[:lower:]')
  expected=$(printf '%s' "$expected" | tr '[:upper:]' '[:lower:]')
  if [[ "$actual" != "$expected" ]]; then
    printf 'error: SHA-256 mismatch for %s\n' "$file" >&2
    return 1
  fi
}

if [[ "${1:-}" == "--self-test-checksum" ]]; then
  temp_dir=$(mktemp -d "${TMPDIR:-/tmp}/ziggy-checksum-test.XXXXXX")
  trap 'rm -rf "$temp_dir"' EXIT
  fixture="$temp_dir/fixture"
  printf 'ziggy-checksum-fixture\n' > "$fixture"
  verify_sha256 \
    a6447bde01269eefc3b5d19f592dc421e2fd4c8825d9364a71594a47d9efc521 \
    "$fixture"
  if verify_sha256 \
    0000000000000000000000000000000000000000000000000000000000000000 \
    "$fixture" >/dev/null 2>&1; then
    printf 'error: checksum verifier accepted an incorrect digest\n' >&2
    exit 1
  fi
  printf 'checksum_self_test=passed\n'
  exit 0
fi

if (($# > 1)); then
  printf 'Usage: %s [ROOT|--self-test-checksum]\n' "$0" >&2
  exit 2
fi

root=${1:-.}
root=$(cd "$root" && pwd -P)

gitleaks_version=8.30.1
if [[ -n "${GITLEAKS_BIN:-}" ]]; then
  gitleaks=$GITLEAKS_BIN
else
  platform=$(uname -s)
  architecture=$(uname -m)
  case "$platform/$architecture" in
    Linux/x86_64)
      artifact=linux_x64
      checksum=551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb
      ;;
    Linux/aarch64|Linux/arm64)
      artifact=linux_arm64
      checksum=e4a487ee7ccd7d3a7f7ec08657610aa3606637dab924210b3aee62570fb4b080
      ;;
    Darwin/x86_64)
      artifact=darwin_x64
      checksum=dfe101a4db2255fc85120ac7f3d25e4342c3c20cf749f2c20a18081af1952709
      ;;
    Darwin/arm64)
      artifact=darwin_arm64
      checksum=b40ab0ae55c505963e365f271a8d3846efbc170aa17f2607f13df610a9aeb6a5
      ;;
    *)
      printf 'error: unsupported Gitleaks platform: %s/%s\n' "$platform" "$architecture" >&2
      exit 1
      ;;
  esac

  temp_dir=$(mktemp -d "${TMPDIR:-/tmp}/ziggy-gitleaks.XXXXXX")
  trap 'rm -rf "$temp_dir"' EXIT
  archive="$temp_dir/gitleaks.tar.gz"
  url="https://github.com/gitleaks/gitleaks/releases/download/v${gitleaks_version}/gitleaks_${gitleaks_version}_${artifact}.tar.gz"
  curl --fail --show-error --silent --location \
    --proto '=https' --tlsv1.2 \
    --output "$archive" \
    "$url"

  verify_sha256 "$checksum" "$archive"
  tar -xzf "$archive" -C "$temp_dir" gitleaks
  gitleaks="$temp_dir/gitleaks"
fi

"$gitleaks" dir \
  --redact \
  --no-banner \
  --verbose \
  --max-archive-depth=1 \
  --max-decode-depth=1 \
  "$root"
printf 'staged_tree_secret_scan=passed\n'
