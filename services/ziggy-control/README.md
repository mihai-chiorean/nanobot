# ziggy-control

`ziggy-control` is Ziggy's product-owned Go front door. It routes each admitted
Clerk identity to a dedicated Nanobot process and workspace without changing
the existing client transport-token protocol.

```text
iOS / web -> Cloudflare -> ziggy-control -> private Nanobot -> Spark models
                         -> Clerk
```

## Current responsibilities

- Verify the Clerk JWT on `GET /auth/bootstrap` with the official Clerk Go SDK.
- Resolve a signed email claim, falling back to Clerk's Users API when needed.
- Resolve a verified Clerk subject and email through a server-owned tenant
  manifest. An unpinned tester subject is durably bound on first successful
  login and cannot later be replaced by another Clerk account.
- Forward bootstrap to that tenant's private runtime without changing the
  response shape, then retain only a SHA-256 fingerprint that binds each
  short-lived transport token to the same runtime.
- Reject unknown or expired transport tokens instead of falling back to the
  owner's runtime. Client-supplied workspace IDs never participate in routing.
- Proxy Nanobot REST bearer tokens, SSE streams, and WebSocket upgrades.
- Accept WebSocket bearer credentials in the upgrade `Authorization` header and
  translate them for the current private Nanobot protocol. Query-token support
  remains a deprecated compatibility path.
- Proxy tenant-authenticated Gmail connector routes through the private
  connector service using a signed, one-minute internal principal envelope.
  Client-supplied principal headers are stripped. The Google callback remains
  public but receives no principal envelope and is authorized by one-use OAuth
  state.
- Keep `/webui/bootstrap`, `/auth/token`, and configured private paths out of
  the public proxy.
- Expose `/healthz` and upstream-aware `/readyz` endpoints.
- Emit structured request logs with only bounded route, method, status,
  response bytes, duration, service, and build version. Request-time warnings
  and errors use bounded error classes and contain no raw path, URL, identity,
  credential, query, request ID, or session ID.
- Optionally emit content-free OpenTelemetry metrics and traces to a loopback
  OTLP/HTTP collector.
- Admit bounded tenant-fair HTTP, SSE, and WebSocket traffic without waiting;
  health and readiness remain available when a traffic class is full.

Configuration is immutable after startup. Admission uses atomic try-acquire
and release counters and returns `503 Retry-After: 1` when a class is full.
The standard HTTP transport provides concurrency-safe connection pooling, and
each request carries a fresh cryptographic correlation ID.

## Compatibility bridge

The current Nanobot `/auth/token` endpoint mints the opaque REST/WebSocket token
expected by the existing iOS and web clients. `ziggy-control` validates Clerk
at the control boundary, then calls the selected private tenant's `/auth/token`
with `Authorization: Bearer <upstream_bootstrap_secret>`. The public Clerk
bearer is replaced and is never forwarded to a tenant. The reverse proxy strips
`X-Nanobot-Auth` rather than treating it as a client-controlled credential.

This private token-issue exchange is deliberate and temporary. The next
protocol change should add a private Nanobot token-broker endpoint authenticated
only by `ziggy-control`; it should not expose another public token mint or
change the mobile protocol during the front-door cutover.

## Configuration

Required variables:

| Variable | Purpose |
|---|---|
| `CLERK_SECRET_KEY` or `CLERK_SECRET_KEY_FILE` | Backend key, supplied inline for development or through a credential file. Set exactly one. |
| `ZIGGY_UPSTREAM_URL` | Private Nanobot origin. It must use an explicit loopback, RFC1918 IPv4, IPv6 ULA/link-local, or Tailscale `100.64.0.0/10` address. Userinfo, query, fragment, and public hostnames are rejected. |
| `ZIGGY_TENANTS_FILE` | Immutable tenant allocation manifest. Production requires it. |
| `ZIGGY_TENANT_BINDINGS_FILE` | Durable first-login Clerk subject bindings. Production requires it and the service must be able to write it. |

Optional variables are documented in [`.env.example`](.env.example). In
production, at least one `ZIGGY_AUTHORIZED_PARTIES` value is mandatory. Tenant
identity is resolved through the manifest and durable Clerk subject binding;
an email match alone is not an identity boundary. The origin allowlist is
enforced when a token contains `azp`;
Clerk's native clients legitimately omit that browser-origin claim, so those
tokens continue through issuer, signature, lifetime, subject, and tenant
binding validation. Development, local, staging, and test environments retain
the explicit escape hatch for deployments without a browser origin or subject
pin.

The manifest format is shown in
[`deploy/tenants.example.json`](deploy/tenants.example.json). It must contain
exactly one `legacy_default` allocation. Every active tenant has unique
`user_id`, `workspace_id`, email, private upstream URL, and
`upstream_bootstrap_secret` with at least 32 characters. Keep the production
manifest mode `0600`. Pin known Clerk subjects in the manifest; otherwise the
first verified matching email binds the subject atomically in
`ZIGGY_TENANT_BINDINGS_FILE`. Removing or disabling an allocation immediately
blocks new bootstraps after restart.

