# Ziggy Observability Architecture

Status: proposed

## Decision

Use OpenTelemetry and Prometheus formats as the instrumentation boundary and
send a deliberately small, content-free telemetry set to Grafana Cloud. Run the
official `otelcol-contrib` distribution on Beelink as the gateway. Run one
resource-capped collector on Spark only for local journal access, host/model
metrics, and forwarding selected Prometheus targets. Keep Langfuse local for
LLM-specific traces, prompts, token use, cost, and evaluation.

This gives the system an off-site control plane that remains available when
Spark, Beelink, the home network, or their storage is down. The collector is
vendor-neutral and supports Prometheus and generic OTLP inputs, so changing the
storage backend does not require replacing application instrumentation.

References:

- [OpenTelemetry Collector](https://opentelemetry.io/docs/collector/)
- [Grafana Cloud pricing and free retention](https://grafana.com/pricing/)

Use the ordinary Grafana Cloud Metrics, Logs, and Traces products. Do not enable
the separately billed Application Observability product or emit its
`traces_host_info` billing signal during the initial rollout.

## Requirements

- The cloud view must remain available when Spark, Beelink, or the home network
  is unavailable.
- An alert must identify the failed component and link to the preceding service
  events or error logs when they exist.
- No prompts, transcripts, email or document content, request bodies,
  credentials, authorization headers, cookies, or URL query strings may leave
  the private network.
- Collection must use no GPU memory and have a hard CPU and memory ceiling on
  Spark. Losing optional telemetry is preferable to contending with inference.
- Configuration, dashboards, recording rules, and alerts live in source control
  and are deployed through Lab.

## Current state

- Spark runs `nanobot-prometheus` and `nanobot-grafana` in Docker.
- Spark Prometheus currently scrapes only Nanobot and the Qwen-facing proxy. It
  does not expose host, GPU, systemd state, or restart information.
- Prometheus retains up to 90 days or 20 GB on Spark-local storage.
- Beelink has separate CVAT Grafana and Langfuse/ClickHouse deployments.
- `ziggy-control`, `ziggy-cloudflared`, and Spark user services log to journald.
- `ziggy-control` emits structured request logs with service, version, request
  ID, route, status, response bytes, duration, and Cloudflare Ray ID.
- `ziggy-cloudflared` exposes local Prometheus metrics on port 20241.

The Spark Prometheus/Grafana pair is useful for development but cannot be the
production source of truth: it disappears during the most important failure,
an unavailable Spark host.

## Data flow

```text
public /healthz and /readyz <---------- Grafana synthetic probes

ziggy-control -- OTLP metadata only ---+
cloudflared ---- Prometheus -----------+      +---------------------------+
Beelink journal allowlist ------------+----->| OTel gateway on Beelink   |
Docker service state -----------------+      +------------+--------------+
                                                               |
Spark endpoint probes over Tailscale --------------------------+
                                                               | TLS
Spark selected journal events ----------+                      |
Spark host/model health metrics ---------+  +-------------------v---+
Qwen Prometheus metrics -----------------+->| OTel collector on Spark|
                                            | MemoryMax/CPUQuota     |
                                            +-----------+------------+
                                                        |
                           +----------------------------+-----------+
                           | metrics / selected logs / sampled spans|
                           v                                        v
                    Grafana Cloud                          local Langfuse
                    operational view                      private AI data
```

The collector enriches every signal with stable `service.name`, `service.version`,
`deployment.environment`, and `host.name` attributes. The Spark service has a
systemd memory limit and CPU quota and does no trace processing, secret
scanning, or broad journal ingestion.

## Collected surface

### Metrics and probes

- Host availability, boot ID, uptime, load, CPU, RAM, disk, filesystem, network,
  and OOM counters.
- GPU utilization, memory used/free, temperature, power, and exporter health.
- Active/failed state and restart events for Qwen, the anti-sycophancy proxy,
  Nanobot gateway, Whisper, Ziggy ingest, `ziggy-control`, Cloudflare tunnel,
  Redis, and the backend containers.
- Process RSS/CPU and health for the inference and gateway processes. Alert if
  the retired Minimax process or unit becomes active.
- Black-box probes from Beelink to Spark ports `8012`, `8001`, and `18792`, and
  cloud probes to public `/healthz` and `/readyz`.
- Qwen queue depth, first-token latency, generation rate, and failures when the
  upstream exporter exposes them.

### Logs

Only ship warning/error events and lifecycle messages from the named services
above, plus kernel OOM and NVIDIA driver failures. Do not ship general Nanobot,
application, access, Docker, or user-session logs. Retain full journals locally
for deeper investigation. Filtering is allowlist-first; redaction is a second
line of defense, not permission to export arbitrary logs.

### Traces

Instrument `ziggy-control` first. Export service/operation names, timestamps,
status, latency, retry count, and a generated trace ID. Strip headers, cookies,
request and response bodies, user identifiers, raw URLs, and query strings
before export. Keep all errors and slow requests and sample only 1-5% of normal
requests. Propagate W3C trace context to Nanobot as support is added.

## Incident workflow

The fleet dashboard is organized for a single question: what changed before the
service stopped working?

1. A cloud probe detects that public readiness failed even if both local hosts
   are unreachable.
2. The incident timeline overlays host boot IDs, deploy annotations, systemd
   state transitions, restart events, GPU-memory headroom, OOM counters, and
   endpoint probe results.
3. Selecting a failed service opens only its allowlisted warning/error events
   for the five minutes before and after the transition.
4. A related sampled trace shows which request boundary failed or timed out,
   without request content or identity attributes.
5. A runbook link provides the local `lab` commands for retrieving complete
   journals when the cloud metadata is insufficient.

Grafana Cloud is therefore not the forensic data store. It is the durable
incident index and alerting surface that points to a narrow cause and the local
evidence needed to confirm it.

## Rollout

### Phase 1: host and ingress visibility

1. Create one Grafana Cloud Free stack with separate least-privilege write
   credentials for metrics, logs, and traces. Disable Application
   Observability.
2. Install the full OTel gateway on Beelink and resource-capped collector on
   Spark through Lab.
3. Collect host/GPU/service-state metrics, only the selected journald events,
   Cloudflare metrics, and the existing Spark Prometheus targets.
4. Add an external synthetic check for `https://chat.mihaichiorean.com/healthz`
   and an internal readiness check for `/readyz`.
5. Alert on public unavailability, readiness failure, tunnel disconnects,
   sustained 5xx rates, disk pressure, memory pressure, Spark GPU failure,
   service restart/failure, Qwen health, and unexpected Minimax activation.
6. Build one fleet dashboard whose service rows link directly to filtered logs
   for the selected host, service, and incident window.

### Phase 2: application telemetry

1. Add OpenTelemetry HTTP server/client spans and RED metrics to
   `ziggy-control` on a separate loopback telemetry listener.
2. Propagate W3C trace context to Nanobot and all Go services.
3. Instrument Nanobot, Whisper, Ziggy ingest, and model calls around queueing,
   first-token latency, tool execution, and completion.
4. Correlate operational trace IDs with Langfuse trace IDs without exporting
   prompt content into general-purpose logs.

### Phase 3: service objectives

- Public availability and bootstrap success rate.
- Chat request success rate and end-to-end latency.
- Time to first token and tokens per second.
- Tool-call failure and timeout rate.
- Whisper queue time, transcription latency, and rejected audio.
- GPU saturation, model queue depth, OOM events, and restarts.

## Alternatives

### Managed product fit

Grafana Cloud Free is the initial production choice. Its ordinary free stack
currently includes 10,000 active metric series, 50 GB each of logs and traces,
14-day retention, and three users. The existing Spark Prometheus head has about
549 series, so the first rollout has substantial headroom if journal collection
remains allowlisted.

Do not enable Grafana Cloud Application Observability initially. Its current
new-customer pricing adds $0.025 per host-hour plus telemetry charges, which is
about $36.50 per month for two continuously connected physical hosts before
telemetry. Plain Cloud Metrics, Logs, Traces, dashboards, and alerting provide
the capabilities required here without that product.

References:

- [Grafana Cloud deployment and free allowances](https://grafana.com/grafana/deployment-options/)
- [Grafana Cloud pricing](https://grafana.com/pricing/)
- [Application Observability host-hour pricing](https://grafana.com/docs/grafana-cloud/monitor-applications/application-observability/pricing/)

Better Stack is the simplest runner-up for external probes and incident UX. Its
free plan currently includes 10 monitors/heartbeats, 30 GB of metrics, and 3 GB
each of logs and traces retained for three days. That history is short for
intermittent home-lab failures, and using it would split the current Prometheus
and Grafana workflow across products. Reconsider it if operational simplicity
matters more than retention and ecosystem continuity.

SigNoz Cloud provides an integrated OpenTelemetry APM experience but currently
starts at $49 per month. Chronosphere targets enterprise-scale telemetry control
and has no public self-service price. Neither is justified at the current scale.

References:

- [Better Stack pricing](https://betterstack.com/pricing)
- [SigNoz pricing](https://signoz.io/pricing/)
- [Chronosphere distributed tracing](https://chronosphere.io/platform/distributed-tracing/)

### Self-hosted Grafana LGTM

Best fit if telemetry must stay local. It preserves Grafana, Prometheus, Loki,
and Tempo conventions, but introduces log and trace storage plus backup and
upgrade work. Tempo expects durable object storage for production traces. It
also needs to run outside Spark if it is expected to report Spark outages.

References: [Loki architecture](https://grafana.com/docs/loki/latest/get-started/architecture/),
[Tempo architecture](https://grafana.com/docs/tempo/latest/introduction/architecture/)

### SigNoz

Good integrated OpenTelemetry UI and self-hosting story. Its backend is another
ClickHouse deployment, duplicating the database and operational footprint
already present for Langfuse. Adopt it only if its integrated APM workflow is
more valuable than retaining the existing Grafana ecosystem.

Reference: [SigNoz architecture](https://signoz.io/docs/architecture/)

### OpenObserve

Strong compact self-hosted candidate with one product for logs, metrics, and
traces and OTLP ingestion. It is worth a later evaluation if Grafana Cloud cost
or data locality becomes a problem. Choosing it now would add a second query
and dashboard model while the current Prometheus/Grafana deployment is still
useful.

Reference: [OpenObserve documentation](https://openobserve.ai/docs/)

## Guardrails

- Do not use telemetry labels for user IDs, conversation IDs, request IDs, or
  other unbounded values; keep high-cardinality identifiers in logs and traces.
- Do not export Clerk tokens, Nanobot tokens, WebSocket query strings, prompts,
  email bodies, transcripts, or tool credentials.
- Drop `http.request.header.*`, `http.response.header.*`, `url.full`,
  `url.query`, cookies, and request/response body attributes in the collector,
  even when an SDK is believed not to emit them.
- Use an allowlist for journal units and priorities. Never rely on automatic PII
  or secret detection as the primary privacy control.
- Put Grafana Cloud credentials in systemd credentials, not environment files,
  and give each host write-only scopes. The dashboard account has MFA.
- Cap the Spark collector with systemd `MemoryMax` and `CPUQuota`; do not run eBPF
  auto-instrumentation or local Loki/Tempo databases on Spark.
- Start the Spark collector with `MemoryMax=192M` and `CPUQuota=5%`. Treat those as hard
  ceilings to validate under load, not resource reservations; lower them only
  after measuring collection gaps and queue behavior.
- Pin container and collector versions instead of deploying `latest` tags.
- Keep collection configuration and dashboards in source control and deploy
  them through Lab.
- Set retention and ingestion budgets before enabling verbose application
  traces. Grafana Cloud's current free allowances list 50 GB per month each for
  logs and traces with 14-day retention; metrics use a separate active-series
  allowance. That is sufficient for a measured first rollout.
