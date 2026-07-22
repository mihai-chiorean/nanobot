# Ziggy Connectors Implementation

Status: Phase 0/1 foundation

This document records what is implemented in `services/ziggy-connectors` and
the boundary to the main agent's tenant identity/runtime work.

## Implemented boundary

The service is a private Go HTTP service. The only accepted caller identity is
a short-lived, HMAC-signed principal header produced by the private gateway:

```text
X-Ziggy-Principal: base64url({user_id, workspace_id, expires_at})
X-Ziggy-Principal-Signature: base64url(HMAC-SHA256(principal, trust-key))
```

The connector fails closed when either header is absent, malformed, expired, or
not signed by its configured trust key. Clients cannot select a user or
workspace by supplying an unsigned ID. The gateway must therefore strip these
headers from untrusted inbound traffic and add its own signed values after
server-side identity resolution.

OAuth start requires this principal. It creates a tenant-scoped transaction
containing an encrypted PKCE verifier, then returns a Google authorization URL
with the exact configured redirect URI, `S256` PKCE, a signed state, and the
minimum Phase 1 scopes. The state claims include the stable user ID,
workspace ID, transaction ID, nonce, and expiry. Callback processing verifies
the signature and expiry before looking up the transaction with both tenant
columns, atomically consumes it, decrypts the verifier, exchanges the code,
validates both Google OpenID userinfo and the Gmail profile, and persists an
encrypted refresh token. A second callback is rejected as replay.

The persisted account model contains the internal tenant pair, provider
subject, email, scopes, status, and encrypted refresh token. The account status
endpoint never returns ciphertext or tokens. The AES-256-GCM implementation is
behind `crypto.Cipher`, leaving a KMS/envelope-encryption implementation as a
provider replacement without changing repositories or handlers. AES-GCM
associated data binds each encrypted PKCE verifier and refresh token to its
tenant and transaction/account ID, so ciphertext moved between rows or tenants
fails authentication. The current
key file is the local/initial production boundary; production deployment must
manage rotation and old key versions before rotating `TokenKeyVersion`.

The PostgreSQL migration and repository use `(user_id, workspace_id, ...)` in
primary/unique keys and in every account/transaction query. `account_id`,
`transaction_id`, and state hashes are never treated as globally authorized
identifiers. The memory repository exists for local development and tests only.

## Provider surface

`provider.Gmail` is intentionally narrow:

- construct an authorization URL;
- exchange a code with a PKCE verifier and exact redirect URI;
- validate the Google profile.

The Google adapter uses HTTPS endpoints and never logs request headers, codes,
access tokens, refresh tokens, or response bodies. The fake adapter supports
HTTP contract tests. There is no Gmail mutation method, `gmail.modify` scope,
`https://mail.google.com/` scope, `messages.delete`, or `batchDelete` in this
foundation.

## Health and shutdown

`/healthz` is a process check and `/readyz` checks the repository. `cmd` uses a
signal context and `http.Server.Shutdown` with a bounded timeout. `Serve` is
also directly testable with a canceled context, which keeps shutdown behavior
independent of process signal tests.

## Nanobot scheduler decision

The current checked-in Nanobot cron service is file-backed JSON under one
instance workspace and executes due jobs sequentially in-process. It has no
durable tenant key, PostgreSQL lease/fence, cross-process uniqueness, or
connector credential boundary. The supplied runtime snapshot adds
`schedule_work` and `work` concepts, but those changes still sit on top of the
same Nanobot cron/runtime process and are not a multi-tenant durable scheduler.

**Decision: do not reuse either implementation for Gmail jobs now.** No
connector credential, Gmail account ID, message cursor, or tenant-scoped job
should be placed in Nanobot cron payloads or workspace files. This keeps stock
Nanobot unchanged and avoids treating an uncommitted runtime snapshot as a
product contract.

The later integration should be a Go-owned, tenant-aware Work boundary:

1. `ziggy-work` stores schedules, runs, retry state, approvals, cancellation,
   usage, and audit records in PostgreSQL, with tenant identity on every key.
2. A PostgreSQL-backed worker system such as River claims fenced runs and
   invokes connector methods with an expiring workspace capability.
3. `ziggy-connectors` performs the bounded Gmail read and returns sanitized,
   tenant-local data or a durable result reference. It never gives a provider
   token to Nanobot, Spark, a model prompt, or job arguments.
4. Stock Nanobot may later call a capability-scoped connector MCP endpoint for
   interactive read-only requests. Its configured HTTP headers and
   `enabledTools` allowlist are useful transport controls, but they are not the
   authoritative tenant or credential check; the connector remains the
   enforcement point.

The current Phase 0/1 service therefore stops at account linkage and status.
Adding live schedules requires the main agent's stable tenant mapping,
workspace runtime generation/fencing, durable Work service, encrypted
content-safe persistence, and restricted-scope Google verification/security
assessment gates described by the parent architecture documents.

## Integration steps

1. `ziggy-control` now resolves Clerk subject to internal user/workspace,
   strips inbound principal headers, and signs a one-minute private principal
   for protected `/connectors/*` routes. Configure the same trust key in both
   services through their `ZIGGY_CONNECTORS_TRUST_KEY_FILE` settings.
2. Register the exact value of `ZIGGY_CONNECTORS_GOOGLE_REDIRECT_URI` in Google
   Cloud Console. No client-provided redirect URI is accepted.
3. Run the migration through the deployment migration process and provide
   `ZIGGY_CONNECTORS_DATABASE_URL_FILE` in staging/production. The current
   binary uses memory storage only when no database URL is configured.
4. Before external pilot enablement, complete restricted-scope verification,
   security assessment, deletion/offboarding behavior, and two-real-account
   tenant isolation tests. Gmail is feature-flagged off until those gates are
   satisfied.
5. Add the Go Work/worker boundary and a narrow MCP server in a later change;
   keep those additions outside this Phase 0 account-linking contract until the
   runtime owner supplies capability and generation validation APIs.
