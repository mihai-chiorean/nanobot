# Ziggy Phase 1-3 and 7 Review

Status: remediation in progress

Date: 2026-07-24

Scope:

- TestFlight external-release gate
- durable tenant lifecycle and manifest migration
- rootless per-tenant runtime proof
- Ziggy product-repository extraction

## Review Rounds

Three independent reviews covered:

1. feature and acceptance completeness;
2. concurrency, security, and resource isolation;
3. deployment, observability, rollback, CI, and repository boundaries.

The initial combined branch was not approved for merge. The reviews found
release-blocking races in runtime generation handling, missing PostgreSQL CI,
checkout-dependent export metadata, incomplete migration and socket contracts,
and release configuration that did not fail closed.

## Fixed Before Review Closure

- Runtime endpoint and bootstrap-secret uniqueness is database-enforced.
- PostgreSQL readiness requires both runtime-isolation indexes.
- The manifest importer is atomic, idempotent for sequential retries, and
  content-free in its output.
- Spark's peer-authenticated Unix-socket PostgreSQL URL is accepted.
- Terminal deletion erases runtime endpoint and bootstrap-secret material.
- Persistent tenant data is separate from generation-scoped runtime cleanup.
- Oversized runtime-manager socket requests are bounded before allocation.
- Product export includes the runtime manager and cannot recreate a root
  `nanobot/` tree during the web build.
- The product root no longer applies Nanobot's MIT license to all private
  Ziggy code; the complete upstream license remains under `licenses/`.
- Web dependency updates removed every high and critical npm advisory, and
  product CI rejects their reintroduction.

## Required Remediation

The branch must not merge until focused follow-up reviews close these items:

- persist and serialize runtime generation fences, including absent-container
  and destructive-replay cases;
- make runtime-manager socket ownership, peer authorization, and single-manager
  startup enforceable;
- prove generated rootless systemd/Quadlet units can access only the Podman
  state they require;
- enforce count-independent per-tenant admission in PostgreSQL mode and avoid
  duplicate database resolution;
- make concurrent manifest import converge safely and add schema identity plus
  least-privilege role grants;
- run PostgreSQL lifecycle tests in source and exported-product CI;
- make product exports independent of checkout branch and remote spelling;
- close secret-scanner gaps and remove unowned, unbuildable bridge content;
- fail Release iOS builds closed on production Clerk configuration and expose
  version/build information for support.

## Explicit Production Blocks

The following remain separate production gates even after the code review
findings above are fixed:

- PostgreSQL must not become the tenant authority until its import, backup,
  rollback, and lifecycle reconciliation drill passes on Spark.
- The rootless runtime manager must not route real tenants until Podman,
  subordinate IDs, LSM enforcement, an immutable runtime image, the enforcing
  egress gateway, capability rotation, and the two-tenant adversarial matrix
  pass on Linux.
- Disable and deletion must withdraw active routes and terminate established
  streaming connections through an orchestrated runtime receipt.
- The product repository depends on the separately pinned patched Nanobot
  runtime until the remaining generic patches are upstreamed or packaged as an
  immutable artifact.
- External TestFlight submission requires complete Apple reviewer metadata and
  approval. A processed build alone does not authorize external distribution.

## Rollout Decision

Safe deployment is limited to a manifest-compatible `ziggy-control` binary and
documentation/tooling that do not change the live source of tenant authority.
The current manifest, bindings, static per-tenant runtimes, and rollback
artifacts remain intact.

PostgreSQL cutover, rootless runtime routing, and external tester invitations
are independent changes with independent rollback points. They must not be
combined into the source-code merge.
