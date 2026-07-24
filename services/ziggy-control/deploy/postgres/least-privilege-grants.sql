\set ON_ERROR_STOP on
-- Required psql variables:
--   database_name: database containing the Ziggy tenant lifecycle schema
--   control_role:  login role used by ziggy-control
--   import_role:   login role used only by import-tenant-manifest

BEGIN;

REVOKE CONNECT ON DATABASE :"database_name" FROM PUBLIC;
GRANT CONNECT ON DATABASE :"database_name" TO :"control_role", :"import_role";

REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO :"control_role", :"import_role";

REVOKE ALL ON TABLE
    ziggy_tenant_users,
    ziggy_tenant_workspaces,
    ziggy_tenant_runtime_allocations,
    ziggy_tenant_lifecycle_events,
    ziggy_schema_identity
FROM PUBLIC;
REVOKE ALL ON SEQUENCE ziggy_tenant_lifecycle_events_event_id_seq FROM PUBLIC;

GRANT SELECT ON TABLE
    ziggy_tenant_users,
    ziggy_tenant_workspaces,
    ziggy_tenant_runtime_allocations,
    ziggy_tenant_lifecycle_events,
    ziggy_schema_identity
TO :"control_role";
GRANT INSERT (
    user_id, expected_email, lifecycle_status, created_at, updated_at
) ON ziggy_tenant_users TO :"control_role";
GRANT UPDATE (
    expected_email, clerk_subject, lifecycle_status, updated_at,
    activated_at, disabled_at, deleted_at, destruction_receipt
) ON ziggy_tenant_users TO :"control_role";
GRANT INSERT (workspace_id, user_id, created_at)
    ON ziggy_tenant_workspaces TO :"control_role";
GRANT INSERT (
    workspace_id, user_id, runtime_id, generation, allocation_state,
    created_at, updated_at
) ON ziggy_tenant_runtime_allocations TO :"control_role";
GRANT UPDATE (
    allocation_state, upstream_url, upstream_bootstrap_secret, metadata, updated_at
) ON ziggy_tenant_runtime_allocations TO :"control_role";
GRANT INSERT (
    user_id, workspace_id, event_type, actor_kind, runtime_generation, occurred_at
) ON ziggy_tenant_lifecycle_events TO :"control_role";
GRANT USAGE ON SEQUENCE ziggy_tenant_lifecycle_events_event_id_seq TO :"control_role";

GRANT SELECT ON TABLE
    ziggy_tenant_users,
    ziggy_tenant_workspaces,
    ziggy_tenant_runtime_allocations,
    ziggy_schema_identity
TO :"import_role";
GRANT INSERT (
    user_id, expected_email, clerk_subject, lifecycle_status,
    created_at, updated_at, activated_at, disabled_at
) ON ziggy_tenant_users TO :"import_role";
GRANT INSERT (workspace_id, user_id, created_at)
    ON ziggy_tenant_workspaces TO :"import_role";
GRANT INSERT (
    workspace_id, user_id, runtime_id, generation, allocation_state,
    upstream_url, upstream_bootstrap_secret, created_at, updated_at
) ON ziggy_tenant_runtime_allocations TO :"import_role";
GRANT INSERT (
    user_id, workspace_id, event_type, actor_kind, runtime_generation, occurred_at
) ON ziggy_tenant_lifecycle_events TO :"import_role";
GRANT USAGE ON SEQUENCE ziggy_tenant_lifecycle_events_event_id_seq TO :"import_role";

ALTER DEFAULT PRIVILEGES REVOKE ALL ON TABLES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES REVOKE ALL ON SEQUENCES FROM PUBLIC;

COMMIT;
