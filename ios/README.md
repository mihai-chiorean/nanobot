# Ziggy iOS

Native SwiftUI client for the personal Ziggy deployment.

## Requirements

- Xcode 26 or newer
- XcodeGen (`brew install xcodegen`)
- iOS 17 or newer

## Open and run

```sh
cd ios
xcodegen generate
open Ziggy.xcodeproj
```

Create an ignored local configuration from the example and add the Clerk
publishable key for the deployment:

```sh
cp Config/Local.xcconfig.example Config/Local.xcconfig
```

Select the `Ziggy` scheme and an iPhone simulator or signed device. On first
launch, sign in with a method enabled for the Clerk application. The default
server is `https://chat.mihaichiorean.com`.

Clerk owns the persisted identity session. Ziggy stores only the selected
server URL in Keychain; the gateway's short-lived bootstrap and socket tokens
remain in memory.

## Tests

```sh
xcodebuild test \
  -project Ziggy.xcodeproj \
  -scheme Ziggy \
  -destination 'platform=iOS Simulator,name=iPhone 17 Pro'
```

The live round-trip test is intentionally separate. Sign in to Ziggy once on
the target simulator, then run:

```sh
xcodebuild test \
  -project Ziggy.xcodeproj \
  -scheme ZiggyLive \
  -destination 'platform=iOS Simulator,name=iPhone 17 Pro' \
  ZIGGY_LIVE_SESSION_READY=1 \
  ZIGGY_SERVER_URL="https://chat.mihaichiorean.com"
```

The test skips unless `ZIGGY_LIVE_SESSION_READY=1`; it never accepts a shared
guest or owner credential.

See `Docs/ARCHITECTURE.md`, `Docs/FEATURES.md`, and
`Docs/AUTHENTICATION.md` for protocol and design decisions.
