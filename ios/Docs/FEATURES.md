# Ziggy for iOS Features

## V1 implemented

### Identity and connection

- Single-user identity fixed to Mihai Chiorean and
  `mihai.v.chiorean@gmail.com`.
- Enrollment against a configurable HTTPS Ziggy server with a private access
  code stored in Keychain.
- Short-lived bootstrap tokens remain in memory and refresh before expiry or
  each WebSocket reconnect.
- Connection, authentication, queue, and decoding failures surface in the UI.

### Conversations

- Live conversation list and REST history restoration.
- New and resumed WebSocket chats with reconnect-safe attachment.
- User, assistant, progress, and streaming message presentation.
- Basic Markdown and selectable text.
- Multi-line composer with image attachments, background-work mode, and
  Apple Speech dictation.

### Work

- Task list for queued, running, waiting, completed, failed, and cancelled
  work.
- Background task creation from the conversation composer.
- Live ordered event timeline, cancellation, and follow-up messages.

### Settings and transport

- Account, server, model, connection, and transport diagnostics.
- Compact native interface using the PWA's neutral color tokens, message
  hierarchy, composer layout, and conversation-list density.
- System, light, and dark appearance modes with a persisted preference and a
  quick toggle in chat and enrollment headers.
- Remove saved enrollment and reconnect controls.
- Bidirectional Ziggy chat/work over WebSocket.
- A reusable OpenAI-compatible SSE parser and client for stateless streaming
  endpoints. SSE is not used as a replacement for Ziggy's control socket.

## Next

- Sign in with Google backed by server-side token verification and an email
  allowlist.
- APNs notifications for completed or waiting work.
- Share extension for URLs, text, and images.
- App Intent for Ask Ziggy and Action Button integration.
- Durable local history cache and outbound outbox.
- Authenticated artifact previews for background tasks.
- On-device transcription and wake-phrase experiments.

## Non-goals

- A WebView wrapper around the PWA.
- Running Ziggy's agent runtime or primary model on the phone.
- Shipping a permanent bearer token in the app bundle.
- Multi-user memory or data partitioning in the current personal build.
