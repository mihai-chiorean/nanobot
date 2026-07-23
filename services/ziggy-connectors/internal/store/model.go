package store

import (
	"context"
	"errors"
	"sync"
	"time"
)

var ErrNotFound = errors.New("connector record not found")
var ErrReplay = errors.New("OAuth transaction already consumed")

type Tenant struct {
	UserID      string
	WorkspaceID string
}

func (t Tenant) Valid() bool { return t.UserID != "" && t.WorkspaceID != "" }

type Account struct {
	ID                    string
	Tenant                Tenant
	Provider              string
	ProviderSubject       string
	Email                 string
	Scopes                []string
	EncryptedRefreshToken []byte
	TokenKeyVersion       string
	Status                string
	LastError             string
	CreatedAt             time.Time
	UpdatedAt             time.Time
}

type OAuthTransaction struct {
	ID                    string
	Tenant                Tenant
	StateHash             []byte
	Nonce                 string
	EncryptedPKCEVerifier []byte
	RedirectURI           string
	ExpiresAt             time.Time
	ConsumedAt            *time.Time
}

// RuntimeOAuthClient is a credential held by one fenced runtime generation.
// Its tenant and runtime identity are never supplied by token requests.
type RuntimeOAuthClient struct {
	ClientID          string
	Tenant            Tenant
	RuntimeID         string
	RuntimeGeneration int64
	SecretHash        []byte
	Scopes            []string
	CreatedAt         time.Time
	UpdatedAt         time.Time
}

type AccountRepository interface {
	SaveAccount(context.Context, Tenant, Account) error
	ListAccounts(context.Context, Tenant) ([]Account, error)
	GetAccount(context.Context, Tenant, string) (Account, error)
}

type OAuthTransactionRepository interface {
	CreateOAuthTransaction(context.Context, Tenant, OAuthTransaction) error
	ConsumeOAuthTransaction(context.Context, Tenant, []byte) (OAuthTransaction, error)
}

type RuntimeOAuthClientRepository interface {
	UpsertRuntimeOAuthClient(context.Context, RuntimeOAuthClient) error
	GetRuntimeOAuthClient(context.Context, string) (RuntimeOAuthClient, error)
	GetRuntimeOAuthClientForRuntime(context.Context, Tenant, string, int64) (RuntimeOAuthClient, error)
}

type Readiness interface{ Ready(context.Context) error }

type MemoryRepository struct {
	mu                  sync.Mutex
	accounts            map[string]Account
	transactions        map[string]OAuthTransaction
	runtimeOAuthClients map[string]RuntimeOAuthClient
}

func NewMemoryRepository() *MemoryRepository {
	return &MemoryRepository{accounts: map[string]Account{}, transactions: map[string]OAuthTransaction{}, runtimeOAuthClients: map[string]RuntimeOAuthClient{}}
}

func tenantKey(t Tenant) string             { return t.UserID + "\x00" + t.WorkspaceID }
func accountKey(t Tenant, id string) string { return tenantKey(t) + "\x00" + id }

func (r *MemoryRepository) SaveAccount(_ context.Context, tenant Tenant, account Account) error {
	if !tenant.Valid() || account.ID == "" || account.Tenant != tenant {
		return errors.New("account tenant mismatch")
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	account.Tenant = tenant
	account.EncryptedRefreshToken = append([]byte(nil), account.EncryptedRefreshToken...)
	r.accounts[accountKey(tenant, account.ID)] = account
	return nil
}

func (r *MemoryRepository) ListAccounts(_ context.Context, tenant Tenant) ([]Account, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	var result []Account
	for _, account := range r.accounts {
		if account.Tenant == tenant {
			account.EncryptedRefreshToken = nil
			account.Scopes = append([]string(nil), account.Scopes...)
			result = append(result, account)
		}
	}
	return result, nil
}

func (r *MemoryRepository) GetAccount(_ context.Context, tenant Tenant, id string) (Account, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	account, ok := r.accounts[accountKey(tenant, id)]
	if !ok {
		return Account{}, ErrNotFound
	}
	return account, nil
}

func (r *MemoryRepository) CreateOAuthTransaction(_ context.Context, tenant Tenant, tx OAuthTransaction) error {
	if !tenant.Valid() || tx.ID == "" || tx.Tenant != tenant {
		return errors.New("OAuth transaction tenant mismatch")
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	tx.StateHash = append([]byte(nil), tx.StateHash...)
	tx.EncryptedPKCEVerifier = append([]byte(nil), tx.EncryptedPKCEVerifier...)
	r.transactions[tenantKey(tenant)+"\x00"+string(tx.StateHash)] = tx
	return nil
}

func (r *MemoryRepository) ConsumeOAuthTransaction(_ context.Context, tenant Tenant, stateHash []byte) (OAuthTransaction, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	tx, ok := r.transactions[tenantKey(tenant)+"\x00"+string(stateHash)]
	if !ok {
		return OAuthTransaction{}, ErrNotFound
	}
	if tx.ConsumedAt != nil || time.Now().After(tx.ExpiresAt) {
		return OAuthTransaction{}, ErrReplay
	}
	now := time.Now()
	tx.ConsumedAt = &now
	r.transactions[tenantKey(tenant)+"\x00"+string(stateHash)] = tx
	return tx, nil
}

func (r *MemoryRepository) UpsertRuntimeOAuthClient(_ context.Context, client RuntimeOAuthClient) error {
	if !client.Tenant.Valid() || client.ClientID == "" || client.RuntimeID == "" || client.RuntimeGeneration < 1 || len(client.SecretHash) == 0 {
		return errors.New("runtime OAuth client is invalid")
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	for id, existing := range r.runtimeOAuthClients {
		if id != client.ClientID && existing.Tenant == client.Tenant && existing.RuntimeID == client.RuntimeID {
			delete(r.runtimeOAuthClients, id)
		}
	}
	if client.CreatedAt.IsZero() {
		client.CreatedAt = time.Now()
	}
	client.UpdatedAt = time.Now()
	client.SecretHash = append([]byte(nil), client.SecretHash...)
	client.Scopes = append([]string(nil), client.Scopes...)
	r.runtimeOAuthClients[client.ClientID] = client
	return nil
}

func (r *MemoryRepository) GetRuntimeOAuthClient(_ context.Context, clientID string) (RuntimeOAuthClient, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	client, ok := r.runtimeOAuthClients[clientID]
	if !ok {
		return RuntimeOAuthClient{}, ErrNotFound
	}
	client.SecretHash = append([]byte(nil), client.SecretHash...)
	client.Scopes = append([]string(nil), client.Scopes...)
	return client, nil
}

func (r *MemoryRepository) GetRuntimeOAuthClientForRuntime(_ context.Context, tenant Tenant, runtimeID string, generation int64) (RuntimeOAuthClient, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	for _, client := range r.runtimeOAuthClients {
		if client.Tenant == tenant && client.RuntimeID == runtimeID && client.RuntimeGeneration == generation {
			client.SecretHash = append([]byte(nil), client.SecretHash...)
			client.Scopes = append([]string(nil), client.Scopes...)
			return client, nil
		}
	}
	return RuntimeOAuthClient{}, ErrNotFound
}

func (r *MemoryRepository) Ready(context.Context) error { return nil }

var _ AccountRepository = (*MemoryRepository)(nil)
var _ OAuthTransactionRepository = (*MemoryRepository)(nil)
var _ RuntimeOAuthClientRepository = (*MemoryRepository)(nil)
