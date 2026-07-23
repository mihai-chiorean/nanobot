CREATE TABLE IF NOT EXISTS ziggy_connector_runtime_oauth_clients (
    client_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    runtime_id TEXT NOT NULL,
    runtime_generation BIGINT NOT NULL CHECK (runtime_generation > 0),
    secret_hash BYTEA NOT NULL,
    scopes TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, workspace_id, runtime_id, runtime_generation)
);

CREATE INDEX IF NOT EXISTS ziggy_connector_runtime_oauth_clients_tenant_idx
    ON ziggy_connector_runtime_oauth_clients (user_id, workspace_id, runtime_id, runtime_generation);
