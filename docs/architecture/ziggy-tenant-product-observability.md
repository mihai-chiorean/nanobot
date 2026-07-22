# Ziggy Tenant and Product Observability

Status: current operational contract plus proposed tenant reporting contract

Contract version: 1

## Scope and current truth

This document separates what exists on the current Ziggy branch from the
multi-tenant runtime, memory, and product reporting surfaces that do not exist
yet.

Implemented today:

- `ziggy-control` is a single-owner front door. One configured Clerk email and
  optional subject map to one existing Nanobot upstream and workspace.
- It proxies REST, SSE, WebSocket, owner bootstrap, and legacy guest enrollment.
  It has no tenant resolver or control-plane database.
- It now emits optional OTLP/HTTP metrics and traces to a loopback collector for
  HTTP, bootstrap outcome, Clerk calls, Nanobot calls, and readiness probes.
- Beelink and Spark collector profiles already collect host, probe, systemd,
  Cloudflare, and selected model signals. Beelink accepts authenticated
  loopback application OTLP; Spark exposes no application OTLP receiver. This
  change does not modify those profiles.
- Existing Nanobot sessions, workspace files, and memory remain in the one
  current workspace. No current service publishes session, memory, workspace
  byte, or file inventory.

Not implemented today:

- invited, active, disabled, or deleted tenant records;
- a user-to-workspace registry, runtime supervisor, runtime leases, or multiple
  isolated Nanobot processes;
- authoritative turn/token/queue usage events or product events;
- a memory service, memory inventory snapshots, or memory search telemetry;
- PostgreSQL reporting tables/views or the admin observability API defined
  below; and
- a filesystem inventory scanner. Any such scan is explicitly a temporary
  single-owner source, not proof of tenant isolation.

The fleet dashboard is usable with current metrics. The tenant/product
dashboard is a versioned contract: its fleet aggregate panels work now, while
tenant panels state that their PostgreSQL views are pending.

## Privacy boundary

Metrics are anonymous fleet aggregates. The following values are forbidden as
metric attributes/Prometheus labels, without exception:

- tenant, user, Clerk subject, email, workspace, runtime, device, request,
  trace, conversation, session, work, tool-call, memory, document, or file IDs;
- raw path, URL, query string, host selected by a user, or IP address;
- prompt, response, transcript, search query, tool input/output, filename, or
  document content; and
- authorization, cookie, enrollment code, Nanobot token, OAuth token, or any
  credential-derived value or hash.

Allowed metric dimensions are fixed enums or deployment configuration with a
small controlled domain: service, version, environment, host role, route
template, canonical method, status class, outcome, runtime state, operation,
model class, client platform, and product event name from a reviewed registry.
Unknown values collapse to `other`, `unset`, or `unknown`.

Operational traces use only the same dimensions plus numeric HTTP status. They
do not record exception text because network errors can embed URLs. Only W3C
`traceparent`/`tracestate` is propagated; baggage is ignored. An untrusted
remote sampled flag cannot override the local head-sampling ratio.

Application logs follow the same content restrictions. Request-time logs do
not contain raw paths, URLs, error strings, request IDs, trace IDs, Cloudflare
ray IDs, identity fields, or session IDs. Warning/error records use a bounded
route and error class.

Durable application records are different from metrics. They may contain
internal tenant/workspace/runtime keys as typed database columns because those
keys are required for authorization, deletion, quotas, and per-tenant reports.
They must not be copied into OTel labels. Grafana uses an opaque, non-secret
`tenant_ref` from the reporting schema instead of an email or production UUID.

## Current OTel contract

OTel is disabled unless both `ZIGGY_OTEL_ENDPOINT` and
`ZIGGY_OTEL_AUTH_FILE` are configured and valid. The endpoint must be an
absolute loopback HTTP(S) URL, normally `http://127.0.0.1:4318`. The file is a
systemd client credential at `/etc/ziggy/secrets/otel-local-auth` containing
`ziggy-control:<plain random password>`. The collector separately reads
`/etc/ziggy/secrets/otel-local-users.htpasswd`, containing the same username
and a hash of the same password. These files are not interchangeable. The
client value is converted directly into the OTLP exporter Authorization
header, is never logged, and is not a Grafana credential. Export is
asynchronous, bounded, non-retrying at the SDK layer, and limited to a
three-second attempt. Initialization errors disable telemetry; shutdown gets a
bounded flush after HTTP shutdown. Serving does not depend on telemetry.

