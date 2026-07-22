# Ziggy Observability Architecture

Status: proposed; collector configuration is implemented but host rollout is a
separate promotion

## Decision

Use OpenTelemetry and Prometheus formats as the instrumentation boundary and
send a small, content-free operational telemetry set to Grafana Cloud. Run one
resource-capped `otelcol-contrib` process on each host because journald and host
metrics are node-local. Keep Langfuse local for prompts, model inputs/outputs,
token use, cost, and evaluation.

Grafana Cloud is an off-site alerting and incident index, not the forensic log
store. Full journals and private AI observability remain local.

## Privacy boundary

No prompt, transcript, email/document content, request or response body,
credential, authorization/cookie/header value, raw URL, query string, request
ID, user ID, workspace ID, session ID, or other tenant identity may leave the
private network.

The export boundary is allowlist-first:

1. Beelink accepts OTLP/HTTP only on loopback, only with local Basic auth, and
   only for `service.name=ziggy-control`.
2. Application metric names, span names, resource keys, span/datapoint keys,
   and bounded enum values are explicit allowlists. Unknown telemetry is
   dropped, not merely redacted.
3. All span events are dropped. Spans containing links and metric datapoints
   containing exemplars are dropped because `v0.147.0` cannot safely clear
   their nested attributes in place.
4. Filesystem mountpoint attributes are dropped because they can contain user
   or workspace paths. Prometheus metric names are allowlisted at scrape time.
   Identity and high-cardinality labels are dropped, followed by a short label
   allowlist.
5. Health-probe URLs are mapped from exact configured URLs to fixed target
   names, then the URL and error-message attributes are discarded.
6. Journald messages are used only to classify an allowlisted lifecycle or
   host-health event. The raw body and all journal fields are erased before
   export; the output contains a fixed `event.name`, timestamp/severity, and
   stable service/host resources only.

There is no cloud export of ziggy-control, Cloudflared, Docker, Qwen, Nanobot,
or user-session warning bodies. Redaction is defense in depth and never grants
permission to ingest a broad log source.

The CI privacy canary sends legacy and current URL, query, header, body,
identity, workspace, session, and request attributes through authenticated
OTLP. It also sends a span event, span link, status message, metric exemplar,
and unapproved metric. CI fails if authentication does not reject an anonymous
request, approved telemetry disappears, or any canary value reaches the file
exporter.

## Credentials

Grafana write tokens are one per host, scoped only for the required telemetry
write APIs, and loaded by systemd from
`/etc/ziggy/secrets/grafana-cloud-otel-token`. The collector Basic auth client
uses `password_file`; no cloud token is copied into process environment.

The Beelink local OTLP credential is a paired secret generated from one random
password:

```text
/etc/ziggy/secrets/otel-local-users.htpasswd
  -> collector systemd credential: otel-local-users

/etc/ziggy/secrets/otel-local-auth
  -> ziggy-control systemd credential: otel-local-auth
  -> ZIGGY_OTEL_AUTH_FILE=%d/otel-local-auth
```

The second file contains `ziggy-control:<plain random password>`; the Go client
normalizes it to a Basic Authorization value internally. Secret source files
are root-owned mode `0400`; credential paths, not values, are placed in process
environments. Rotation restarts the collector and ziggy-control.

## Implemented data flow

```text
ziggy-control -- authenticated loopback OTLP metrics/traces --+
Cloudflared -- allowlisted Prometheus metrics ---------------+--> Beelink collector
Beelink host/probes/system units -----------------------------+        |
content-free system lifecycle/kernel events -----------------+        |
                                                                      | TLS
Spark host/model probes --------------------------------------+        |
Qwen allowlisted Prometheus metrics --------------------------+--> Spark collector
content-free kernel OOM/GPU/storage events -------------------+        |
                                                                      v
                                                                Grafana Cloud

private prompts/model content ---------------------------------> local Langfuse only
```

Each pipeline sets only stable resources: `service.name`,
`service.namespace`, `deployment.environment`, `host.name`,
`ziggy.host.role`, and application `service.version` where applicable.

## Current collected surface

### Beelink

- Host uptime, load, CPU, memory, disk, filesystem, network, and paging metrics
  from explicitly enabled host scrapers and a final metric/attribute allowlist.
- Named checks for local health/readiness and public health. The checked-in
  collector does not create the independent external synthetic monitor; that
  remains a Grafana-side rollout action.
- A reviewed subset of Cloudflared connection/request/error metrics.
- `systemd.unit.state` for the four system units `ziggy-control.service`,
  `ziggy-cloudflared.service`, `docker.service`, and
  `otelcol-contrib.service`.
- Fixed service started/stopped/failed/restart-scheduled events for
  ziggy-control, Cloudflared, and Docker, plus fixed host OOM/storage-error
  events. No message body is exported.
- The nine named ziggy-control RED/uptime metrics currently emitted by the Go
  service.
- Content-free ziggy-control spans with bounded operation/route/method/status
  attributes.
- An allowlisted subset of collector process heartbeat, queue,
  exporter-failure, receiver/processor refusal, and tail-sampler self-metrics
  scraped from an explicit loopback Prometheus pull reader.

### Spark

- The same bounded host metric classes.
- Named health checks for Qwen, the anti-sycophancy proxy, and Nanobot gateway.
- A reviewed subset of Qwen/vLLM queue, cache, latency, token, and outcome
  metrics when those exact names exist at the deployed endpoint.
