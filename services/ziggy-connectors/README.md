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
| `ZIGGY_CONNECTORS_OAUTH_ISSUER_URL` | Loopback OAuth authorization-server issuer; production default `https://127.0.0.1:8790` |
| `ZIGGY_CONNECTORS_MCP_RESOURCE_URL` | Canonical loopback MCP resource URL; production default `https://127.0.0.1:8790/mcp` |
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
| `ZIGGY_CONNECTORS_CLIENT_CREDENTIAL_PEPPER_FILE` | At least 32 random bytes used to hash persisted runtime client secrets |
| `ZIGGY_CONNECTORS_MCP_ACCESS_SIGNING_KEY_FILE` | At least 32 random bytes used to sign five-minute MCP access tokens |
| `ZIGGY_CONNECTORS_DATABASE_URL_FILE` | PostgreSQL URL; required in production, optional for local memory storage |
| `ZIGGY_CONNECTORS_TLS_CERT_FILE` | Loopback TLS server certificate; required in production |
| `ZIGGY_CONNECTORS_TLS_KEY_FILE` | Loopback TLS private key; required in production |

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

## Runtime MCP OAuth

Interactive `/accounts` and `/oauth/google/start` retain the signed control
principal boundary. Runtime MCP access is separate: `POST /oauth/token` accepts
only HTTP Basic runtime client credentials and `grant_type=client_credentials`.
The service derives the user, workspace, runtime ID, and runtime generation
from the persisted client record; it rejects tenant identity request fields.

The direct production endpoints are `https://127.0.0.1:8790/oauth/token` and
`https://127.0.0.1:8790/mcp`. Run `deploy/generate-local-tls.sh` once on the
Spark host to create the loopback certificate and install its local CA in the
system trust store. The server publishes RFC 9728 protected-resource
metadata at `/.well-known/oauth-protected-resource/mcp` and OAuth authorization
server metadata at `/.well-known/oauth-authorization-server`. An unauthenticated
MCP request returns the `resource_metadata` bearer challenge and scope guidance
expected by MCP OAuth clients. The RFC 8707 `resource` token-request parameter
is required and must equal the configured MCP resource URL.

MCP access tokens are connector-only HS256 JWTs with the configured issuer,
the MCP resource URL as audience, `iat`, `exp`, random `jti`, and granted
scopes. They expire after five minutes. `/mcp` accepts only these bearer tokens,
and advertises `io.modelcontextprotocol/oauth-client-credentials` during MCP
initialization. Google access and refresh tokens never reach Nanobot.

The server publishes three read-only tools:

- `gmail_connection_status`
- `gmail_search`
- `gmail_get_message`

Search is capped at 20 messages per call. Message reads omit attachments and
cap the extracted text body at 64 KiB. Access tokens are cached only until
shortly before expiry, refreshes are coalesced, and Gmail calls are limited per
tenant. Every result labels email content as untrusted external data.

`gmail.status`, `gmail.search`, and `gmail.read` are independently enforced by
the token's scopes. A token exposes only the corresponding status, search, and
message-read tools.

### Provision a runtime client

Apply both connector migrations, then provision one client per
`(user_id, workspace_id, runtime_id, runtime_generation)`. Create the
mode-`0600` source credential as the runtime's Unix user before invoking the
provisioner. The provisioner reads it, persists only its peppered hash, and
prints no secret. Re-running it for the same runtime fence is idempotent.
Generation replacement and secret rotation deliberately fail closed until the
orchestrator can stage and health-check a second credential.

```sh
install -d -m 0700 /home/mihai/.config/credstore
umask 077
openssl rand -base64 48 > "/home/mihai/.config/credstore/ziggy-mcp-${ZIGGY_RUNTIME_INSTANCE}"

sudo /usr/local/bin/provision-runtime-oauth-client \
  --database-url-file /run/credentials/ziggy-connectors.service/database-url \
  --client-credential-pepper-file /run/credentials/ziggy-connectors.service/client-credential-pepper \
  --user-id "$ZIGGY_USER_ID" \
  --workspace-id "$ZIGGY_WORKSPACE_ID" \
  --runtime-id "$ZIGGY_RUNTIME_ID" \
  --runtime-generation "$ZIGGY_RUNTIME_GENERATION" \
  --secret-file "/home/mihai/.config/credstore/ziggy-mcp-${ZIGGY_RUNTIME_INSTANCE}"
```

Use the emitted `client_id` and scopes `gmail.status gmail.search gmail.read`
in the Spark Nanobot configuration. The owner instance name is `owner`; tenant
instance names are their systemd `%i` workspace IDs. The corresponding unit
loads the flat `ziggy-mcp-<instance>` source file with `LoadCredential` into
its read-only `/run/credentials/...` mount for Nanobot to consume.

For the owner runtime, install the checked-in credential drop-in before
writing a config that references `${ZIGGY_MCP_CLIENT_SECRET_FILE}`:

```sh
install -d -m 0700 /home/mihai/.config/systemd/user/nanobot-gateway.service.d
install -m 0600 \
  services/ziggy-control/deploy/systemd/spark/nanobot-gateway.service.d/40-mcp-client-credential.conf \
  /home/mihai/.config/systemd/user/nanobot-gateway.service.d/
systemctl --user daemon-reload
systemctl --user cat nanobot-gateway.service
```

The final command must show `LoadCredential=mcp-client-secret:ziggy-mcp-owner`
and `ZIGGY_MCP_CLIENT_SECRET_FILE=%d/mcp-client-secret`. Tenant runtimes get
the equivalent wiring from the checked-in `nanobot-tenant@.service` template.

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
`migrations/001_connector_foundation.sql` and
`migrations/002_runtime_oauth_clients.sql` using the deployment's migration
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
