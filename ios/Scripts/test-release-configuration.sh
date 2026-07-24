#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
validator="$script_dir/validate-release-configuration.sh"
live_key="pk_live_""release_validation_fixture"

expect_failure() {
  if env "$@" "$validator" >/dev/null 2>&1; then
    printf 'error: Release configuration validator accepted invalid input: %s\n' "$*" >&2
    exit 1
  fi
}

env CONFIGURATION=Debug CLERK_PUBLISHABLE_KEY= CURRENT_PROJECT_VERSION= "$validator"
expect_failure CONFIGURATION=Release CLERK_PUBLISHABLE_KEY= CURRENT_PROJECT_VERSION=42 ZIGGY_ARCHIVE_BUILD_NUMBER=42
expect_failure CONFIGURATION=Release CLERK_PUBLISHABLE_KEY=pk_test_validation_fixture CURRENT_PROJECT_VERSION=42 ZIGGY_ARCHIVE_BUILD_NUMBER=42
expect_failure CONFIGURATION=Release CLERK_PUBLISHABLE_KEY="$live_key" CURRENT_PROJECT_VERSION=2
expect_failure CONFIGURATION=Release CLERK_PUBLISHABLE_KEY="$live_key" CURRENT_PROJECT_VERSION=1 ZIGGY_ARCHIVE_BUILD_NUMBER=1
expect_failure CONFIGURATION=Release CLERK_PUBLISHABLE_KEY="$live_key" CURRENT_PROJECT_VERSION=2 ZIGGY_ARCHIVE_BUILD_NUMBER=3
expect_failure CONFIGURATION=Release CLERK_PUBLISHABLE_KEY="$live_key" CURRENT_PROJECT_VERSION=1.2 ZIGGY_ARCHIVE_BUILD_NUMBER=1.2
env \
  CONFIGURATION=Release \
  CLERK_PUBLISHABLE_KEY="$live_key" \
  CURRENT_PROJECT_VERSION=42 \
  ZIGGY_ARCHIVE_BUILD_NUMBER=42 \
  "$validator"

printf 'release_configuration_tests=passed\n'
