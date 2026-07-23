# Ziggy iOS and Swift Modernization

Date: 2026-07-23

## Baseline

The production branch builds with:

- Xcode 26.6 (`17F113`)
- Apple Swift 6.3.3 in Swift 6 language mode
- iOS 26.5 SDK
- iOS 18.0 minimum deployment target
- Complete strict-concurrency checking

Xcode 27 is beta and is not a production baseline. `SWIFT_VERSION = 6.0`
selects Swift 6 language mode; it should not be changed to `6.3`.

References:

- [Xcode 26.6 release notes](https://developer.apple.com/documentation/Xcode-Release-Notes/xcode-26_6-release-notes)
- [Swift 6.3 release](https://www.swift.org/blog/swift-6.3-released/)
- [Approachable concurrency](https://developer.apple.com/videos/play/wwdc2025/268/)

## Decisions

### Testing

XCTest is not deprecated. Swift Testing is the default for in-process unit and
integration tests. XCTest remains the correct framework for UI automation,
performance tests, and APIs such as `XCUIApplication` and `XCTAttachment`.

The Ziggy unit suite uses Swift Testing. Suites that share process-global test
stubs are explicitly serialized because Swift Testing executes tests in
parallel by default. The live UI suite remains XCTest.

### Concurrency

Approachable Concurrency is enabled alongside complete strict-concurrency
checking. Module-wide MainActor isolation is deliberately not enabled. The
current app target mixes SwiftUI code with JSON models, Keychain access, SSE,
REST, and WebSocket actors; default MainActor isolation should follow a module
split or an explicit isolation audit.

Outbound WebSocket frames use one actor-owned FIFO drain. Independent
unstructured send tasks could reorder frames after suspension and requeue
failures out of submission order.

### Generated project ownership

`ios/project.yml` is the source of truth for targets, packages, build settings,
and schemes. XcodeGen 2.46.0 and Xcode 26.6 are pinned as generation metadata.
The checked-in custom `Info.plist` owns app metadata and permission strings;
generated-plist settings must not duplicate it.

## Deferred Work

1. Split UI and non-UI code into separate modules before considering default
   MainActor isolation.
2. Replace timing-based WebSocket lifecycle checks with deterministic clocks
   where the test surface justifies the added abstraction.
3. Add fixture-driven UI tests for signed-out, conversation, rich-content,
   Work, and connector states. Keep the authenticated live test as a separate
   integration gate.
4. Remove one duplicate Swift 6.2.1 toolchain installation from the developer
   machine. It currently produces a warning but does not affect Xcode's
   selected Swift 6.3.3 compiler.
5. Use a Clerk production instance and `pk_live_` publishable key for
   TestFlight. The current Clerk development instance is not a release
   identity environment.
