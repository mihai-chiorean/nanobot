# Ziggy Work Beelink Rollout And Rollback

Status: Phase 1 production procedure

This procedure deploys only the landed durable task/event behavior. Schedules,
durable runs, approvals, fencing, and connector/MCP capabilities are future
milestones and are not rollout gates in this phase.

## Production Values

| Item | Value |
|---|---|
| Unix account/group | `ziggy-work`, system account with no interactive shell |
| PostgreSQL | Native PostgreSQL 16 socket `/var/run/postgresql`, port `5433` |
| Database and role | Database `ziggy_work`, quoted role `"ziggy-work"` |
| Database DSN | `postgresql:///ziggy_work?host=/var/run/postgresql&port=5433&sslmode=disable` |
| Database secret | `/etc/ziggy/secrets/work-database-url`, mode `0400` |
| Listener | `127.0.0.1:8791` only |
| Binary and unit | `/usr/local/bin/ziggy-work`, `ziggy-work.service` |
| Work secrets | `/etc/ziggy/secrets/work-database-url`, `work-trust-key`, `work-runtimes.json` |
| Control integration | `30-work-credential.conf`; `ZIGGY_WORK_URL=http://127.0.0.1:8791` |

Record the approved artifact checksum and release in the change ticket, not in
this repository or shell history. Preserve every existing per-tenant SQLite
export unchanged, with checksums and a server-owned tenant/workspace mapping.
Do not map tenants by email alone.

## 1. Prepare Host And Credentials

```sh
sudo useradd --system --home-dir /var/lib/ziggy-work --create-home \
  --shell /usr/sbin/nologin ziggy-work
sudo install -d -o ziggy-work -g ziggy-work -m 0700 /var/lib/ziggy-work
sudo install -d -o root -g root -m 0750 /etc/ziggy/secrets
```

Provision the dedicated `ziggy_work` database and `ziggy-work` role on the
native PostgreSQL 16 instance. The role must not create databases, install
extensions, or access unrelated schemas. Install these root-owned credentials
with mode `0400`:

- `work-database-url`, containing exactly
  `postgresql:///ziggy_work?host=/var/run/postgresql&port=5433&sslmode=disable`;
- `work-trust-key`, shared only with Ziggy Control and at least 32 random
  bytes;
- `work-runtimes.json`, containing only the approved private runtime mapping.

Install `/usr/local/bin/ziggy-work` and `ziggy-work.service`. The unit must
run as `User=ziggy-work`, use `NoNewPrivileges=yes`, load all three secrets
through systemd, bind only to `127.0.0.1:8791`, and not share the Nanobot
account. Do not start it until migration/import prerequisites are complete.

## 2. Migrations And Import Gate

Back up `ziggy_work` and record PostgreSQL/application schema versions. The
release provides `/usr/local/bin/migrate-river` and
`/usr/local/bin/import-nanobot`. Run those installed binaries as the
`ziggy-work` account with the three secrets loaded through a protected
systemd credential context. Apply River migrations and then
`migrations/001_work_foundation.sql` before starting the service, and verify
the task/event/import tables and River schema versions.

Do not substitute `go run`, an unpinned River package, or an invented command.
The application service never migrates silently. Record the exact approved
release commands and checksums in the change ticket.

Checksum each read-only export as an operator step before import. The supplied
Phase 1 JSON importer binds each invocation to exactly one explicit
tenant/workspace, rejects duplicate records and malformed/trailing data, is
idempotent by tenant and source record, and converts imported in-flight tasks
to interrupted state with a matching terminal migration event. It preserves scheduled plans as data without
automatically rescheduling or backfilling them in this phase.

Run import in a staging database first, then production with the same mapping.
Expected reconciliation:

- owner: 14 tasks, 743 events, 1 step, 9 artifacts;
- tester: empty;
- no cross-tenant rows, and no automatic scheduled work created by import.

Keep source exports read-only through post-cutover validation and the agreed
retention window.

## 3. Start Privately And Validate Phase 1

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now ziggy-work.service
sudo systemctl status ziggy-work.service
sudo systemctl is-active --quiet ziggy-work.service
ss -ltn | grep -F '127.0.0.1:8791'
```

Through Ziggy Control, validate only the implemented `/api/work` behavior:

1. Owner can create/read tasks and events over REST and receive them over SSE.
2. Owner can cancel a task and send a follow-up message to a waiting task.
3. Tester starts empty and can create/read only tester data.
4. Changing task/event identifiers cannot read or mutate the other tenant.
5. River retry, Nanobot failure, cancellation, restart, SSE reconnect, and
   import conflict handling behave as the Phase 1 contract states.
6. Logs and metrics contain no tenant/content values or credentials.

Do not test or advertise schedule, run, approval, fence, connector, MCP, or
automatic-rescheduling APIs in this phase; they are not landed behavior.
The Work listener remains loopback-only and must not be exposed through
Cloudflare or a new firewall rule.

## 4. Install Control Work Drop-In And Canary

The current Control integration signs the principal described in the
architecture and uses `ZIGGY_WORK_URL=http://127.0.0.1:8791` plus the
`work-trust-key` systemd credential.

```sh
sudo install -D -o root -g root -m 0644 \
  services/ziggy-control/deploy/systemd/ziggy-control.service.d/30-work-credential.conf \
  /etc/systemd/system/ziggy-control.service.d/30-work-credential.conf
sudo chmod 0400 /etc/ziggy/secrets/work-trust-key
sudo systemctl daemon-reload
sudo systemctl restart ziggy-control
sudo systemctl is-active ziggy-control
```

The owner is the first canary. Validate the Phase 1 task/event, REST/SSE,
cancel, and follow-up flows through Control, then enable the tester and repeat
tenant-isolation checks. Confirm existing chat and Nanobot REST/SSE/WebSocket
paths remain healthy.

## 5. Rollback

Rollback for authorization failure, cross-tenant data, migration/import error,
corruption, or Work health failure preserves PostgreSQL, River tables,
application data, SQLite exports, and Work service files.

1. Remove only the Work Control drop-in:

   ```sh
   sudo rm /etc/systemd/system/ziggy-control.service.d/30-work-credential.conf
   ```

2. Edit the Control environment file and remove only `ZIGGY_WORK_URL` and
   `ZIGGY_WORK_TRUST_KEY_FILE`. Do not remove the base Control config,
   tenant bindings, Nanobot credentials, connector drop-ins, or database
   credentials.
3. Reload and restart only Control:

   ```sh
   sudo systemctl daemon-reload
   sudo systemctl restart ziggy-control
   ```

4. Verify `/api/work` is disabled/unavailable through Control while existing
   chat remains healthy. Stop Work only if required:

   ```sh
   sudo systemctl stop ziggy-work.service
   ```

5. Do not drop or roll back PostgreSQL tables. Preserve the database and
   exports; reconcile queued/running Phase 1 tasks before a later attempt and
   do not replay provider effects blindly.
