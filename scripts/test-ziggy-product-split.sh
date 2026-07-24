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
if [[ -e "$export_dir/web/bridge" ]]; then
  printf 'error: product export contains the unowned WhatsApp bridge\n' >&2
  exit 1
fi

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
for filename in .env .env.local .env.production.local .env-example .env_example; do
  printf 'REAL_SECRET=not-an-example\n' > "$nested_env/web/src/config/$filename"
  if python3 "$validator" --export "$nested_env" >/dev/null 2>&1; then
    printf 'error: validator accepted a nested environment file: %s\n' "$filename" >&2
    exit 1
  fi
  rm "$nested_env/web/src/config/$filename"
done

content_secrets="$tmp_dir/content-secrets"
cp -a "$export_dir" "$content_secrets"
mkdir -p "$content_secrets/web/src"
while IFS= read -r secret; do
  printf 'credential = "%s"\n' "$secret" > "$content_secrets/web/src/credential-fixture.ts"
  if python3 "$validator" --export "$content_secrets" >/dev/null 2>&1; then
    printf 'error: validator accepted secret content: %s\n' "$secret" >&2
    exit 1
  fi
done < <(python3 - <<'PY'
print("-----BEGIN " + "PRIVATE KEY-----")
print("glc_" + "a1" * 16)
print("sk_live_" + "a1" * 12)
print("sk_test_" + "b2" * 12)
print("AIza" + "A1" * 17 + "A")
print("postgresql://release_user:" + "S3cureDatabaseCredential!" + "@db.example.test:5432/ziggy")
print("ghp_" + "a1" * 18)
print("github_pat_" + "A1_" * 28)
print("AKIA" + "A1" * 8)
print("xoxb-" + "a1" * 10)
print("sk-ant-api03-" + "a1" * 20)
print("sk-proj-" + "a1" * 20)
print("glpat-" + "a1" * 10)
print("npm_" + "a1" * 18)
print("SG." + "a1" * 8 + "." + "b2" * 16)
PY
)

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

bad_deployment="$tmp_dir/bad-independent-deployment"
cp -a "$export_dir" "$bad_deployment"
python3 - \
  "$bad_deployment/.ziggy/nanobot.lock.json" \
  "$bad_deployment/.ziggy/export-metadata.json" <<'PY'
import json
import sys

for path in sys.argv[1:]:
    data = json.loads(open(path, encoding="utf-8").read())
    runtime = (
        data["effective_runtime"]
        if "effective_runtime" in data
        else data["nanobot_dependency"]["effective_runtime"]
    )
    runtime["independent_deployment"]["deployable"] = True
    runtime["independent_deployment"]["status"] = "ready"
    open(path, "w", encoding="utf-8").write(json.dumps(data, indent=2, sort_keys=True) + "\n")
PY
if python3 "$validator" --export "$bad_deployment" >/dev/null 2>&1; then
  printf 'error: validator accepted an independently deployable source export\n' >&2
  exit 1
fi

source_commit=$(git -C "$source_dir" rev-parse HEAD)
clone="$tmp_dir/separate-clone"
git clone --quiet --no-local "$source_dir" "$clone"
git -C "$clone" checkout --quiet -b review/deterministic-export "$source_commit"
git -C "$clone" remote set-url origin https://github.com/mihai-chiorean/nanobot.git
branch_export="$tmp_dir/branch-export"
"$clone/scripts/export-ziggy-product.sh" --source "$clone" --destination "$branch_export" >/dev/null

git -C "$clone" checkout --quiet --detach "$source_commit"
git -C "$clone" remote set-url origin git@github.com:mihai-chiorean/nanobot.git
detached_export="$tmp_dir/detached-export"
"$clone/scripts/export-ziggy-product.sh" --source "$clone" --destination "$detached_export" >/dev/null

tree_id() {
  local tree=$1
  git -C "$tree" add --all --force
  git -C "$tree" write-tree
}

expected_tree=$(tree_id "$export_dir")
branch_tree=$(tree_id "$branch_export")
detached_tree=$(tree_id "$detached_export")
if [[ "$expected_tree" != "$branch_tree" || "$expected_tree" != "$detached_tree" ]]; then
  printf 'error: export changes across clone, branch, detached HEAD, or remote URL forms\n' >&2
  printf 'expected=%s branch=%s detached=%s\n' \
    "$expected_tree" "$branch_tree" "$detached_tree" >&2
  exit 1
fi


cp -a "$export_dir" "$examples"
mkdir -p "$examples/web/src"
example_token='glc_'"EXAMPLE_PLACEHOLDER"
printf 'credential = "%s"\n' "$example_token" > "$examples/web/src/credential-fixture.example.ts"
python3 "$validator" --export "$examples" >/dev/null

printf 'split_validation_tests=passed\n'
