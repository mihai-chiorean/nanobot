# Ziggy for iOS Architecture

## Deployment target

- Swift 6 and SwiftUI.
- iOS 17 minimum, tested primarily on iOS 26 and iPhone 17 Pro.
- No third-party runtime dependencies in V1.
- XcodeGen owns deterministic project generation through `project.yml`.

## Layers

### App

`ZiggyApp` builds the dependency graph and owns scene lifecycle. `AppModel` is
the V1 main-actor state machine for enrollment, routing, conversation state,
work state, global connection status, and persisted appearance preference. Its
transport-facing methods are small enough to extract into feature stores when
local persistence arrives.

### Core models

`Models` contains Codable wire payloads and small domain models. Wire types do
not import SwiftUI. UI-specific formatting stays in feature modules.

### Networking

- `ZiggyRESTClient` performs bootstrap and snapshot requests with ephemeral
  bearer tokens.
- `ZiggyWebSocketClient` is an actor that owns one multiplexed socket,
  reconnect backoff, chat attachment, frame decoding, and queued sends.
- `SSEClient` parses OpenAI-compatible `text/event-stream` responses from
  `URLSession.AsyncBytes`.
- `AssistantStream` normalizes connected, delta, completion, and failure
  events without pretending WebSocket and SSE have identical capabilities.

WebSocket is the production Ziggy transport because chat and work are
bidirectional. SSE is available for the separate OpenAI-compatible inference
endpoint and future stateless completion surfaces.

### Security

- `CredentialStore` stores the long-lived guest enrollment code in Keychain.
- Short-lived `nbwt_` tokens remain in memory and are refreshed for reconnects.
- REST sends tokens in the `Authorization` header.
- The current server requires the token as a WebSocket query item. The client
  constructs that URL only at connection time and never logs it. A future
  server revision should accept an authorization header or initial auth frame.

### Features

- `Chat`: conversation list, transcript, streaming reducer, composer, media.
- `Work`: task list, detail timeline, cancellation, follow-up.
- `Settings`: enrollment, endpoint, diagnostics, owner-only controls.
- `Speech`: permission and dictation service behind a protocol.

The SwiftUI design system mirrors the PWA's neutral light/dark tokens and
compact interaction hierarchy without embedding the web client.

UI state is held by a `@MainActor @Observable` reference type. Transport actors
emit `AsyncStream` values that are reduced into stable view state.

## Production gateway contract

### Bootstrap and REST

- `GET /webui/guest/bootstrap?code=...`
- `GET /api/sessions`
- `GET /api/sessions/{key}/messages`
- `GET /api/work`
- `GET /api/work/{task_id}`
- `GET /api/work/{task_id}/events?after_seq=...`
- `GET /api/settings` for owner scope

REST requests use `Authorization: Bearer <short-lived token>`.

### WebSocket client frames

- `new_chat`
- `attach`
- `message`
- `work.create`
- `work.subscribe`
- `work.cancel`
- `work.message`

### WebSocket server events

- `ready`, `attached`
- `delta`, `stream_end`, `message`, `error`
- `work.created`, `work.subscribed`, `work.event`

### SSE

The optional OpenAI-compatible endpoint uses `POST /v1/chat/completions` with
`stream: true`, emits `data: <JSON>` events, and terminates with
`data: [DONE]`. SSE does not carry Ziggy work control or session attachment.

## State and failure policy

- UI state is main-actor isolated.
- A socket drop moves the app to reconnecting and retains unsent frames.
- Exponential reconnect backoff is bounded and reset after a successful open.
- REST credentials refresh shortly before their advertised expiry.
- A malformed additive event surfaces a diagnostic without dropping the
  otherwise healthy WebSocket.
- Unit tests do not depend on Spark. The separate `ZiggyLive` scheme is the
  only test that reaches the production gateway.

## Test strategy

- Unit tests for SSE framing, WebSocket envelopes, REST decoding, and reducers.
- Simulator smoke run and screenshots at iPhone 17 Pro dimensions.
- A manually enabled XCUITest round trip against `chat.mihaichiorean.com`.
