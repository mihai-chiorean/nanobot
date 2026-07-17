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

Select the `Ziggy` scheme and an iPhone simulator or signed device. On first
launch, enter the private Ziggy access code. The default server is
`https://chat.mihaichiorean.com`.

The app stores the enrollment code in Keychain. Ephemeral bootstrap and socket
tokens remain in memory.

## Tests

```sh
xcodebuild test \
  -project Ziggy.xcodeproj \
  -scheme Ziggy \
  -destination 'platform=iOS Simulator,name=iPhone 17 Pro'
```

The live round-trip test is intentionally separate:

```sh
xcodebuild test \
  -project Ziggy.xcodeproj \
  -scheme ZiggyLive \
  -destination 'platform=iOS Simulator,name=iPhone 17 Pro' \
  ZIGGY_GUEST_CODE="$ZIGGY_GUEST_CODE" \
  ZIGGY_SERVER_URL="https://chat.mihaichiorean.com"
```

See `Docs/ARCHITECTURE.md`, `Docs/FEATURES.md`, and
`Docs/AUTHENTICATION.md` for protocol and design decisions.
