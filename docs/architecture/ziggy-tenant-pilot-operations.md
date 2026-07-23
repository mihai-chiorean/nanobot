# Ziggy Tenant Pilot Operations

Status: implementation-ready pilot boundary

## Isolation unit

Each admitted user receives all of the following as one allocation:

```text
Clerk subject + verified email
        |
        v
ziggy-control tenant registry
        |
        +-- stable user_id
        +-- stable workspace_id
        +-- one private Nanobot upstream
                         |
                         +-- one process
                         +-- one config/runtime tree
                         +-- one workspace
                              +-- sessions/
                              +-- memory/
                              +-- cron/
                              +-- media via the config-scoped runtime tree
                              +-- audit.jsonl
```

The client never sends an authoritative workspace ID. Clerk authentication
selects an allocation during `/auth/bootstrap`. `ziggy-control` records only a
SHA-256 fingerprint of each short-lived Nanobot transport token and routes all
later REST, SSE, and WebSocket traffic back to the runtime that issued it.
Unknown tokens fail with `401`; they never fall through to the owner.

The manifest admits by verified email and may pin `clerk_subject`. If the
subject is absent, the first successful Clerk login binds it atomically in the
state file. A different subject cannot later claim the same allocation.

## Spark runtime provisioning

Deploy Nanobot source as an immutable release tree and switch the stable
working-directory symlink atomically. Do not overwrite the historical dirty
vendor checkout that was used to establish the pilot:

```sh
release=/home/mihai/workspace/ziggy/releases/nanobot-<git-sha>
mkdir -p "$release"
# Extract the reviewed source artifact into $release, then verify its commit manifest.
ln -sfn "$release" /home/mihai/workspace/ziggy/current-nanobot.next
mv -Tf /home/mihai/workspace/ziggy/current-nanobot.next \
  /home/mihai/workspace/ziggy/current-nanobot
```

Both owner and tenant units must use `current-nanobot` as `WorkingDirectory`
and execute `python3 -m nanobot gateway` directly. Rollback repoints the symlink
to the prior immutable release and restarts the units; workspace, session,
memory, Work, cron, and media state live outside the release tree.

Install the user unit from
`services/ziggy-control/deploy/systemd/spark/nanobot-tenant@.service`, then
enable user-manager lingering before logout or reboot. Run this once as root
and verify the result:

```sh
sudo loginctl enable-linger mihai
test "$(loginctl show-user mihai -p Linger --value)" = yes
```

Create a fresh per-runtime bootstrap secret on Spark with mode `0600`; do not
print it or put it in shell history. Keep the same value for the Spark secret
file and the matching Beelink manifest field:

```sh
install -d -m 0700 /home/mihai/.config/ziggy/tenants
umask 077
openssl rand -base64 48 > /home/mihai/.config/ziggy/tenants/<workspace-id>.bootstrap-secret
chmod 0600 /home/mihai/.config/ziggy/tenants/<workspace-id>.bootstrap-secret
```

Generate a tenant config from the known-working owner config:

```sh
python3 provision_tenant.py \
  --source-config /home/mihai/.nanobot/config.json \
  --tenant-root /home/mihai/.local/share/ziggy/tenants/<workspace-id> \
  --email tester@example.com \
  --gateway-port 18800 \
  --websocket-host 100.86.74.94 \
  --websocket-port 18802 \
  --bootstrap-secret-file /home/mihai/.config/ziggy/tenants/<workspace-id>.bootstrap-secret

systemctl --user daemon-reload
systemctl --user enable --now nanobot-tenant@<workspace-id>.service
```

The generator creates an empty workspace, preserves only loopback model
providers, removes every non-WebSocket channel and inherited MCP server,
disables shell execution, enables `restrictToWorkspace`, and pins the runtime's
Clerk email allowlist. It also sets WebSocket `tokenIssuePath` to `/auth/token`
and stores the trimmed per-runtime `tokenIssueSecret`; the CLI rejects missing
or short secret material and never prints it. Tenant processes receive no
Clerk backend key. It does not copy sessions, memory, cron state, media, or the
owner's cloud provider credentials.

