#!/usr/bin/env bash
set -euo pipefail

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

  if command -v sha256sum >/dev/null 2>&1; then
    printf '%s  %s\n' "$checksum" "$archive" | sha256sum --check
  else
    printf '%s  %s\n' "$checksum" "$archive" | shasum -a 256 --check
  fi
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
