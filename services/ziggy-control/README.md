# ziggy-control

`ziggy-control` is Ziggy's product-owned Go front door. Phase one places it
between Cloudflare and the existing single-owner Nanobot runtime without
changing the client protocol or agent behavior.

```text
iOS / web -> Cloudflare -> ziggy-control -> private Nanobot -> Spark models
                         -> Clerk
```

## Current responsibilities

- Verify the Clerk JWT on `GET /auth/bootstrap` with the official Clerk Go SDK.
- Resolve a signed email claim, falling back to Clerk's Users API when needed.
- Enforce one configured owner email and, optionally, one Clerk subject.
- Forward the existing bootstrap request and response without changing their
  shape.
- Proxy Nanobot REST bearer tokens, SSE streams, and WebSocket upgrades.
- Accept WebSocket bearer credentials in the upgrade `Authorization` header and
  translate them for the current private Nanobot protocol. Query-token support
  remains a deprecated compatibility path.
- Accept guest enrollment at `POST /webui/guest/bootstrap` using a form `code`
  or `join_code` field, or a Bearer `Authorization` header. Legacy GET/query
  enrollment remains temporarily available and returns `Deprecation: true`.
- Keep `/webui/bootstrap`, `/auth/token`, and configured private paths out of
  the public proxy.
- Expose `/healthz` and upstream-aware `/readyz` endpoints.
- Emit structured request logs with only bounded route, method, status,
  response bytes, duration, service, and build version. Request-time warnings
  and errors use bounded error classes and contain no raw path, URL, identity,
  credential, query, request ID, or session ID.
- Optionally emit content-free OpenTelemetry metrics and traces to a loopback
  OTLP/HTTP collector.

Configuration is immutable after startup. Request handling uses no global
mutable state or application locks. The standard HTTP transport provides
concurrency-safe connection pooling, and each request carries a fresh
cryptographic correlation ID.

## Compatibility bridge

The current Nanobot `/auth/bootstrap` endpoint both validates Clerk and mints
the opaque REST/WebSocket token expected by the existing iOS and web clients.
For this first phase, `ziggy-control` validates and authorizes the owner, then
forwards the original Clerk bearer token to that endpoint. Nanobot validates it
a second time and returns its existing response unchanged.

This duplicate validation is deliberate and temporary. The next protocol
change should add a private Nanobot token-broker endpoint authenticated only by
`ziggy-control`; it should not expose another public token mint or change the
mobile protocol during the front-door cutover.

## Configuration

Required variables:

| Variable | Purpose |
|---|---|
| `CLERK_SECRET_KEY` or `CLERK_SECRET_KEY_FILE` | Backend key, supplied inline for development or through a credential file. Set exactly one. |
| `ZIGGY_OWNER_EMAIL` | Only account admitted to the shared phase-one workspace. |
| `ZIGGY_UPSTREAM_URL` | Private Nanobot origin, such as `http://127.0.0.1:8765`. |

Optional variables are documented in [`.env.example`](.env.example). Pin
`ZIGGY_OWNER_SUBJECT` after the first successful login so an email change or
account replacement cannot transfer access. Configure
`ZIGGY_AUTHORIZED_PARTIES` when the Clerk clients emit a stable `azp` claim.
Startup emits explicit warnings while either the subject pin or authorized-party
check is absent.

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
- Clerk credential: `/etc/ziggy/secrets/clerk-secret-key`, owned by root and
  mode `0400`; systemd exposes it to the service with `LoadCredential`
- local OTel client credential: `/etc/ziggy/secrets/otel-local-auth`, owned by
  root and mode `0400`; it contains
  `ziggy-control:<plain random password>`, and the checked-in unit maps it to
  `ZIGGY_OTEL_AUTH_FILE` with `LoadCredential`. The separate collector file
  `/etc/ziggy/secrets/otel-local-users.htpasswd` contains the same username and
  a hash of the same password. The two files are not interchangeable. The
  generated client file is required by the production unit.
- unprivileged system account: `ziggy-control`

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

This phase intentionally has no PostgreSQL dependency, no workspace scheduler,
and no second-user support. Admitting another email to this binary would place
that user in the owner's Nanobot workspace. The next milestone replaces the
owner policy with a durable identity/workspace resolver and routes two test
users to separate private runtimes.
