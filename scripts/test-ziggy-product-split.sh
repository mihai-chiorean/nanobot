#!/usr/bin/env bash
set -euo pipefail

usage() {
  printf 'Usage: %s --source PATH --export PATH\n' "$0" >&2
}

source_dir=""
export_dir=""
while (($#)); do
  case "$1" in
    --source)
      (($# >= 2)) || { usage; exit 2; }
      source_dir=$2
      shift 2
      ;;
    --export)
      (($# >= 2)) || { usage; exit 2; }
      export_dir=$2
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

[[ -n "$source_dir" && -n "$export_dir" ]] || { usage; exit 2; }
source_dir=$(cd "$source_dir" && pwd -P)
export_dir=$(cd "$export_dir" && pwd -P)
validator="$source_dir/scripts/validate-ziggy-product-split.py"

python3 "$validator" --source "$source_dir" --export "$export_dir"

tmp_root=$(printenv TMPDIR || printf '/tmp')
tmp_dir=$(mktemp -d "$tmp_root/ziggy-split-test.XXXXXX")
trap 'rm -rf "$tmp_dir"' EXIT

missing="$tmp_dir/missing-required"
cp -a "$export_dir" "$missing"
rm -rf "$missing/web"
if python3 "$validator" --export "$missing" >/dev/null 2>&1; then
  printf 'error: validator accepted an export missing web/\n' >&2
  exit 1
fi

bulk="$tmp_dir/nanobot-bulk-copy"
cp -a "$export_dir" "$bulk"
mkdir -p "$bulk/nanobot/agent"
printf 'fork copy sentinel\n' > "$bulk/nanobot/agent/loop.py"
if python3 "$validator" --export "$bulk" >/dev/null 2>&1; then
  printf 'error: validator accepted a Nanobot bulk copy\n' >&2
  exit 1
fi

printf 'split_validation_tests=passed\n'
