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

web_build="$tmp_dir/web-build"
cp -a "$export_dir" "$web_build"
mkdir -p "$web_build/web/dist"
printf 'fixture build output\n' > "$web_build/web/dist/index.html"
"$source_dir/scripts/assert-ziggy-web-build.sh" "$web_build" >/dev/null

nested_env="$tmp_dir/nested-env"
cp -a "$export_dir" "$nested_env"
mkdir -p "$nested_env/web/src/config"
printf 'REAL_SECRET=not-an-example\n' > "$nested_env/web/src/config/.env"
if python3 "$validator" --export "$nested_env" >/dev/null 2>&1; then
  printf 'error: validator accepted a nested .env file\n' >&2
  exit 1
fi

content_secrets="$tmp_dir/content-secrets"
cp -a "$export_dir" "$content_secrets"
mkdir -p "$content_secrets/web/src"
private_key='-----BEGIN '"PRIVATE KEY-----"
for secret in \
  "$private_key" \
  'glc_''12345678901234567890123456789012' \
  'sk_live_''123456789012345678901234' \
  'sk_test_''123456789012345678901234' \
  'AIza''12345678901234567890123456789012345'; do
  printf 'credential = "%s"\n' "$secret" > "$content_secrets/web/src/credential-fixture.ts"
  if python3 "$validator" --export "$content_secrets" >/dev/null 2>&1; then
    printf 'error: validator accepted secret content: %s\n' "$secret" >&2
    exit 1
  fi
done

printf 'TOKEN=%s\n' "$secret" > "$content_secrets/web/src/.env.example"
if python3 "$validator" --export "$content_secrets" >/dev/null 2>&1; then
  printf 'error: validator accepted a realistic token in .env.example\n' >&2
  exit 1
fi

examples="$tmp_dir/explicit-examples"
bad_baseline="$tmp_dir/bad-baseline-lock"
cp -a "$export_dir" "$bad_baseline"
python3 - "$bad_baseline/.ziggy/nanobot.lock.json" <<'PY'
import json
import sys

path = sys.argv[1]
data = json.loads(open(path, encoding="utf-8").read())
data["upstream_baseline"]["commit"] = "1" * 40
open(path, "w", encoding="utf-8").write(json.dumps(data, indent=2, sort_keys=True) + "\n")
PY
if python3 "$validator" --export "$bad_baseline" >/dev/null 2>&1; then
  printf 'error: validator accepted a mismatched upstream baseline pin\n' >&2
  exit 1
fi

bad_effective="$tmp_dir/bad-effective-lock"
cp -a "$export_dir" "$bad_effective"
python3 - "$bad_effective/.ziggy/nanobot.lock.json" <<'PY'
import json
import sys

path = sys.argv[1]
data = json.loads(open(path, encoding="utf-8").read())
data["effective_runtime"]["commit"] = "2" * 40
open(path, "w", encoding="utf-8").write(json.dumps(data, indent=2, sort_keys=True) + "\n")
PY
if python3 "$validator" --export "$bad_effective" >/dev/null 2>&1; then
  printf 'error: validator accepted a mismatched effective runtime pin\n' >&2
  exit 1
fi


cp -a "$export_dir" "$examples"
mkdir -p "$examples/web/src"
example_token='glc_'"EXAMPLE_PLACEHOLDER"
printf 'credential = "%s"\n' "$example_token" > "$examples/web/src/credential-fixture.example.ts"
python3 "$validator" --export "$examples" >/dev/null

printf 'split_validation_tests=passed\n'