Resource attributes are `service.name=ziggy-control`,
`service.namespace=ziggy`, `service.version`, and
`deployment.environment`. The local collector adds host metadata.

| OTel metric | Unit | Allowed attributes | Implemented meaning |
|---|---|---|---|
| `ziggy.control.service.uptime` | `s` | resource only | Age of this control process's telemetry instance. |
| `ziggy.control.http.server.requests` | `{request}` | `ziggy.route`, `http.request.method`, `http.response.status_class` | Completed front-door requests. |
| `ziggy.control.http.server.duration` | `s` | same as requests | Full handler duration, including long-lived streams/upgrades. |
| `ziggy.control.http.server.response.size` | `By` | same as requests | Bytes written through the HTTP response writer. |
| `ziggy.control.http.server.active_requests` | `{request}` | `ziggy.route` | Requests currently in the handler. |
| `ziggy.control.auth.bootstrap.attempts` | `{attempt}` | `ziggy.outcome` | Owner bootstrap outcomes: `authorized`, `unauthenticated`, `denied`, `invalid_method`, `unavailable`, or `rejected`. |
| `ziggy.control.upstream.requests` | `{request}` | `ziggy.operation`, `http.response.status_class`, `ziggy.outcome` | Clerk, readiness, enrollment, bootstrap, and ordinary Nanobot calls. |
| `ziggy.control.upstream.duration` | `s` | same as upstream requests | Upstream request duration. |
| `ziggy.control.upstream.active_requests` | `{request}` | `ziggy.operation` | Current calls to each bounded upstream class. |

Routes are `health`, `readiness`, `bootstrap`, `enrollment`, `proxy`, and
`other`. Upstream operations are `identity`, `readiness`,
`nanobot_bootstrap`, `nanobot_enrollment`, `nanobot_proxy`, and `other`.

Server spans are named `HTTP <route>` and client spans are named
`upstream <operation>`. Span attributes are limited to `ziggy.route`,
`ziggy.operation`, canonical `http.request.method`, and numeric
`http.response.status_code`. The low-volume control plane defaults to 100%
parent-aware head acceptance. Beelink then retains every error and slow trace
plus two percent of normal traces. `ZIGGY_OTEL_TRACE_SAMPLE_RATIO` remains an
emergency resource-control override; a sampled remote parent cannot force a
trace past the configured local ratio. RED metrics remain complete regardless
of tracing decisions.

Grafana Cloud converts OTel dots to underscores and adds Prometheus type/unit
suffixes. For example, the request counter is queried as
`ziggy_control_http_server_requests_total` and the duration histogram as
`ziggy_control_http_server_duration_seconds_bucket`.

## Future low-cardinality fleet metrics

The following names are reserved contract proposals, not live metrics. Do not
build alerts that assume they exist.

| Area | Proposed metric | Allowed attributes |
|---|---|---|
| Runtime lifecycle | `ziggy.runtime.instances` | `state`, `runtime_kind`, `host_role` |
| Runtime lifecycle | `ziggy.runtime.transitions` | `from_state`, `to_state`, `reason_class` |
| Runtime lifecycle | `ziggy.runtime.start.duration` | `outcome`, `runtime_kind` |
| Admission/queue | `ziggy.model.queue.depth` | `work_class`, `model_class` |
| Admission/queue | `ziggy.model.queue.wait` | `work_class`, `outcome` |
| Fleet usage | `ziggy.usage.turns` | `work_class`, `model_class`, `outcome` |
| Fleet usage | `ziggy.usage.tokens` | `direction`, `model_class` |
| Memory search | `ziggy.memory.search.requests` | `operation`, `outcome`, `backend_class` |
| Memory search | `ziggy.memory.search.duration` | `operation`, `outcome`, `backend_class` |
| Product | `ziggy.product.events` | reviewed `event_name`, `client_platform`, `outcome` |

These metrics answer fleet health and capacity questions only. They cannot be
used to reconstruct a tenant bill, quota, deletion inventory, or product
funnel. Those answers come from the durable records below.

## Authoritative event store

