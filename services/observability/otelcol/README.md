# Ziggy OpenTelemetry Collector

These profiles send a deliberately narrow, content-free operational telemetry
set to the ordinary Grafana Cloud OTLP endpoint. They do not enable Grafana
Application Observability or use Grafana's broad default Linux profile.

## Pinned binary

Production uses `otelcol-contrib` `v0.147.0`. The reviewed Linux AMD64 and
ARM64 archive hashes are in `otelcol-contrib-0.147.0-linux.sha256`.
`install-otelcol-contrib` selects the native architecture, downloads only the
exact release archive over HTTPS, verifies the checked-in SHA-256, and installs
the binary under:

```text
/opt/ziggy/otelcol-contrib/0.147.0/{amd64|arm64}/otelcol-contrib
```

The deployment script switches `/usr/local/bin/otelcol-contrib` only after the
candidate binary validates the candidate host profile. CI downloads and runs
the matching binary on native AMD64 and ARM64 runners.

## Credentials

Grafana Cloud uses one write-only token per host. The token source file is:

```text
/etc/ziggy/secrets/grafana-cloud-otel-token
```

It is root-owned mode `0400`. systemd loads it as the
`grafana-cloud-api-key` credential. The collector's Basic auth client reads
the credential file directly through `password_file`; the token is never
copied into a process environment variable.

Beelink OTLP/HTTP requires a separate local Basic credential. Generate the
paired server and client files on Beelink with:

```sh
sudo services/observability/otelcol/generate-otel-credentials
```

This installs:

```text
/etc/ziggy/secrets/otel-local-users.htpasswd  # collector server credential
/etc/ziggy/secrets/otel-local-auth            # ziggy-control username:password
```

Both are root-owned mode `0400` and are derived from the same random password.
The collector unit loads the first as systemd credential `otel-local-users`.
The ziggy-control unit loads the second as `otel-local-auth` and points
`ZIGGY_OTEL_AUTH_FILE` at `%d/otel-local-auth`. The client file contains one
`ziggy-control:<plain random password>` line; the Go client converts it to an
Authorization header internally. Neither secret belongs in
the repository, an environment file, command arguments, terminal output, or a
container image.

After credential rotation, restart both services. Restart the collector first;
ziggy-control telemetry fails closed during the brief mismatch and application
serving continues without telemetry.

## Profiles

`beelink.yaml` collects:

- authenticated loopback OTLP/HTTP metrics and traces from only
  `service.name=ziggy-control`;
- an exact application metric-name and attribute allowlist;
- exact, bounded span names and attributes, with every span event removed and
  spans with links dropped;
- host, health-check, Cloudflared, and system-level unit-state metrics;
- content-free service lifecycle and kernel health events.

`spark.yaml` collects host metrics, three named health probes, an exact Qwen
Prometheus metric allowlist, and content-free kernel OOM/GPU/storage events.
Spark has no OTLP receiver or application trace pipeline. It does not collect
Spark application logs or user-systemd state metrics.

Journal records are selected by explicit source and message class. Before
export, the collector maps each accepted record to a fixed `event.name`, erases
the body, and retains only the event name plus stable service/host resources.
There is no broad warning stream for ziggy-control, Cloudflared, Docker, Qwen,
Nanobot, or any other application.

Filesystem mountpoint attributes are dropped because they can expose user or
workspace paths. Prometheus scrape-time relabeling keeps only reviewed metric
names, drops identity/high-cardinality labels, then applies a short label
allowlist. Probe URLs are mapped to fixed target names and removed. Unknown
metrics, labels, OTLP resources, attributes, span names, span links, span
events, and metric exemplars are dropped.

## Sampling and buffering

The collector memory limiter leaves cgroup headroom below the systemd
`MemoryMax`: Beelink uses a `320 MiB` limit plus a `32 MiB` spike allowance
under `448M`, and Spark uses `128 MiB` plus `16 MiB` under `192M`. The remaining
space covers collector runtime, exporter queues, and filesystem-storage
overhead instead of allowing the limiter to consume the full cgroup budget.

