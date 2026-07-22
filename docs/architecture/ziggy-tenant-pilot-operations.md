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

Install the user unit from
`services/ziggy-control/deploy/systemd/spark/nanobot-tenant@.service`, then
generate a tenant config from the known-working owner config:

```sh
python3 provision_tenant.py \
  --source-config /home/mihai/.nanobot/config.json \
  --tenant-root /home/mihai/.local/share/ziggy/tenants/<workspace-id> \
  --email tester@example.com \
  --gateway-port 18800 \
  --websocket-host 100.86.74.94 \
  --websocket-port 18802

systemctl --user daemon-reload
systemctl --user enable --now nanobot-tenant@<workspace-id>.service
```

The generator creates an empty workspace, preserves only loopback model
providers, removes every non-WebSocket channel, removes MCP servers, disables
shell execution, enables `restrictToWorkspace`, and pins the runtime's Clerk
email allowlist. It does not copy sessions, memory, cron state, media, or the
owner's cloud provider credentials.

The systemd template presents home and system paths read-only and permits
writes only below that tenant root. This is a meaningful pilot boundary, but
the processes still run under the same Unix account. Before broader untrusted
use, move each runtime into its own container/user namespace with exactly one
writable workspace mount as specified in `ziggy-multitenant-runtime.md`.

## Front-door activation

1. Add the allocation to `/etc/ziggy/tenants.json` with a unique private Spark
   port and `status: active`. Exactly one owner entry has
   `legacy_default: true`.
2. Keep the owner entry's `upstream_url` byte-for-byte equivalent to
   `ZIGGY_UPSTREAM_URL`; startup fails if they differ.
3. Set `ZIGGY_TENANT_BINDINGS_FILE` to
   `/var/lib/ziggy-control/tenant-bindings.json`. The systemd
   `StateDirectory` owns its parent.
4. Restart `ziggy-control`. Startup validates all IDs, emails, duplicate
   workspaces, duplicate subjects, and private upstream addresses before it
   accepts traffic.
5. Have the tester sign in through Clerk. Legacy guest enrollment routes are
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
