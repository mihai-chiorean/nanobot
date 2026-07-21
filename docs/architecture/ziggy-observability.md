# Ziggy Observability Architecture

Status: proposed

## Decision

Use OpenTelemetry and Prometheus formats as the instrumentation boundary, run
Grafana Alloy on every host, and send production telemetry to Grafana Cloud.
Keep Langfuse for LLM-specific traces, prompts, token use, cost, and evaluation.

This gives the system an off-site control plane that remains available when
Spark, Beelink, the home network, or their storage is down. Alloy is an open
source OpenTelemetry Collector distribution with native Prometheus, Loki,
Tempo, and generic OTLP support, so changing the storage backend does not
require replacing application instrumentation.

References:

- [Grafana Alloy overview](https://grafana.com/oss/alloy-opentelemetry-collector/)
- [OpenTelemetry Collector](https://opentelemetry.io/docs/collector/)
- [Grafana Cloud pricing and free retention](https://grafana.com/pricing/)

## Current state

- Spark runs `nanobot-prometheus` and `nanobot-grafana` in Docker.
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
ziggy-control ---- OTLP / Prometheus ----+
cloudflared ------- Prometheus ----------+       +-------------------+
systemd journal --- journald receiver ---+------>| Alloy on Beelink  |
Docker logs ------- Docker discovery ----+       +---------+---------+
                                                          |
Nanobot ---------- OTLP / logs ---------+                 | OTLP/HTTPS
vLLM ------------- Prometheus ----------+       +---------v---------+
GPU/host ---------- exporters ----------+------>| Alloy on Spark    |
                                                +---------+---------+
                                                          |
                           +------------------------------+------+
                           | metrics / logs / traces              |
                           v                                     v
                    Grafana Cloud                         Langfuse
                    operational view                     LLM quality view
```

Alloy enriches every signal with stable `service.name`, `service.version`,
`deployment.environment`, and `host.name` attributes. Secrets and prompt or
conversation bodies must be removed before export.

## Rollout

### Phase 1: host and ingress visibility

1. Create one Grafana Cloud stack and scoped write credentials.
2. Install Alloy as a systemd service on Beelink and Spark through Lab.
3. Collect host metrics, journald logs, Docker metrics/logs, cloudflared metrics,
   and the existing Spark Prometheus targets.
4. Add an external synthetic check for `https://chat.mihaichiorean.com/healthz`
   and an internal readiness check for `/readyz`.
5. Alert on public unavailability, readiness failure, tunnel disconnects,
   sustained 5xx rates, disk pressure, memory pressure, and Spark GPU failure.

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
- Pin container and collector versions instead of deploying `latest` tags.
- Keep collection configuration and dashboards in source control and deploy
  them through Lab.
- Set retention and ingestion budgets before enabling verbose application
  traces. Grafana Cloud's current free allowances list 50 GB per month each for
  logs and traces with 14-day retention; metrics use a separate active-series
  allowance. That is sufficient for a measured first rollout.