`--enable-gmail-mcp` adds only Ziggy's read-only Gmail MCP server. Do not enable
it for untrusted testers while tenant runtimes share the `mihai` Unix account;
the same-account pilot boundary does not defend against a compromised runtime
reading another runtime's host files.

First provision a connector OAuth client for the runtime generation. Write its
secret to the per-user systemd credential store, not into Nanobot config:

```sh
install -d -m 0700 /home/mihai/.config/credstore
umask 077
openssl rand -base64 48 > "/home/mihai/.config/credstore/ziggy-mcp-<workspace-id>"

sudo /usr/local/bin/provision-runtime-oauth-client \
  --database-url-file /run/credentials/ziggy-connectors.service/database-url \
  --client-credential-pepper-file /run/credentials/ziggy-connectors.service/client-credential-pepper \
  --user-id "<user-id>" \
  --workspace-id "<workspace-id>" \
  --runtime-id "<runtime-id>" \
  --runtime-generation 1 \
  --secret-file "/home/mihai/.config/credstore/ziggy-mcp-<workspace-id>"
```

Then add `--enable-gmail-mcp --gmail-mcp-client-id <client-id>` to the tenant
generator command above. Existing trusted runtimes can be patched without
replacing the rest of their config:

```sh
python3 configure_gmail_mcp.py \
  --config /path/to/runtime/config.json \
  --client-id "<client-id>"
systemctl --user restart <runtime-unit>
```

The migration is idempotent, writes atomically, preserves file ownership and
mode, and never prints either secret.

The owner runtime uses a separate drop-in and credential source name. Install
it before patching the owner config:

```sh
install -d -m 0700 /home/mihai/.config/systemd/user/nanobot-gateway.service.d
install -m 0600 \
  services/ziggy-control/deploy/systemd/spark/nanobot-gateway.service.d/40-mcp-client-credential.conf \
  /home/mihai/.config/systemd/user/nanobot-gateway.service.d/
systemctl --user daemon-reload
systemctl --user cat nanobot-gateway.service
```

The rendered owner unit must load `ziggy-mcp-owner` and export its mounted path
as `ZIGGY_MCP_CLIENT_SECRET_FILE`; otherwise the environment reference in the
Nanobot config remains unresolved and startup fails.

Run the live smoke through a transient user unit so it receives the same
systemd credential mount and CA trust as the owner gateway:

```sh
systemd-run --user --wait --pipe --collect \
  --unit=ziggy-gmail-mcp-smoke \
  --property=LoadCredential=mcp-client-secret:/home/mihai/.config/credstore/ziggy-mcp-owner \
  --property=Environment=SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
  --property=Environment=PYTHONPATH=/home/mihai/workspace/ziggy/current-nanobot \
  /bin/sh -c \
  'export ZIGGY_MCP_CLIENT_SECRET_FILE="$CREDENTIALS_DIRECTORY/mcp-client-secret";
   exec /home/mihai/workspace/ziggy/.venv/bin/python3 \
   /home/mihai/workspace/ziggy/current-nanobot/services/ziggy-control/deploy/spark/smoke_gmail_mcp.py \
   --config /home/mihai/.nanobot/config.json'
```

Success prints only `Gmail MCP smoke passed`. The smoke performs the real
CA-verified HTTPS connection, client-credentials token exchange, MCP
initialization, tool discovery, and tenant-scoped Gmail connection-status call.

Roll out this boundary in dependency order:

1. Install the connector migration, signing/pepper/TLS credentials, local CA,
   connector binary, and runtime OAuth client registration without restarting.
2. Install the Nanobot release, systemd credential drop-in, and OAuth-based MCP
   config.
3. Restart `ziggy-connectors`, verify HTTPS readiness and token issuance, then
   restart the Nanobot runtime and execute a Gmail MCP smoke test.
4. Only after that smoke succeeds, deploy `ziggy-control` with the legacy
   `/runtime/connectors/mcp` route removed.

Rollback must reverse the same boundary as one unit: restore the prior control
route, prior connector binary/unit, prior Nanobot release/config, and restart
connector then runtime. Do not roll back only one component of the auth
contract.

