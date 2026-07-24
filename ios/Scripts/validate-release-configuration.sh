#!/usr/bin/env bash
set -euo pipefail

if [[ "${CONFIGURATION:-}" != "Release" ]]; then
  exit 0
fi

publishable_key=${CLERK_PUBLISHABLE_KEY:-}
if [[ ! "$publishable_key" =~ ^pk_live_[A-Za-z0-9_-]{8,}$ ]]; then
  printf 'error: Release builds require a Clerk pk_live_ publishable key\n' >&2
  exit 1
fi

build_number=${CURRENT_PROJECT_VERSION:-}
if [[ ! "$build_number" =~ ^[1-9][0-9]*$ ]]; then
  printf 'error: Release builds require a positive integer CURRENT_PROJECT_VERSION\n' >&2
  exit 1
fi

printf 'release_configuration=passed\n'
