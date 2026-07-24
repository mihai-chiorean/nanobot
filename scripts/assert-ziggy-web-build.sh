#!/usr/bin/env bash
set -euo pipefail

if (($# == 0)); then
  root=.
else
  root=$1
fi
root=$(cd "$root" && pwd -P)

test -d "$root/web/dist"
test ! -e "$root/nanobot"
grep -Fq 'path.resolve(__dirname, "./dist")' "$root/web/vite.config.ts"
if grep -Fq '../nanobot/web/dist' "$root/web/vite.config.ts"; then
  printf 'error: Vite build output still targets root nanobot/web/dist\n' >&2
  exit 1
fi

python3 "$root/scripts/validate-ziggy-product-split.py" --export "$root"
printf 'web_build_boundary=passed\n'
