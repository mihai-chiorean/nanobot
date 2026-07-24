-- Apply after 001_tenant_lifecycle_foundation.sql. Existing pending
-- allocations retain empty endpoint fields; activated endpoints and bootstrap
-- secrets must remain exclusive to one tenant.
CREATE UNIQUE INDEX IF NOT EXISTS ziggy_tenant_runtime_allocations_upstream_url_unique
    ON ziggy_tenant_runtime_allocations (upstream_url)
    WHERE upstream_url <> '';

CREATE UNIQUE INDEX IF NOT EXISTS ziggy_tenant_runtime_allocations_bootstrap_secret_unique
    ON ziggy_tenant_runtime_allocations (upstream_bootstrap_secret)
    WHERE upstream_bootstrap_secret <> '';