Use PostgreSQL as the application source of truth. A service writes its state
change and a transactional outbox row in one transaction. An idempotent worker
copies outbox records into append-only, monthly partitioned event tables and
updates hourly/daily rollups. OTel may carry a content-free copy for incident
timelines, but delivery to an OTel collector is lossy and never authoritative.

Every durable event has `event_id` (UUID), `schema_version`, `occurred_at`,
`ingested_at`, `source_service`, `idempotency_key`, and typed internal foreign
keys. Payloads use versioned columns or validated JSON with an allowlist; no
arbitrary client property map is accepted.

Required version-1 records:

| Record | Required fields beyond the envelope | Explicitly excluded |
|---|---|---|
| `tenant_state_event_v1` | `tenant_id`, `from_state`, `to_state`, `reason_class`, operator/system actor class | email, Clerk token, free-text reason |
| `runtime_lifecycle_event_v1` | `tenant_id`, `workspace_id`, `runtime_id`, generation, from/to state, reason class, runtime kind, image digest, duration | logs, paths, credential/capability |
| `tenant_usage_event_v1` | tenant/workspace, work class, model class, outcome, input/output token counts, turn duration, queue outcome/wait/depth-at-admission, retry count | prompt/response, conversation/session ID, model request body |
| `inventory_snapshot_v1` | tenant/workspace, source, captured time, runtime/session/memory/workspace object counts, byte counts, file counts, scan completeness | path, filename, memory text, file hash |
| `memory_search_event_v1` | tenant/workspace, operation, backend class/version, outcome, candidate/result counts, latency, bytes read | query text, query hash, result text, memory ID |
| `product_event_v1` | tenant, reviewed event name/version, client platform/version class, outcome, typed allowlisted numeric/boolean dimensions | email, device ID, session/conversation ID, free text, arbitrary properties |

`tenant_account_v1` is durable state rather than an event. It holds internal
IDs, a random unique `tenant_ref` used only for restricted reporting, lifecycle
state (`invited`, `active`, `disabled`, `deletion_pending`, `deleted`), and
state timestamps. Email and external identity remain in the authorization
schema and are not projected into reporting views.

## Reporting views and admin API

The tenant dashboard queries a `reporting` schema through a PostgreSQL role
that has `CONNECT`, `USAGE ON SCHEMA reporting`, and `SELECT` only on these
views:

| View | Contract |
|---|---|
| `reporting.admin_tenant_state_counts_v1` | one row per state: `tenant_state`, `tenant_count`, `as_of` |
| `reporting.admin_tenant_inventory_latest_v1` | one row per `tenant_ref`: state, inventory source/time/completeness, and `*_count`, `*_bytes`, `*_file_count` for each of runtime, session, memory, and workspace |
| `reporting.admin_tenant_usage_hourly_v1` | `bucket_start`, `tenant_ref`, turns, failed/rejected turns, input/output tokens, turn latency p50/p95, queued/rejected queue turns, queue wait p50/p95 |
| `reporting.admin_tenant_memory_search_hourly_v1` | `bucket_start`, `tenant_ref`, searches/failures, candidate/result counts, bytes read, latency p50/p95 |
| `reporting.admin_product_daily_v1` | `day`, `tenant_ref`, reviewed event name, event count, success count, client platform |

The version suffix freezes these dashboard-facing column contracts. Counts and
bytes are non-negative `bigint`; latency values are non-negative `double
precision` milliseconds; times are UTC `timestamptz`; enums and `tenant_ref`
are bounded `text` values; and inventory completeness is `boolean`.

```text
admin_tenant_state_counts_v1(
  tenant_state, tenant_count, as_of
)
admin_tenant_inventory_latest_v1(
  tenant_ref, tenant_state, inventory_source, inventory_complete, captured_at,
  runtime_count, runtime_bytes, runtime_file_count,
  session_count, session_bytes, session_file_count,
  memory_count, memory_bytes, memory_file_count,
  workspace_count, workspace_bytes, workspace_file_count
)
admin_tenant_usage_hourly_v1(
  bucket_start, tenant_ref, turns, failed_turns, rejected_turns,
  input_tokens, output_tokens, turn_latency_p50_ms, turn_latency_p95_ms,
  queued_turns, queue_rejected_turns, queue_wait_p50_ms, queue_wait_p95_ms
)
admin_tenant_memory_search_hourly_v1(
  bucket_start, tenant_ref, searches, failed_searches, candidate_count,
  result_count, bytes_read, search_latency_p50_ms, search_latency_p95_ms
)
admin_product_daily_v1(
  day, tenant_ref, event_name, event_count, success_count, client_platform
)
```

