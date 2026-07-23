# ziggy-connectors

`ziggy-connectors` owns tenant-scoped Google Gmail account linking and
read-only inference access. It validates a Google identity, stores only an
encrypted refresh token, and exposes bounded Gmail tools over MCP. It does not
send, modify, archive, trash, or delete Gmail messages.

## Trust boundary

This service is private. It accepts authenticated tenant identity only from a
trusted Ziggy gateway boundary. Protected requests must contain:

- `X-Ziggy-Principal`: base64url JSON with `user_id`, `workspace_id`, and a
  future `expires_at` Unix timestamp.
- `X-Ziggy-Principal-Signature`: base64url HMAC-SHA256 over the exact principal
  header value, using the shared private-gateway key.

There is no development auth bypass. Requests without a valid signed principal
are rejected with `401`. The gateway, not a client, chooses the stable user and
workspace IDs. The OAuth callback is intentionally unauthenticated at the HTTP
layer, but it accepts only a valid signed one-use state transaction and derives
its tenant from that transaction.

The service must bind to loopback in production. `ziggy-control` exposes the
public `/connectors/*` prefix, strips it before proxying, removes spoofed
principal headers, and injects a signed principal valid for one minute.

## Configuration

Copy `.env.example` as a reference only. Do not load or commit an `.env` file
containing secrets. Non-secret environment variables are:

| Variable | Purpose |
| --- | --- |
| `ZIGGY_CONNECTORS_ENV` | `development`, `test`, `staging`, or `production` |
| `ZIGGY_CONNECTORS_LISTEN_ADDR` | HTTP listen address; default `127.0.0.1:8790` |
| `ZIGGY_CONNECTORS_VERSION` | Health response version; default `dev` |
| `ZIGGY_CONNECTORS_GOOGLE_CLIENT_ID` | Google OAuth client ID |
| `ZIGGY_CONNECTORS_GOOGLE_REDIRECT_URI` | Exact registered callback URI; no query/fragment, HTTPS in production |
| `ZIGGY_CONNECTORS_GOOGLE_AUTH_URL` | Optional Google authorization endpoint override for tests |
| `ZIGGY_CONNECTORS_GOOGLE_TOKEN_URL` | Optional Google token endpoint override for tests |
| `ZIGGY_CONNECTORS_GOOGLE_USERINFO_URL` | Optional Google userinfo endpoint override for tests |
| `ZIGGY_CONNECTORS_GOOGLE_PROFILE_URL` | Optional Gmail profile endpoint override for tests |
| `ZIGGY_CONNECTORS_GOOGLE_GMAIL_URL` | Optional Gmail API base URL override for tests |
| `ZIGGY_CONNECTORS_SHUTDOWN_TIMEOUT` | Optional positive Go duration; default `10s` |
| `ZIGGY_CONNECTORS_STATE_TTL` | Optional positive Go duration; default `10m` |

Secret values are read from files, with trailing whitespace removed:

| Variable | File content |
| --- | --- |
| `ZIGGY_CONNECTORS_GOOGLE_CLIENT_SECRET_FILE` | Google OAuth client secret |
| `ZIGGY_CONNECTORS_STATE_SIGNING_KEY_FILE` | At least 32 random bytes for OAuth state HMAC |
| `ZIGGY_CONNECTORS_TOKEN_ENCRYPTION_KEY_FILE` | Exactly 32 random bytes for AES-256-GCM |
| `ZIGGY_CONNECTORS_TRUST_KEY_FILE` | At least 32 random bytes shared with the private gateway |
| `ZIGGY_CONNECTORS_DATABASE_URL_FILE` | PostgreSQL URL; required in production, optional for local memory storage |

Do not put secrets in logs or command-line arguments. Generate keys into the
deployment secret mechanism, for example:

```sh
umask 077
openssl rand -base64 32 | tr -d '\\n' > /run/secrets/ziggy-connectors/state-signing-key
openssl rand -base64 32 | tr -d '\\n' > /run/secrets/ziggy-connectors/token-encryption-key
openssl rand -base64 32 | tr -d '\\n' > /run/secrets/ziggy-connectors/private-gateway-trust-key
```

The service requests only `openid`, `email`, and
`https://www.googleapis.com/auth/gmail.readonly`.

## Inference tools

Authenticated `POST /mcp` is a Streamable HTTP MCP endpoint. `ziggy-control`
authenticates the tenant runtime, removes its capability, and signs a
one-minute tenant principal before forwarding. Google access and refresh tokens
never reach Nanobot.

The server publishes three read-only tools:

- `gmail_connection_status`
- `gmail_search`
- `gmail_get_message`

Search is capped at 20 messages per call. Message reads omit attachments and
cap the extracted text body at 64 KiB. Access tokens are cached only until
shortly before expiry, refreshes are coalesced, and Gmail calls are limited per
tenant. Every result labels email content as untrusted external data.

## Local commands

From this directory:

```sh
go test ./...
go vet ./...
go run ./cmd/ziggy-connectors
```

Without `ZIGGY_CONNECTORS_DATABASE_URL_FILE`, local startup uses an in-memory
repository. That is suitable for tests only; accounts and OAuth transactions
are lost on restart. With PostgreSQL, apply
`migrations/001_connector_foundation.sql` using the deployment's migration
runner before starting the service.

The service listens on `127.0.0.1:8790` by default. `GET /healthz` is a
process health check, `GET /readyz` checks the configured repository, and
authenticated `GET /accounts` returns tenant-scoped status without token
material. Authenticated `GET /oauth/google/start` returns an authorization URL;
Google returns to `GET /oauth/google/callback`.

## Current limits

The PostgreSQL repository uses `database/sql` with the `pgx/v5` driver. Every
account and OAuth transaction key and query includes both `user_id` and
`workspace_id`. AES-GCM associated data also binds PKCE verifiers and refresh
tokens to their tenant and object ID, so moving ciphertext to another tenant
row fails authentication. Refresh-token decryption is not exposed through the
HTTP or MCP APIs.
