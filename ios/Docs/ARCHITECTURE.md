# Ziggy for iOS Architecture

## Deployment target

- Swift 6 and SwiftUI.
- iOS 18 minimum, tested primarily on iOS 26 and iPhone 17 Pro.
- ClerkKit and ClerkKitUI provide native authentication.
- Textual `StructuredText` renders Markdown block structure, selection, and
  dynamic type. Existing link, image, and HTML safety gates run before
  structured rendering.
- XcodeGen owns deterministic project generation through `project.yml`.

## Layers

### App

`ZiggyApp` configures Clerk, builds the dependency graph, and owns scene
lifecycle. `AppModel` is the V1 main-actor state machine for authentication,
routing, conversation state,
work state, global connection status, and persisted appearance preference. Its
transport-facing methods are small enough to extract into feature stores when
local persistence arrives.

### Core models

`Models` contains Codable wire payloads and small domain models. Wire types do
not import SwiftUI. UI-specific formatting stays in feature modules. The
versioned `RichContentMessage` model preserves unknown blocks as placeholders;
`LegacyContentAdapter` maps current string/JSON messages and progress/tool
metadata without interpreting HTML. The current production Nanobot path uses
that legacy adapter for markdown, progress, and media presentation; it does not
assume that the backend emits typed blocks. `rich_content_v1` remains disabled
unless the bootstrap response explicitly advertises it.

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

- Clerk owns the persisted native identity session.
- `CredentialStore` stores only the selected server URL using
  `WhenUnlockedThisDeviceOnly` Keychain accessibility.
- Short-lived `nbwt_` tokens remain in memory and are refreshed for reconnects.
- `GET /auth/bootstrap` receives a fresh Clerk session JWT in its
  `Authorization` header and resolves tenancy on the server.
- REST sends tokens in the `Authorization` header.
- WebSocket handshakes send short-lived credentials in the `Authorization`
  header. Credentials are never placed in WebSocket URLs.

### Features

- `Chat`: conversation list, transcript, streaming reducer, composer, media.
- `Work`: task list, detail timeline, cancellation, follow-up.
- `Settings`: identity, sign-out, endpoint, and transport diagnostics.
- `Speech`: permission and dictation service behind a protocol.

The SwiftUI design system mirrors the PWA's neutral light/dark tokens and
compact interaction hierarchy without embedding the web client.

UI state is held by a `@MainActor @Observable` reference type. Transport actors
emit `AsyncStream` values that are reduced into stable view state.

## Production gateway contract

### Bootstrap and REST

- `GET /auth/bootstrap` with a Clerk bearer token
- `GET /api/sessions`
- `GET /api/sessions/{key}/messages`
- `GET /api/work`
- `GET /api/work/{task_id}`
- `GET /api/work/{task_id}/events?after_seq=...`
- `GET /api/settings` when authorized by server policy

REST requests use `Authorization: Bearer <short-lived token>`.
The Clerk JWT and Ziggy credentials are sent only in authorization headers and
never in URLs.

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
- Connection, bootstrap-refresh, and REST results are generation-fenced so an
  account switch cannot apply stale credentials or tenant data to the new
  session. Old sockets cannot request credentials for a newer account.
- A socket drop moves the app to reconnecting and retains unsent frames.
- Exponential reconnect backoff is bounded and reset after a successful
  ping/pong confirms the WebSocket upgrade.
- REST credentials refresh shortly before their advertised expiry.
- A malformed additive event surfaces a diagnostic without dropping the
  otherwise healthy WebSocket.
- Unit tests do not depend on Spark. The separate `ZiggyLive` scheme is the
  only test that reaches the production gateway.

## Test strategy

- Unit tests for SSE framing, WebSocket envelopes, REST decoding, and reducers.
- Simulator smoke run and screenshots at iPhone 17 Pro dimensions.
- A manually enabled XCUITest round trip against `chat.mihaichiorean.com`.
