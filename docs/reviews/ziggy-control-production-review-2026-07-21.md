# Ziggy Control Production Review

Date: 2026-07-21

Three independent reviews examined the front door before production cutover.

## Feature completeness

- Fixed: readiness now probes a configured path and requires a 2xx response.
- Fixed: Clerk user lookups are pooled and successful email resolutions are cached.
- Fixed: startup warns when the owner subject is not pinned or Clerk authorized-party validation is disabled.
- Accepted for this release: only `/auth/bootstrap` accepts a Clerk session token. The bootstrap response exchanges it for Nanobot's short-lived token; applying Clerk middleware to subsequent API, SSE, and WebSocket requests would break that protocol.
- Follow-up: move the WebSocket token from the query string to an authorization header after Nanobot and the iOS client both support it.
- Follow-up: publish signed release artifacts in CI. This release uses a checksummed static binary.

## Concurrency and resource efficiency

- Fixed: readiness uses a lock-free, stale-while-refresh cache so probes do not fan out to Spark.
- Fixed: upstream and Clerk HTTP transports have bounded connection pools.
- Fixed: request bodies are bounded without applying global read/write deadlines that would terminate SSE and WebSocket connections.
- Fixed: untrusted `X-Forwarded-For` values are discarded before constructing the trusted proxy chain.
- Fixed: graceful shutdown force-closes connections after its deadline and exits cleanly.
- Verified: unit tests and the race detector cover cached identity lookup, cached readiness, SSE flushing, and WebSocket hijacking.

## Observability

- Fixed: structured access logs include service, version, request ID, route, status, response bytes, duration, and Cloudflare Ray ID.
- Fixed: bootstrap authorization success and denial are explicit events without logging email addresses or credentials.
- Fixed: health and readiness semantics and operational smoke checks are documented.
- Follow-up: export service metrics and traces through an OpenTelemetry-compatible collector after the ingress cutover.

## Release gate

The release must pass `make verify`, produce a Linux AMD64 artifact with an embedded commit version, pass local and remote checksum verification, and pass live health, readiness, authenticated bootstrap, SSE, and WebSocket smoke checks before the public route changes.
