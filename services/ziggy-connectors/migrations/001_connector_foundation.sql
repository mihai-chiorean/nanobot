-- Every connector row is keyed by both stable tenant dimensions. Do not
-- replace these predicates with account_id-only lookups.
CREATE TABLE IF NOT EXISTS ziggy_connector_accounts (
    user_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    provider_subject TEXT NOT NULL,
    email TEXT NOT NULL,
    scopes TEXT NOT NULL,
    encrypted_refresh_token BYTEA NOT NULL,
    token_key_version TEXT NOT NULL,
    status TEXT NOT NULL,
    last_error TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, workspace_id, account_id),
    UNIQUE (user_id, workspace_id, provider, provider_subject)
);

CREATE INDEX IF NOT EXISTS ziggy_connector_accounts_tenant_idx
    ON ziggy_connector_accounts (user_id, workspace_id, created_at);

CREATE TABLE IF NOT EXISTS ziggy_connector_oauth_transactions (
    user_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    transaction_id TEXT NOT NULL,
    state_hash BYTEA NOT NULL,
    nonce TEXT NOT NULL,
    encrypted_pkce_verifier BYTEA NOT NULL,
    redirect_uri TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ,
    PRIMARY KEY (user_id, workspace_id, state_hash),
    UNIQUE (user_id, workspace_id, transaction_id)
);

CREATE INDEX IF NOT EXISTS ziggy_connector_oauth_transactions_expiry_idx
    ON ziggy_connector_oauth_transactions (user_id, workspace_id, expires_at);