Beelink tail sampling waits 10 seconds and keeps all received error traces,
traces slower than two seconds, and 2% of other traces. The tail sampler is
bounded to 1,000 in-flight traces with bounded decision caches. To let it see
errors and latency before making a decision, production ziggy-control must use:

```text
ZIGGY_OTEL_TRACE_SAMPLE_RATIO=1.0
```

The client still has a bounded in-memory batch queue. A lower client head
sample ratio would discard traces before collector error/latency policies can
inspect them.

Exporter queues and journald cursors use `file_storage` under
`/var/lib/ziggy-otelcol`. The Beelink exporter queue is capped at 64 MiB and
Spark at 32 MiB by the byte sizer; BoltDB metadata and compaction temporarily
add limited overhead beyond those payload caps. Batches are size-limited,
storage is fsynced and compacted, and export retries continue for up to 30
minutes. The systemd `StateDirectory` is private to the collector user. Queue
overflow or storage failure drops optional telemetry rather than consuming
unbounded disk or blocking application work.

## Deploy and rollback

Install `curl`, `openssl`, `apache2-utils`, `util-linux` (`flock`), `systemd`,
and `journalctl`. Stage
the root-owned Grafana credential on each host and the local OTLP credential
pair on Beelink. From a reviewed repository checkout, run:

```sh
sudo services/observability/otelcol/ziggy-otelcol-deploy deploy beelink
sudo services/observability/otelcol/ziggy-otelcol-deploy deploy spark
```

The deploy command:

1. Creates the unprivileged `otelcol-contrib` account if needed.
2. Downloads and verifies the architecture-specific pinned binary.
3. Validates the candidate profile with the candidate binary and credential
   file paths before changing the live service.
4. Saves the current config, unit, drop-in, launcher, and binary symlink under
   `/var/lib/ziggy-otelcol-deployments`.
5. Installs the candidate, enables and restarts the service, and confirms the
   local `otelcol_process_uptime_seconds_total` heartbeat, exporter queue
   capacity, and a positive `otelcol_exporter_sent_metric_points_total`
   counter. The last signal is
   incremented only after Grafana accepts a metric export.
6. Automatically restores the saved deployment if startup fails.

Deploy and rollback take a host-wide, nonblocking lock at
`/run/lock/ziggy-otelcol-deploy.lock`; a concurrent operation fails clearly
before changing collector state. The deployment backup state remains
root-owned and mode `0700`, and candidate validation uses an isolated temporary
directory before any live files are changed.

Manual rollback restores the immediately preceding deployment:

```sh
sudo services/observability/otelcol/ziggy-otelcol-deploy rollback
```

Rollback changes collector code/config only. For an existing service it also
requires the restored service to be active, expose collector self-telemetry,
and show a positive successful-export counter. A service that was previously
absent is stopped, disabled, and verified inactive. Persistent cursors and
queued telemetry remain under the stable state directory and should not be
deleted.
If a new collector version changes the storage schema, test downgrade against
a copied state directory before promotion.

## Verification

Run before deployment:

```sh
sh -n \
  services/observability/otelcol/generate-otel-credentials \
  services/observability/otelcol/install-otelcol-contrib \
  services/observability/otelcol/validate-privacy-canary \
  services/observability/otelcol/ziggy-otelcol-deploy \
  services/observability/otelcol/ziggy-otelcol-start
services/observability/otelcol/validate-privacy-canary \
  /usr/local/bin/otelcol-contrib
```

After deployment:

```sh
systemctl status otelcol-contrib --no-pager
journalctl -u otelcol-contrib --since -5m --no-pager
```

The deploy command already requires an active process, local collector
heartbeat, bounded queue metric, and successful Grafana metric response. Then
verify the expected host uptime, named probe, queue/failure/refusal metrics, and
selected service metrics in Grafana. Inject the privacy canary only in CI/local
test collectors; never send test secrets to the production endpoint. Full
journals remain local and are the forensic source when content-free cloud
events are insufficient.
