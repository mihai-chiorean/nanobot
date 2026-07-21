# Ziggy Grafana dashboards

The dashboard JSON is versioned and importable:

- `dashboards/ziggy-fleet-control-plane-v1.json` uses current Prometheus/OTLP
  signals.
- `dashboards/ziggy-tenant-product-v1.json` contains one live anonymous fleet
  panel and pending PostgreSQL panels for the version-1 reporting views in
  `docs/architecture/ziggy-tenant-product-observability.md`.

The tenant dashboard does not imply that the tenancy database, runtime
inventory, usage store, or product event store exists. Panel descriptions mark
those sources pending. It never substitutes tenant-labeled metrics for durable
records.

## Publication credential

Dashboard publication uses a Grafana **service account token** for the target
stack's Grafana HTTP API. It does not use the Grafana Cloud access-policy tokens
that write metrics, logs, or traces.

Create a `Ziggy` folder first. A custom role on the publishing service account
needs exactly these publication actions:

```text
dashboards:create  scope folders:uid:<ZIGGY_FOLDER_UID>
dashboards:write   scope folders:uid:<ZIGGY_FOLDER_UID>
```

Granting folder **Edit** is a simpler but broader alternative. Add
`folders:create` on `folders:uid:general` only if separate automation must
create the folder. Dashboard read/delete, folder read/write/delete, data-source
administration, and telemetry write scopes are not required by the publisher
below.

The operator must provide these values without pasting them into source or
terminal output:

```text
GRAFANA_URL=https://<stack-host>.grafana.net
GRAFANA_SERVICE_ACCOUNT_TOKEN=<stack service account token>
GRAFANA_FOLDER_UID=<pre-created Ziggy folder UID>
```

Publish each file with Grafana's versioned dashboard API. A create conflict
means the stable UID already exists and is updated with `PUT`:

```sh
payload=$(mktemp)
trap 'rm -f "$payload"' EXIT HUP INT TERM
for dashboard in dashboards/*.json; do
  uid=$(jq -er '.uid' "$dashboard")
  jq --arg folder "$GRAFANA_FOLDER_UID" \
    '{metadata: {name: .uid, annotations: {"grafana.app/folder": $folder, "grafana.app/message": "sync versioned Ziggy dashboard"}}, spec: .}' \
    "$dashboard" >"$payload"
  status=$(curl --silent --show-error --output /dev/null --write-out '%{http_code}' \
    --request POST \
    --header "Authorization: Bearer $GRAFANA_SERVICE_ACCOUNT_TOKEN" \
    --header 'Content-Type: application/json' \
    --data-binary @"$payload" \
    "$GRAFANA_URL/apis/dashboard.grafana.app/v1/namespaces/default/dashboards")
  case "$status" in
    200|201) ;;
    409)
      curl --fail-with-body --silent --show-error --output /dev/null \
        --request PUT \
        --header "Authorization: Bearer $GRAFANA_SERVICE_ACCOUNT_TOKEN" \
        --header 'Content-Type: application/json' \
        --data-binary @"$payload" \
        "$GRAFANA_URL/apis/dashboard.grafana.app/v1/namespaces/default/dashboards/$uid"
      ;;
    *) echo "dashboard create failed with HTTP $status" >&2; exit 1 ;;
  esac
done
```

The stable dashboard UIDs ensure updates address only these dashboards.

## Data sources

After import, choose the stack's Prometheus/Mimir data source in the
`Metrics data source` variable. Publishing JSON does not require permission to
query it; dashboard viewers need folder-scoped `folders:read` and
`dashboards:read`, plus `datasources:query` for that data source.

The tenant dashboard also asks for a PostgreSQL `Product reporting data source`.
That source is intentionally unavailable until the durable store exists. Its
database login must be a separate read-only role with only:

```text
CONNECT on the product reporting database
USAGE on schema reporting
SELECT on reporting.admin_tenant_state_counts_v1
SELECT on reporting.admin_tenant_inventory_latest_v1
SELECT on reporting.admin_tenant_usage_hourly_v1
SELECT on reporting.admin_tenant_memory_search_hourly_v1
SELECT on reporting.admin_product_daily_v1
```

Do not reuse an application writer, migration owner, collector token, or
Grafana publication token for the PostgreSQL data source.

## Application to collector authentication

The local Beelink OTLP receiver uses a matched client/server credential pair:

- `/etc/ziggy/secrets/otel-local-auth`, root-owned mode `0400`, is the
  ziggy-control client file and contains `ziggy-control:<plain random password>`.
- `/etc/ziggy/secrets/otel-local-users.htpasswd`, root-owned mode `0400`, is the
  collector server file and contains `ziggy-control` with a hash of the same
  password.

The files are not interchangeable. The checked-in ziggy-control unit requires
and loads the client file as `otel-local-auth`, exposing only its systemd
credential path through `ZIGGY_OTEL_AUTH_FILE`. Set the non-secret
`ZIGGY_OTEL_ENDPOINT=http://127.0.0.1:4318` in
`/etc/ziggy/ziggy-control.env`. Never put the plaintext password or derived
Authorization header value in that environment file, and do not use a Grafana
Cloud token for this hop.

Provision both files atomically on Beelink before restarting the collector and
ziggy-control. The collector deployment owns that generator. Confirm only file
presence, ownership, and mode during deployment; do not echo or transfer either
credential value through chat or shell history.