Set `ZIGGY_CONNECTORS_URL` to enable `/connectors/*`. The URL must pass the same
private-network validation as Nanobot. The systemd unit discovers the shared
`connector-trust-key` through `CREDENTIALS_DIRECTORY`; outside systemd, also set
`ZIGGY_CONNECTORS_TRUST_KEY_FILE`. The key is shared only with
`ziggy-connectors`.

The default global admission limits are 64 ordinary HTTP requests, 8 SSE
streams, and 8 WebSocket connections. Each configured tenant receives an
equal share of each class, while anonymous traffic receives a separate share;
the global limits remain hard caps. Override the global limits with
`ZIGGY_MAX_HTTP_IN_FLIGHT`, `ZIGGY_MAX_SSE_IN_FLIGHT`, and
`ZIGGY_MAX_WEBSOCKET_IN_FLIGHT`. `/healthz` and `/readyz` bypass these limits.

Startup first requires the configured readiness path to return 2xx, then
requires the private Nanobot contract: unauthenticated `GET /auth/bootstrap`
must return `401`. A missing or incompatible upstream blocks deployment before
the listener starts. `ZIGGY_UPSTREAM_PREFLIGHT` may be disabled only outside
production when deliberately testing against an incomplete runtime.

Set `ZIGGY_OTEL_ENDPOINT=http://127.0.0.1:4318` and
`ZIGGY_OTEL_AUTH_FILE` to enable application metrics and traces through the
local collector. The credential file contains one bounded
`ziggy-control:<plain random password>` value in production. The parser also
accepts a bounded preformatted `Basic <base64(username:password)>` value for
compatibility.
Empty is the default; endpoint-only or credential-only configuration disables
OTel with a secret-free warning. The endpoint must be loopback. This is a
collector-local credential, not a Grafana credential, and it must be supplied
through systemd `LoadCredential`, never an environment value.
`ZIGGY_OTEL_TRACE_SAMPLE_RATIO` defaults to `1.0`, allowing the authenticated
local collector to retain every error/slow trace and two percent of normal
traces. Lower the ratio only for emergency resource control. A remote sampled
parent cannot force local acceptance. `ZIGGY_DEPLOYMENT_ENVIRONMENT` defaults
to `production`. Export uses bounded
background queues and three-second attempts, so a missing collector cannot
block or fail HTTP serving.

The current metric names are:

- `ziggy.control.service.uptime`
- `ziggy.control.http.server.requests`, `.duration`, `.response.size`, and
  `.active_requests`
- `ziggy.control.auth.bootstrap.attempts`
- `ziggy.control.upstream.requests`, `.duration`, and `.active_requests`

Their labels are limited to route, canonical HTTP method, status class,
upstream operation, and outcome enums. They never contain user, tenant,
workspace, conversation, session, request, or credential values. The complete
contract is in
[`../../docs/architecture/ziggy-tenant-product-observability.md`](../../docs/architecture/ziggy-tenant-product-observability.md).

Do not expose the default `127.0.0.1:8787` listener directly. Cloudflare should
route `chat.mihaichiorean.com` to it, while Nanobot remains bound to loopback on
its own port.

## Build and test

Go 1.24 or newer is required.

```sh
make verify
make linux-amd64
make linux-arm64
```

The Linux AMD64 artifact targets the current Beelink. The ARM64 artifact keeps
the planned all-on-Spark move a configuration and deployment change.

For local execution, provide the variables in your shell and run:

```sh
go run ./cmd/ziggy-control
```

## Beelink systemd deployment

The checked-in unit expects:

- binary: `/usr/local/bin/ziggy-control`
- non-secret config: `/etc/ziggy/ziggy-control.env`, mode `0600`
- tenant manifest: `/etc/ziggy/tenants.json`, owned by
  `ziggy-control:ziggy-control` and mode `0600`; root-only ownership prevents
  the unprivileged service from starting
- tenant subject bindings: `/var/lib/ziggy-control/tenant-bindings.json`,
  written atomically by the `ziggy-control` account; the unit's
  `StateDirectory=ziggy-control` creates the parent
- Clerk credential: `/etc/ziggy/secrets/clerk-secret-key`, owned by root and
  mode `0400`; the base unit is the only unit that loads it with
  `LoadCredential`
- optional local OTel client credential: `/etc/ziggy/secrets/otel-local-auth`,
  owned by root and mode `0400`; it contains
  `ziggy-control:<plain random password>`. The separate collector file
  `/etc/ziggy/secrets/otel-local-users.htpasswd` contains the same username and
  a hash of the same password. The two files are not interchangeable. The
  generated client file is loaded only by the OTel drop-in.
