# Authentication

## Identity flow

The app uses Clerk's native iOS SDK. `AuthView` presents the methods enabled in
the Clerk application, Clerk persists its own session, and the app requests a
fresh Clerk session token when it needs to bootstrap Ziggy.

1. The user signs in through Clerk.
2. The app sends the Clerk session JWT in the `Authorization` header to
   `GET /auth/bootstrap`.
3. `ziggy-control` verifies the JWT and resolves the signed Clerk subject to a
   server-owned user and workspace. Email is admission/profile data, not the
   tenant key.
4. The gateway returns short-lived REST and WebSocket credentials scoped to
   that resolved runtime.
5. The app keeps Ziggy credentials in memory. Refresh is serialized and one
   authenticated REST request may be retried after a `401`.

The app never accepts a user, tenant, or workspace ID from the client as an
authorization decision. It does not compare an email locally, ship a guest
code, or reuse the Clerk JWT as a long-lived Ziggy socket credential.

## Configuration

The Xcode build setting `CLERK_PUBLISHABLE_KEY` is injected into Info.plist as
`ZiggyClerkPublishableKey`. Put the publishable key in the ignored
`Config/Local.xcconfig`; do not commit deployment-specific configuration.

The Clerk Native API application must register:

- App ID prefix: `98KW2QQ963`
- Bundle ID: `com.mihaichiorean.ziggy`
- Callback: `com.mihaichiorean.ziggy://callback`

`ZiggyApp` forwards callback URLs to Clerk and observes Clerk session changes
so sign-in, sign-out, expiry, and account switching rebuild the Ziggy
connection. Initial launch is handled once by `AppModel.start()` to avoid
creating duplicate sockets when Clerk restores an existing session.

Social login methods and Gmail connector authorization are separate grants.
Signing in with Google does not grant Ziggy access to Gmail, and disconnecting
Gmail does not sign the user out of Ziggy.

## Server policy

Clerk proves identity; `ziggy-control` controls admission and tenancy. Every
TestFlight tester needs an enabled server-side mapping to a dedicated workspace
and runtime. Unknown or disabled identities fail closed.
