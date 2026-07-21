# Ziggy OpenTelemetry Collector

These profiles send a deliberately narrow operational telemetry set to the
ordinary Grafana Cloud OTLP endpoint. They do not enable Grafana Application
Observability and do not use Grafana's broad default Linux profile.

## Credential

Create one Grafana Cloud access policy with only `metrics:write`, `logs:write`,
and `traces:write`. Create a separate token for each host so either credential
can be revoked independently.

The token is the `GRAFANA_CLOUD_API_KEY`. The numeric instance ID is the Basic
Auth username. `ziggy-otelcol-start` derives the Basic Auth header at process
startup and reads the token through a systemd credential; no token belongs in
this repository, the collector YAML, or a persistent environment file.

The deployed credential path is:

```text
/etc/ziggy/secrets/grafana-cloud-otel-token
```

It must be owned by root with mode `0400`.

## Profiles

- `beelink.yaml` receives loopback-only OTLP, host and systemd unit metrics,
  Ziggy health probes, Cloudflare metrics, selected warning/error logs, and
  lifecycle/kernel events.
- `spark.yaml` receives loopback-only OTLP metrics, host metrics, model health,
  Qwen Prometheus metrics, and only lifecycle/kernel events. It intentionally
  does not export Nanobot, model, transcript, prompt, or tool logs.

The Spark unit is capped at 192 MiB and 5% CPU. The collector has no GPU
dependency. NVIDIA metrics require a small separate exporter and are not
enabled until Spark's existing exporter state is inspected.

## Install

Install the pinned `otelcol-contrib` package for the host architecture, then
copy the profile and launcher:

```sh
install -o root -g root -m 0755 -d /usr/local/libexec
install -o root -g root -m 0755 -d /etc/systemd/system/otelcol-contrib.service.d
install -o root -g root -m 0644 HOST.yaml /etc/otelcol-contrib/config.yaml
install -o root -g root -m 0755 ziggy-otelcol-start /usr/local/libexec/ziggy-otelcol-start
install -o root -g root -m 0644 systemd/otelcol-contrib-HOST.conf \
  /etc/systemd/system/otelcol-contrib.service.d/ziggy.conf
usermod -aG systemd-journal otelcol-contrib
systemctl daemon-reload
systemctl restart otelcol-contrib
```

Restarting after the group change is required. Validate with:

```sh
otelcol-contrib validate --config=/etc/otelcol-contrib/config.yaml
systemctl status otelcol-contrib --no-pager
journalctl -u otelcol-contrib --since -5m --no-pager
```

The service must be healthy before adding dashboards or alerts. Confirm in
Grafana Explore that each host has `system.uptime`, `httpcheck.status`, and
collector self-metrics. Then verify that exported logs contain only the
allowlisted lifecycle/error classes and no user content.

## Secret staging from the Mac

Replacement tokens are staged locally, outside the repository:

```text
~/.config/ziggy/secrets/grafana-cloud/beelink-token
~/.config/ziggy/secrets/grafana-cloud/spark-token
```

The directory must be mode `0700` and each file mode `0600`. Copy each file to
the matching host without printing it, then install it as the root-owned
credential above. Revoke a token immediately if it appears in terminal output,
chat, logs, source control, or command history.
