# TestFlight Pilot Runbook

Status: preparation only. Do not invite external testers until the tenant release
gate below passes.

This runbook covers a private Ziggy pilot. Apple TestFlight distribution and
Ziggy account admission are independent controls: a tester must be invited in
both places, and removing a tester from TestFlight does not revoke Ziggy access.

## Release Gate

Before uploading an external build:

- Replace owner/guest bootstrap with authenticated user sessions.
- Resolve every request to a server-owned user and workspace mapping.
- Give every tester a separate Nanobot runtime and filesystem root.
- Pass the two-user REST, SSE, WebSocket, session, file, context, and memory
  leakage matrix in
  [external-testers-tenancy-testflight.md](../research/external-testers-tenancy-testflight.md).
- Enforce one active turn per user, a bounded global queue, request limits,
  cancellation, disable, and deletion.
- Confirm logs, traces, metrics, crashes, and feedback contain no prompts,
  tokens, email addresses, Clerk subjects, or workspace identifiers.
- Keep integrations, Work, cron, shell execution, arbitrary MCP, uploads, and
  remote media disabled for the first pilot.

The current owner-only build does not pass this gate and must not be shared.

## Apple Prerequisites

1. Confirm the Apple Developer Program membership for team `98KW2QQ963` and
   App Store Connect access with permission to create apps, upload builds, and
   manage TestFlight.
2. Sign in to that team in Xcode under **Settings > Accounts** and let Xcode
   manage distribution signing.
3. Register the explicit App ID `com.mihaichiorean.ziggy` if it does not already
   exist.
4. Create the App Store Connect app record with the exact bundle ID
   `com.mihaichiorean.ziggy`, a unique SKU, the primary language, and the Ziggy
   display name.
5. Add the privacy policy URL, feedback email, beta description, and review
   contact. Complete app privacy and export-compliance answers from the actual
   shipped data-flow inventory.

Apple references:

- [Upload builds](https://developer.apple.com/help/app-store-connect/manage-builds/upload-builds)
- [TestFlight overview](https://developer.apple.com/help/app-store-connect/test-a-beta-version/testflight-overview/)
- [Invite external testers](https://developer.apple.com/help/app-store-connect/test-a-beta-version/invite-external-testers)

## Local Preflight

Run from `ios/` after the release-gate changes have landed:

```sh
xcodegen generate
xcodebuild -project Ziggy.xcodeproj \
  -scheme Ziggy \
  -configuration Release \
  -destination 'generic/platform=iOS' \
  -showBuildSettings \
  | grep -E 'PRODUCT_BUNDLE_IDENTIFIER|DEVELOPMENT_TEAM|MARKETING_VERSION|CURRENT_PROJECT_VERSION'

security find-identity -v -p codesigning
```

Expected values:

```text
PRODUCT_BUNDLE_IDENTIFIER = com.mihaichiorean.ziggy
DEVELOPMENT_TEAM = 98KW2QQ963
```

Use `MARKETING_VERSION` for the user-visible version. Increment
`CURRENT_PROJECT_VERSION` for every upload; App Store Connect rejects a reused
build number.

## Archive And Internal Smoke Test

Archive with automatic signing:

```sh
rm -rf build/Ziggy.xcarchive
xcodebuild -project Ziggy.xcodeproj \
  -scheme Ziggy \
  -configuration Release \
  -destination 'generic/platform=iOS' \
  -archivePath build/Ziggy.xcarchive \
  -allowProvisioningUpdates \
  archive
```

Open the archive in Xcode Organizer and use **Distribute App > App Store
Connect > Upload**. The Organizer flow is preferred for the first upload
because it exposes signing, entitlement, validation, privacy manifest, and
export-compliance failures directly.

After processing, add the build to an internal group containing only trusted
App Store Connect users. On a TestFlight-installed build, verify:

- Fresh sign-in, cancel, sign-out, token refresh, and account switch.
- REST history, live SSE/WebSocket output, reconnect, and force-quit recovery.
- Two real accounts receive different workspaces and cannot address each
  other's sessions or streams.
- Disable and deletion take effect without an app update.
- Server overload returns a bounded retryable response.
- App version/build and a content-free correlation ID are visible for support.

## External Pilot

1. Create a private external group named `Ziggy Pilot`; do not enable a public
   link.
2. Add the processed build, complete **What to Test**, review contact, and test
   instructions, then submit the first external build for Beta App Review.
3. For each tester, create the Ziggy invitation first but leave it disabled.
4. Add the same email to the private TestFlight group.
5. Activate the Ziggy invitation only after the tester confirms the installed
   build reaches the sign-in screen.
6. Start with three users. Monitor isolation failures, queue pressure, model
   latency, runtime restarts, memory growth, crashes, and feedback before
   expanding the cohort.

Pending tester email addresses are operational data and belong in the local
operator store, not in Git. The current local staging file is
`~/.config/ziggy/testers/pending.csv` with mode `0600`.

## Rollback And Offboarding

- Remove the build from the external group to stop new installs.
- Disable the Ziggy account to revoke application access; do not rely on
  TestFlight removal.
- Close active streams, revoke sessions, stop the user's runtime, and cancel
  queued work.
- Use the tenant deletion inventory to remove the workspace, sessions, memory,
  media, derived indexes, runtime state, and credentials without touching
  another tenant.
- Keep the previous server and iOS build deployable until migration, restore,
  and deletion checks pass for the new version.
