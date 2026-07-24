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
archive_build_number=${ZIGGY_ARCHIVE_BUILD_NUMBER:-}
if [[ ! "$archive_build_number" =~ ^([2-9]|[1-9][0-9]+)$ ]]; then
  printf 'error: Release builds require an explicit ZIGGY_ARCHIVE_BUILD_NUMBER greater than 1\n' >&2
  exit 1
fi
if [[ "$build_number" != "$archive_build_number" ]]; then
  printf 'error: CURRENT_PROJECT_VERSION must match ZIGGY_ARCHIVE_BUILD_NUMBER\n' >&2
  exit 1
fi

printf 'release_configuration=passed\n'