The systemd template presents home and system paths read-only and permits
writes only below that tenant root. `UnsetEnvironment=CLERK_SECRET_KEY
CLERK_SECRET_KEY_FILE` prevents either Clerk backend variable from reaching the
tenant process, including through a tenant environment file. This is a
meaningful pilot boundary, but the processes still run under the same Unix
account. Before broader untrusted use, move each runtime into its own
container/user namespace with exactly one writable workspace mount as specified
in `ziggy-multitenant-runtime.md`.

After provisioning, copy the same bootstrap secret value to the matching
tenant's `upstream_bootstrap_secret` in the Beelink `/etc/ziggy/tenants.json`.
Every active tenant must have a unique value of at least 32 characters. Keep
the manifest owned by `ziggy-control:ziggy-control` and mode `0600`; the
unprivileged front-door process must be able to read it. Transfer the value
through the approved secret channel; do not paste it into logs, commands, or
issue trackers.

After a reboot, verify the user manager and tenant unit before admitting the
tenant:

```sh
loginctl show-user mihai -p Linger
systemctl --user is-enabled nanobot-tenant@<workspace-id>.service
systemctl --user is-active nanobot-tenant@<workspace-id>.service
```

## Front-door activation

1. Add the allocation to `/etc/ziggy/tenants.json` with a unique private Spark
   port and `status: active`. Exactly one owner entry has
   `legacy_default: true`.
2. Keep the owner entry's `upstream_url` byte-for-byte equivalent to
   `ZIGGY_UPSTREAM_URL`; startup fails if they differ.
3. Set `ZIGGY_TENANT_BINDINGS_FILE` to
   `/var/lib/ziggy-control/tenant-bindings.json`. The systemd
   `StateDirectory` owns its parent.
4. Confirm every active allocation has a unique `upstream_bootstrap_secret`
   matching the secret file used to provision its Spark runtime.
5. Set `/etc/ziggy/tenants.json` ownership to `ziggy-control:ziggy-control`
   and mode `0600`.
6. Restart `ziggy-control`. Startup validates all IDs, emails, duplicate
   workspaces, duplicate subjects, private upstream addresses, and bootstrap
   secret material before it accepts traffic.
7. Have the tester sign in through Clerk. Control validates the public Clerk
   bearer, replaces it with `Authorization: Bearer <upstream_bootstrap_secret>`
   for the private tenant `/auth/token` call, and never forwards the Clerk
   bearer. `X-Nanobot-Auth` is stripped by the reverse proxy.
   Legacy guest enrollment routes are
   blocked at the public front door and are not part of tenant provisioning.

## Acceptance tests

Run these before admitting a tester:

1. Existing owner-tenant bootstrap and iOS conversation history still work.
2. Tester bootstrap reaches the tester runtime and begins with zero owner
   sessions and zero owner memory.
3. A tester transport token plus `workspace_id=<owner>` still returns only the
   tester runtime's data.
4. An unknown or expired token returns `401` and produces zero owner-runtime
   requests.
5. Identical `chat_id` values in both runtimes create separate session files.
6. A global legacy session with the same key is never migrated into a custom
   workspace.
7. Audit, cron, media, session, and memory writes remain below the tester's
   tenant root.
8. Stopping the tester unit makes only that tester unavailable; owner chat and
   Qwen remain healthy.

## Scheduled work

Nanobot's existing cron store is now workspace-scoped, so it remains acceptable
for tenant-local reminders inside one runtime. It is not the connector job
system: it has no PostgreSQL lease, cross-process fence, encrypted connector
credential boundary, or product-level usage/audit record. Gmail schedules will
therefore move to a Go `ziggy-work` service and PostgreSQL-backed worker. The
runtime may request such work through a narrow capability later, but Google
tokens and Gmail cursors never enter Nanobot cron payloads or workspace files.

## Rollback

Disable only the affected allocation, restart `ziggy-control`, and stop its
tenant unit. Do not point the user at the owner runtime as a fallback. Preserve
the tenant tree for investigation or export. Rolling back the front-door binary
to a single-tenant version requires first removing all non-owner admissions;
otherwise a tester could be routed into shared owner state.
