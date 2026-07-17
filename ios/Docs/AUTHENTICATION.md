# Authentication

## Current personal build

The iOS app has one fixed principal:

- Name: Mihai Chiorean
- Email: `mihai.v.chiorean@gmail.com`

This identity is presentation and policy context, not a credential supplied by
the phone. The server remains the authority. The installation is enrolled with
a private guest code, stores that code in Keychain, and exchanges it for
short-lived `nbwt_` credentials. Those credentials are never committed or
persisted by the app.

All conversations, work, and memory continue to use Ziggy's existing personal
namespace. V1 intentionally has no per-user data partitioning.

## Google sign-in follow-up

Do not implement this as a client-side email comparison. A production flow
needs these pieces:

1. The app completes Google OIDC with the native SDK or an
   `ASWebAuthenticationSession` authorization-code flow using PKCE.
2. The server verifies signature, issuer, audience, expiry, nonce, and
   `email_verified` on the returned identity.
3. The server allowlists both the stable Google `sub` and
   `mihai.v.chiorean@gmail.com`.
4. Ziggy issues its own revocable device/session credential. The Google token
   is not used as the Ziggy WebSocket credential.
5. The existing `CredentialStoring` and bootstrap boundary swaps from private
   code enrollment to that server-issued credential; chat and work code remain
   unchanged.

Cloudflare's browser owner challenge is not a native authentication API. The
gateway needs a dedicated native exchange endpoint before the private code can
be retired.
