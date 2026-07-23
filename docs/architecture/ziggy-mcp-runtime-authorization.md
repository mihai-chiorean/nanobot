# Ziggy MCP Runtime Authorization

Status: implemented for the Spark single-host deployment.

## Decision

Nanobot runtimes authenticate directly to `ziggy-connectors` with the stable
MCP OAuth Client Credentials extension. Each runtime generation receives a
different high-entropy client secret. The connector exchanges it for a
five-minute access token containing only that runtime's server-derived user,
workspace, generation, audience, and Gmail scopes.

This is the correct non-interactive flow for the current architecture:
Nanobot is a background service and must call MCP tools without a user in a
browser. It also removes the former reuse of Nanobot's WebSocket bootstrap
secret as a Gmail capability.

Enterprise-Managed Authorization (EMA) remains the target for centrally
provisioned third-party MCP servers. Exact EMA requires the identity provider
to issue an ID-JAG that the MCP authorization server can exchange. Clerk does
not currently provide that contract, so Ziggy must not label this phase as
EMA. The current boundary deliberately uses the related stable MCP
client-credentials extension instead.

Google OAuth is separate. It authorizes Ziggy to read one user's Gmail data;
the runtime OAuth client authorizes one Nanobot process to call Ziggy's MCP
server. Neither credential substitutes for the other.

## Data Flow

```mermaid
sequenceDiagram
    participant N as Tenant Nanobot
    participant S as systemd credential mount
    participant A as ziggy-connectors OAuth
    participant M as ziggy-connectors MCP
    participant DB as PostgreSQL
    participant G as Google Gmail API

    N->>S: Read this runtime's client secret
    N->>A: POST /oauth/token<br/>Basic client_id:secret<br/>grant_type=client_credentials
    A->>DB: Load client registration
    DB-->>A: user, workspace, runtime generation, scopes
    A-->>N: Five-minute bearer token
    N->>M: MCP request with bearer token
    M->>M: Verify signature, issuer, audience, expiry, scopes
    M->>DB: Load Gmail account for token tenant
    M->>G: Refresh/access Gmail with connector-held token
    G-->>M: Tenant Gmail data
    M-->>N: Bounded, read-only MCP result
```

`ziggy-control` is not in the runtime data path. It continues to authenticate
the iOS/web user with Clerk for interactive account linking and forwards a
one-minute signed tenant principal to `ziggy-connectors`. The removed
`/runtime/connectors/mcp` route must remain unavailable.

## Stored State

PostgreSQL stores one active runtime client for each:

```text
(user_id, workspace_id, runtime_id, runtime_generation)
```

It stores an HMAC-SHA256 hash of the client secret under a connector-only
pepper, never the secret. The current provisioner is idempotent for one
generation and refuses in-place rotation or generation replacement. A future
orchestrator must stage a second credential, health-check the replacement, and
only then revoke the old generation. Already-issued access tokens can remain
valid for at most five minutes after revocation.

The raw client secret exists only in the user's systemd credential store. A
runtime receives a read-only copy below its own `%d` credential directory and
the Nanobot config references that path through
`ZIGGY_MCP_CLIENT_SECRET_FILE`. The config contains a client ID, endpoint,
scopes, and secret-file path, but no bearer or client secret.

The root-only Spark snapshot includes only `ziggy-mcp-*` credential source
files in a mode-restricted archive alongside the connector database dump, so a
restore retains matching secret/hash pairs. It also includes tenant runtime
configs, user systemd units, and all of `/etc/ziggy`, including the application
environment and credential source files required to restore encrypted
connector rows and the local HTTPS boundary. Snapshot storage must remain
root-only.

Connector-held Google refresh tokens remain encrypted with tenant-bound
AES-GCM associated data. They are never returned through OAuth, MCP, Nanobot,
the model prompt, or logs.

## Authorization Contract

The connector publishes:

- `/.well-known/oauth-protected-resource/mcp`
- `/.well-known/oauth-authorization-server`
- `POST /oauth/token`
- `POST /mcp`

The token endpoint supports HTTP Basic client authentication and only the
`client_credentials` grant. The mandatory RFC 8707 `resource` value must equal
`https://127.0.0.1:8790/mcp`. Production serves the loopback endpoint with a
locally trusted TLS certificate. Tenant and runtime fields in the request are
rejected because identity comes only from the persisted registration.

Scopes are enforced by omitting unauthorized tools from the MCP server:

| Scope | Tool |
| --- | --- |
| `gmail.status` | `gmail_connection_status` |
| `gmail.search` | `gmail_search` |
| `gmail.read` | `gmail_get_message` |

The current registration grants all three read-only scopes. There is no Gmail
modify, send, archive, trash, or delete scope.

## Migration To EMA

When the selected IdP supports ID-JAG issuance, the connector authorization
server can add an EMA token-exchange grant alongside client credentials:

1. Verify the ID-JAG signature, issuer, audience, expiry, and resource.
2. Map the stable IdP subject to Ziggy's user and workspace.
3. Apply centrally managed MCP server and scope policy.
4. Issue the same short-lived connector access-token shape.
5. Retire runtime client secrets after every deployed MCP client supports EMA.

The MCP resource server and Gmail tenant boundary do not need to change. Only
the authorization-server input and provisioning policy change.

## References

- [MCP OAuth Client Credentials](https://modelcontextprotocol.io/extensions/auth/oauth-client-credentials)
- [MCP Enterprise-Managed Authorization](https://modelcontextprotocol.io/extensions/auth/enterprise-managed-authorization)
- [Enterprise-Managed Authorization announcement](https://blog.modelcontextprotocol.io/posts/enterprise-managed-auth/)