- Fixed kernel OOM, GPU-driver, and storage-error events with no message body.
- An allowlisted subset of collector process heartbeat, queue,
  exporter-failure, and receiver/processor refusal self-metrics from the same
  explicit loopback pull reader.

Spark has no OTLP receiver, trace pipeline, application journal pipeline, or
systemd metrics receiver. In particular, this design does **not** claim current
state metrics for Spark user services. Lifecycle text in the user journal is
not a trustworthy state source. Lab should eventually publish desired/deployed
version and health state, and the future runtime manager should publish actual
runtime desired/current state, lease generation, start outcome, and restart
counts as bounded metrics. Until then, endpoint probes, Qwen metrics, kernel
events, and local `systemctl --user` investigation are the available signals.

GPU utilization, memory, temperature, and power metrics are also not currently
implemented. Add them only after selecting and pinning a GPU exporter with an
exact metric/label allowlist and measuring its Spark resource cost.

## Trace sampling

The Beelink collector performs bounded tail sampling:

- retain all received traces with OpenTelemetry `ERROR` status;
- retain all received traces whose end-to-end duration exceeds two seconds;
- retain 2% of remaining traces probabilistically;
- hold at most 1,000 in-flight traces with bounded sampled/non-sampled decision
  caches and a 10-second decision window.

ziggy-control must set `ZIGGY_OTEL_TRACE_SAMPLE_RATIO=1.0` in production so the
collector sees error and latency outcomes before sampling. Any smaller SDK head
sample ratio means the collector cannot promise complete error/slow retention.
Sampling is a cost control, not a privacy control; sanitization runs before the
tail sampler.

## Queues and failure behavior

Exporter queues and journald cursors use the `file_storage` extension under the
systemd-managed `/var/lib/ziggy-otelcol` state directory. The queue byte sizer
caps payloads at 64 MiB on Beelink and 32 MiB on Spark. BoltDB pages and
compaction can temporarily consume additional bounded operational overhead, so
host disk alerts must leave headroom beyond those payload numbers.

Writes are fsynced, online/startup compaction is enabled, and exporter retry
continues for up to 30 minutes. Journald receivers persist cursors and retry
downstream backpressure for up to 30 minutes. Queue overflow, disk exhaustion,
or prolonged cloud failure drops optional telemetry; it must not block Ziggy or
grow disk use without a configured ceiling.

The collector self-metric allowlist provides process uptime as a heartbeat,
queue size/capacity, successful sends, enqueue/send failures, and
receiver/processor refusal counters. Full collector diagnostics remain in its
local journal.

## Resource isolation

The shared systemd unit runs as an unprivileged account with a private state
directory, empty capability set, strict filesystem/home/device protection,
restricted namespaces/address families, `NoNewPrivileges`, and other systemd
sandboxing. It retains journal read access through the `systemd-journal` group.

Beelink is limited to 448 MiB and 15% CPU. Spark is limited to 192 MiB and 5%
CPU. The collector has no GPU dependency. Losing telemetry is preferable to
contending with inference.

## Reproducible rollout

Production pins `otelcol-contrib v0.147.0` and checked-in SHA-256 values for
Linux AMD64 and ARM64 archives. CI runs the native artifact on both
architectures, parses all YAML, validates all profiles, checks shell syntax,
and executes the privacy/auth canary.

The host deploy command downloads and verifies the architecture-specific
artifact, validates the candidate config with the candidate binary and
credential-file paths, snapshots the current binary link/config/unit/drop-in/
launcher, installs the candidate, enables and starts the service, and verifies
that it remains active. Promotion additionally requires the local process
uptime heartbeat, exporter queue-capacity metric, and a positive
`otelcol_exporter_sent_metric_points_total` counter, which records an accepted
Grafana metrics export. A startup or export-readiness failure automatically
restores the snapshot. The same snapshot is available through an explicit
rollback command.

Rollout order:

1. Create one least-privilege Grafana write token per host and the paired local
   OTLP credential on Beelink.
2. Deploy Spark first and verify host/probe/Qwen/collector metrics plus only
   content-free kernel events.
3. Deploy Beelink with ziggy-control telemetry disabled and verify host/probe/
   Cloudflared/systemd/collector signals and content-free events.
4. Configure ziggy-control's loopback endpoint, `otel-local-auth` credential,
   and head sample ratio `1.0`; restart it and verify authenticated metric/trace
   arrival.
5. Run synthetic failure checks and inspect cloud output for only documented
   resources, names, labels, and event fields.
6. Add dashboards and alerts only for signals observed at the deployed version;
   absent allowlisted Qwen/Cloudflared names are a rollout finding, not a reason
   to broaden the regex.

Rollback restores collector artifacts/configuration but preserves persistent
queues and cursors. Storage-format downgrade compatibility must be tested on a
copy before changing the pinned collector version.

## Guardrails

- Do not enable OTLP on Spark until a named, reviewed SDK needs it and can use a
  dedicated authenticated credential and signal allowlist.
- Do not enable broad application, Docker, access, user-journal, or warning-log
  ingestion. Retrieve full logs locally during an incident.
- Do not add labels for user, workspace, conversation, session, request, URL,
  prompt, model input, or unbounded runtime IDs.
- Treat new metric names and attribute keys as denied until code review updates
  the relevant allowlist and privacy canary.
- Do not enable eBPF auto-instrumentation, Application Observability, or a local
  Loki/Tempo database on Spark during this rollout.
- Keep telemetry credentials write-only, host-specific, file-backed, and out of
  source control and process environments.
