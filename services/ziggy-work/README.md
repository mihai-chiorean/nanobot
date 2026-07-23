# ziggy-work

`ziggy-work` is the private, durable Work API on `127.0.0.1:8791`. It accepts
the signed principal headers emitted by `ziggy-control`; it never derives a
tenant from request JSON or query parameters. PostgreSQL is mandatory outside
tests. River is pinned to v0.35.1 and is hidden behind the narrow queue
interface.

## Deployment

On `edge-builder-1`, apply `migrations/001_work_foundation.sql` and the pinned
River v0.35.1 migrations explicitly before starting the unit. No production
migration is performed by the process. The `work-database-url` secret must
contain exactly
`postgresql:///ziggy_work?host=/var/run/postgresql&port=5433&sslmode=disable`.
This uses PostgreSQL 16 Unix-socket peer authentication as the OS user and
quoted role `"ziggy-work"` against database `ziggy_work`. The systemd unit
runs `/usr/local/bin/ziggy-work` as `ziggy-work`, loads
`/etc/ziggy/secrets/work-database-url`, `work-trust-key`, and
`work-runtimes.json` through systemd credentials, and writes artifacts under
`/var/lib/ziggy-work/artifacts`.

The service exposes `/healthz` for process health and `/readyz` for PostgreSQL
and runtime-manifest readiness. `/api/work` and all subpaths require the
connector-compatible HMAC principal headers. Logs and optional OTLP telemetry
contain only route, method, status, operation, outcome, duration, status names,
retry/cancel counters, and dependency names. Tenant IDs, task IDs, URLs,
credentials, prompts, and event bodies are excluded.

## Legacy import

Run the release-installed `import-nanobot` binary before switching the Work
proxy; production must use the installed binary, never `go run`:

```sh
/usr/local/bin/import-nanobot \
  --input owner-work.json \
  --user-id usr_owner --workspace-id ws_owner \
  --artifact-root /path/to/nanobot/workspace \
  --artifact-destination /var/lib/ziggy-work/artifacts
```

The JSON is bounded to 64 MiB, with bounded task/event/artifact counts. The
explicit flags are the only tenant source; tenant fields in JSON are ignored.
The importer preserves opaque `work_` IDs, event sequences, steps, and artifact
metadata. It is idempotent, uses one database transaction, never enqueues
terminal or scheduled imports, rejects trailing JSON, duplicate keys,
traversal, symlink escapes, hash mismatches, and declared size mismatches.
Imported queued/running/waiting tasks receive a durable terminal
`status.changed` migration event when they are converted to `interrupted`, so
the event log and task snapshot converge.
Without a source root, artifact metadata is retained with an explicit
unavailable marker. A source root is required when an artifact map is used.

The currently active legacy scheduled plan is intentionally not recreated in
`ziggy-work`; its scheduled task remains visible but future cron ownership is
still transitional. Run `reconcile-runtimes` as a systemd timer before and
during rollout, or set `ZIGGY_WORK_RECONCILE_INTERVAL=60s` to enable the
bounded, non-enqueuing poller. It polls each active tenant's Nanobot
`/api/work?limit=200` to exhaustion, deduplicates events by runtime task and
runtime sequence, and mirrors task snapshots, event pages, steps, artifact
metadata, and verified artifact bytes.

## Compatibility

The API supports `GET/POST /api/work`, task detail, event pages, SSE at
`/events/stream` with `Last-Event-ID` and `after_seq`, cancel, and both
`/message` and the advertised `/messages` follow-up paths. Create requests
and all mutating task subpaths require an opaque `Idempotency-Key` between 16
and 128 characters. Keys are scoped by tenant and persisted in the same
PostgreSQL transaction as the task/event and River enqueue; reuse with a
different operation or payload returns `409`. Global and per-tenant stream
caps are configured with `ZIGGY_WORK_STREAM_LIMIT` and
`ZIGGY_WORK_TENANT_STREAM_LIMIT`. Create requests with nonempty `media`
return `501` so clients can deliberately fall back to
the legacy WebSocket path instead of silently losing attachments. Artifact
snapshots expose `/api/work/artifacts/{artifact_id}` only for available,
tenant-scoped files.

Nanobot command IDs suppress ordinary River retry duplicates. A process
failure after Nanobot accepts a bus publication but before Nanobot records it
as dispatched can still produce at-least-once delivery. Closing that final
window requires the planned Nanobot transactional outbox; it is not claimed
as Phase 1 exactly-once behavior.
