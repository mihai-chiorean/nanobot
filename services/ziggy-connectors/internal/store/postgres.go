package store

import (
	"context"
	"database/sql"
	"errors"
	"strings"
	"time"
)

type PostgresRepository struct{ db *sql.DB }

func NewPostgresRepository(db *sql.DB) (*PostgresRepository, error) {
	if db == nil {
		return nil, errors.New("database handle is required")
	}
	return &PostgresRepository{db: db}, nil
}

func (r *PostgresRepository) Ready(ctx context.Context) error { return r.db.PingContext(ctx) }

func (r *PostgresRepository) SaveAccount(ctx context.Context, tenant Tenant, account Account) error {
	if !tenant.Valid() || account.ID == "" || account.Tenant != tenant {
		return errors.New("account tenant mismatch")
	}
	createdAt := account.CreatedAt
	if createdAt.IsZero() {
		createdAt = time.Now()
	}
	_, err := r.db.ExecContext(ctx, `
INSERT INTO ziggy_connector_accounts
 (user_id, workspace_id, account_id, provider, provider_subject, email, scopes,
  encrypted_refresh_token, token_key_version, status, last_error, created_at, updated_at)
VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,now())
ON CONFLICT (user_id, workspace_id, account_id) DO UPDATE SET
 provider=EXCLUDED.provider, provider_subject=EXCLUDED.provider_subject, email=EXCLUDED.email,
 scopes=EXCLUDED.scopes, encrypted_refresh_token=EXCLUDED.encrypted_refresh_token,
 token_key_version=EXCLUDED.token_key_version, status=EXCLUDED.status,
 last_error=EXCLUDED.last_error, updated_at=now()
WHERE ziggy_connector_accounts.user_id=$1 AND ziggy_connector_accounts.workspace_id=$2`,
		tenant.UserID, tenant.WorkspaceID, account.ID, account.Provider, account.ProviderSubject, account.Email,
		strings.Join(account.Scopes, " "), account.EncryptedRefreshToken, account.TokenKeyVersion, account.Status, account.LastError, createdAt)
	return err
}

func (r *PostgresRepository) ListAccounts(ctx context.Context, tenant Tenant) ([]Account, error) {
	rows, err := r.db.QueryContext(ctx, `SELECT account_id, provider, provider_subject, email, scopes, token_key_version, status, last_error, created_at, updated_at FROM ziggy_connector_accounts WHERE user_id=$1 AND workspace_id=$2 ORDER BY created_at`, tenant.UserID, tenant.WorkspaceID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var result []Account
	for rows.Next() {
		var account Account
		var scopes string
		if err := rows.Scan(&account.ID, &account.Provider, &account.ProviderSubject, &account.Email, &scopes, &account.TokenKeyVersion, &account.Status, &account.LastError, &account.CreatedAt, &account.UpdatedAt); err != nil {
			return nil, err
		}
		account.Tenant = tenant
		account.Scopes = strings.Fields(scopes)
		result = append(result, account)
	}
	return result, rows.Err()
}

func (r *PostgresRepository) GetAccount(ctx context.Context, tenant Tenant, id string) (Account, error) {
	var account Account
	var scopes string
	err := r.db.QueryRowContext(ctx, `SELECT account_id, provider, provider_subject, email, encrypted_refresh_token, scopes, token_key_version, status, last_error, created_at, updated_at FROM ziggy_connector_accounts WHERE user_id=$1 AND workspace_id=$2 AND account_id=$3`, tenant.UserID, tenant.WorkspaceID, id).Scan(&account.ID, &account.Provider, &account.ProviderSubject, &account.Email, &account.EncryptedRefreshToken, &scopes, &account.TokenKeyVersion, &account.Status, &account.LastError, &account.CreatedAt, &account.UpdatedAt)
	if errors.Is(err, sql.ErrNoRows) {
		return Account{}, ErrNotFound
	}
	account.Tenant = tenant
	account.Scopes = strings.Fields(scopes)
	return account, err
}

func (r *PostgresRepository) CreateOAuthTransaction(ctx context.Context, tenant Tenant, tx OAuthTransaction) error {
	if !tenant.Valid() || tx.ID == "" || tx.Tenant != tenant {
		return errors.New("OAuth transaction tenant mismatch")
	}
	_, err := r.db.ExecContext(ctx, `INSERT INTO ziggy_connector_oauth_transactions (user_id, workspace_id, transaction_id, state_hash, nonce, encrypted_pkce_verifier, redirect_uri, expires_at) VALUES ($1,$2,$3,$4,$5,$6,$7,$8)`, tenant.UserID, tenant.WorkspaceID, tx.ID, tx.StateHash, tx.Nonce, tx.EncryptedPKCEVerifier, tx.RedirectURI, tx.ExpiresAt)
	return err
}

func (r *PostgresRepository) ConsumeOAuthTransaction(ctx context.Context, tenant Tenant, stateHash []byte) (OAuthTransaction, error) {
	tx, err := r.db.BeginTx(ctx, nil)
	if err != nil {
		return OAuthTransaction{}, err
	}
	defer tx.Rollback()
	var record OAuthTransaction
	var consumed sql.NullTime
	err = tx.QueryRowContext(ctx, `SELECT transaction_id, state_hash, nonce, encrypted_pkce_verifier, redirect_uri, expires_at, consumed_at FROM ziggy_connector_oauth_transactions WHERE user_id=$1 AND workspace_id=$2 AND state_hash=$3 FOR UPDATE`, tenant.UserID, tenant.WorkspaceID, stateHash).Scan(&record.ID, &record.StateHash, &record.Nonce, &record.EncryptedPKCEVerifier, &record.RedirectURI, &record.ExpiresAt, &consumed)
	if errors.Is(err, sql.ErrNoRows) {
		return OAuthTransaction{}, ErrNotFound
	}
	if err != nil {
		return OAuthTransaction{}, err
	}
	if consumed.Valid || time.Now().After(record.ExpiresAt) {
		return OAuthTransaction{}, ErrReplay
	}
	if _, err := tx.ExecContext(ctx, `UPDATE ziggy_connector_oauth_transactions SET consumed_at=now() WHERE user_id=$1 AND workspace_id=$2 AND state_hash=$3 AND consumed_at IS NULL`, tenant.UserID, tenant.WorkspaceID, stateHash); err != nil {
		return OAuthTransaction{}, err
	}
	if err := tx.Commit(); err != nil {
		return OAuthTransaction{}, err
	}
	now := time.Now()
	record.ConsumedAt = &now
	record.Tenant = tenant
	return record, nil
}

var _ AccountRepository = (*PostgresRepository)(nil)
var _ OAuthTransactionRepository = (*PostgresRepository)(nil)
var _ Readiness = (*PostgresRepository)(nil)