- optional connector trust credential: `/etc/ziggy/secrets/connector-trust-key`,
  shared with `ziggy-connectors`, owned by root and mode `0400`; it is loaded
  only by the connector drop-in
- unprivileged system account: `ziggy-control`

Install the base unit and only the optional credential drop-ins that are
configured on this host:

```sh
sudo chown ziggy-control:ziggy-control /etc/ziggy/tenants.json
sudo chmod 0600 /etc/ziggy/tenants.json
sudo install -D -o root -g root -m 0644 \
  services/ziggy-control/deploy/systemd/ziggy-control.service \
  /etc/systemd/system/ziggy-control.service
sudo install -d -o root -g root -m 0755 /etc/systemd/system/ziggy-control.service.d
# Install when ZIGGY_OTEL_ENDPOINT is configured:
sudo install -o root -g root -m 0644 \
  services/ziggy-control/deploy/systemd/ziggy-control.service.d/10-otel-credential.conf \
  /etc/systemd/system/ziggy-control.service.d/10-otel-credential.conf
# Install when ZIGGY_CONNECTORS_URL is configured:
sudo install -o root -g root -m 0644 \
  services/ziggy-control/deploy/systemd/ziggy-control.service.d/20-connector-credential.conf \
  /etc/systemd/system/ziggy-control.service.d/20-connector-credential.conf
```

The base unit loads only Clerk. Its `EnvironmentFile` is read before the
credential-path assignments, and the optional OTel path is assigned by its
drop-in after that file, so an empty environment-file value cannot blank a
credential path. The connector uses systemd's `CREDENTIALS_DIRECTORY` and
does not need an environment-file path. To disable an optional integration,
remove its exact drop-in, run `systemctl daemon-reload`, and restart the
service. Upgrades replace the base unit but preserve installed drop-ins;
install, remove, or change a drop-in before the same reload and restart. On the
first upgrade from the older unit, its inline OTel and connector credentials
are removed with the base file, so install the desired drop-ins before
restarting if either integration must remain enabled.

Install or upgrade the versioned binary atomically, retain the previous binary
as `/usr/local/bin/ziggy-control.previous`, then:

```sh
systemctl daemon-reload
systemctl restart ziggy-control
curl --fail http://127.0.0.1:8787/healthz
curl --fail http://127.0.0.1:8787/readyz
```

Rollback is an artifact swap followed by `systemctl restart ziggy-control`;
no Nanobot, model, or data service is rebuilt.

The production Cloudflare connector runs on the same host using
`deploy/systemd/ziggy-cloudflared.service`. Its tunnel token is loaded as a
systemd credential from `/etc/ziggy/secrets/cloudflare-ziggy-tunnel-token`;
do not place the token in the unit, environment file, or command line. The
connector proxies only to `http://127.0.0.1:8787`, keeping Spark private as the
Nanobot and model upstream.

`/healthz` reports only process health. `/readyz` requires a 2xx response from
`ZIGGY_UPSTREAM_READY_PATH` and caches the result briefly to avoid turning
health polling into upstream load. Neither endpoint proves that a user can
authenticate; deployment smoke tests must also exercise `/auth/bootstrap`.

The application and telemetry exporters share the configured
`ZIGGY_SHUTDOWN_TIMEOUT` budget (10 seconds by default). The systemd unit keeps
`TimeoutStopSec=15s`, leaving a stop cushion after HTTP draining and telemetry
flush. Do not configure a shutdown timeout longer than the unit's stop budget.

## Lab workflow

The `lab-control` repository remains the source of truth for host topology and
will eventually own deployment. Use its CLI for lab-wide visibility:

```sh
bin/lab status
bin/lab doctor
bin/lab logs beelink ziggy-control --since 15m
```

Do not make `ziggy-control` call `lab` at request time. Once the service is
represented in `topology/topology.yaml`, deployment and rollback should move
behind `bin/lab app ...`; the front door itself remains independent of the
orchestrator.

## Scope boundary

This pilot uses one Nanobot process, config tree, runtime-data tree, and
workspace per admitted user. It deliberately does not make Nanobot internally
multi-tenant. The allocation manifest is the control-plane source of truth and
the subject-binding file is a small local persistence bridge; PostgreSQL runtime
leases and automatic cold-start scheduling remain later milestones.

The checked-in Spark template `deploy/systemd/spark/nanobot-tenant@.service`
adds a read-only home/system view and one tenant-specific writable tree. Tools
must also run with Nanobot's `restrictToWorkspace` enabled. Legacy guest
enrollment routes are blocked at the public front door. Each generated tenant
config has `tokenIssuePath: "/auth/token"` and its per-runtime
`tokenIssueSecret`; the user unit removes Clerk variables and tenant processes
do not need a Clerk backend key. Query-token WebSocket compatibility remains
transport plumbing, not a tenant selector.
