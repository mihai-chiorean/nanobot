-- Applied explicitly by deployment tooling. ziggy-control never migrates production.
-- Lifecycle events are intentionally content-free: identity data stays on the user row,
-- while the audit trail refers only to product-generated identifiers.
CREATE TABLE IF NOT EXISTS ziggy_tenant_users (
    user_id TEXT PRIMARY KEY,
    expected_email TEXT NOT NULL UNIQUE CHECK (expected_email = lower(expected_email)),
    clerk_subject TEXT UNIQUE,
    lifecycle_status TEXT NOT NULL CHECK (lifecycle_status IN ('invited', 'active', 'disabled', 'deletion_pending', 'deleted')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    activated_at TIMESTAMPTZ,
    disabled_at TIMESTAMPTZ,
    deleted_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS ziggy_tenant_workspaces (
    workspace_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL UNIQUE REFERENCES ziggy_tenant_users(user_id),
    workspace_kind TEXT NOT NULL DEFAULT 'personal' CHECK (workspace_kind = 'personal'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ziggy_tenant_runtime_allocations (
    workspace_id TEXT PRIMARY KEY REFERENCES ziggy_tenant_workspaces(workspace_id),
    user_id TEXT NOT NULL UNIQUE REFERENCES ziggy_tenant_users(user_id),
    runtime_id TEXT NOT NULL UNIQUE,
    generation BIGINT NOT NULL DEFAULT 1 CHECK (generation > 0),
    allocation_state TEXT NOT NULL DEFAULT 'pending' CHECK (allocation_state IN ('pending', 'active', 'disabled', 'deleted')),
    upstream_url TEXT NOT NULL DEFAULT '',
    upstream_bootstrap_secret TEXT NOT NULL DEFAULT '',
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ziggy_tenant_lifecycle_events (
    event_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id TEXT REFERENCES ziggy_tenant_users(user_id),
    workspace_id TEXT REFERENCES ziggy_tenant_workspaces(workspace_id),
    event_type TEXT NOT NULL,
    actor_kind TEXT NOT NULL,
    runtime_generation BIGINT,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ziggy_tenant_lifecycle_events_user_idx
    ON ziggy_tenant_lifecycle_events (user_id, occurred_at);
CREATE INDEX IF NOT EXISTS ziggy_tenant_lifecycle_events_workspace_idx
    ON ziggy_tenant_lifecycle_events (workspace_id, occurred_at);

-- Application roles receive INSERT/SELECT only. The migration owner can still
-- administer this table, so production roles must not be table owners.
REVOKE UPDATE, DELETE ON ziggy_tenant_lifecycle_events FROM PUBLIC;