These views provide the explicit operator answers:

1. Invited, active, and disabled totals come from
   `admin_tenant_state_counts_v1`.
2. Per-tenant runtime, session, memory, and workspace object, byte, and file
   counts come from `admin_tenant_inventory_latest_v1`.
3. Turns, tokens, latency, errors, rejections, and queue use over time come from
   `admin_tenant_usage_hourly_v1`; memory-search usage comes from
   `admin_tenant_memory_search_hourly_v1`.

The planned operator API exposes the same read model, protected by separate
operator authentication and audit:

```text
GET /internal/admin/observability/v1/tenants/summary
GET /internal/admin/observability/v1/tenants?state=&cursor=
GET /internal/admin/observability/v1/tenants/{tenant_ref}/inventory
GET /internal/admin/observability/v1/tenants/{tenant_ref}/usage?from=&to=&bucket=hour
GET /internal/admin/observability/v1/tenants/{tenant_ref}/memory-search?from=&to=&bucket=hour
GET /internal/admin/observability/v1/tenants/{tenant_ref}/product?from=&to=
POST /internal/runtime-observability/v1/inventory-snapshots
```

The runtime endpoint accepts an internal capability bound to one workspace;
the tenant key is resolved server-side. None of these endpoints is implemented
on the current branch.

### Temporary single-owner inventory

Before the tenancy database exists, a future operator-run inventory job may
scan exactly one configured owner workspace root and write an
`inventory_snapshot_v1` with `source=single_owner_filesystem`. It must report
aggregate counts/bytes only, reject symlink escapes, avoid filenames/paths,
mark partial scans, and never export the owner as an OTel label. This is a
temporary migration measurement, not a tenant inventory and not evidence that
multiple users are isolated. No scanner is implemented today, so the dashboard
states `inventory unavailable` rather than manufacturing a value.

## Retention and deletion

Recommended starting policy, subject to legal/product review before billing or
an external pilot:

| Data | Retention |
|---|---|
| Cloud operational traces and allowlisted logs | 14 days; local restricted journals up to 30 days |
| Operational metrics | 13 months when the selected Grafana plan supports it |
| Raw usage and memory-search events | 90 days |
| Hourly tenant usage rollups | 400 days |
| Daily usage/product rollups | 25 months |
| Runtime lifecycle events | 90 days; daily failure/restart rollups 400 days |
| Full inventory snapshots | 90 days; weekly snapshots 400 days |
| Product events | 90 days; consented daily aggregates 25 months |
| Security/admin audit events | 400 days minimum |

Tenant deletion revokes sessions first, stops the runtime, writes a final
inventory/deletion event, deletes tenant content and per-tenant raw/product
records, and removes or irreversibly anonymizes rollup dimensions according to
the deletion policy. Aggregate fleet rollups that cannot identify a tenant may
remain. Backups need the same expiry and deletion ledger. Memory content has a
separate product retention policy; this table governs only inventory/search
metadata.

## Dashboards and publication

Versioned dashboards live under `services/observability/grafana/dashboards`:

- `ziggy-fleet-control-plane-v1.json` uses current Prometheus/OTLP signals.
- `ziggy-tenant-product-v1.json` includes current fleet aggregates and pending
  PostgreSQL panels against the reporting views above.

Publishing dashboard JSON requires a Grafana **service account token**, not a
Grafana Cloud access-policy telemetry token. The target service account needs
folder-scoped `dashboards:create` and `dashboards:write`. Granting it **Edit**
on a pre-created Ziggy folder is a simpler but broader alternative.
`folders:create` is needed only if separate automation must create the folder.
No dashboard read/delete or metrics/logs/traces write scope is needed to
publish.
The exact environment and API command are documented in the Grafana README.

Dashboard viewers separately need `datasources:query` for the selected
Prometheus and PostgreSQL data sources. The PostgreSQL credential is a distinct
read-only reporting role; it is not supplied to dashboard publication
automation or ziggy-control.
