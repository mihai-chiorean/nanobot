package store

import (
	"context"
	"testing"
	"time"
)

func TestMemoryRepositoryScopesAccountsAndTransactionsToTenant(t *testing.T) {
	repo := NewMemoryRepository()
	a := Tenant{UserID: "user-a", WorkspaceID: "workspace-a"}
	b := Tenant{UserID: "user-b", WorkspaceID: "workspace-b"}
	account := Account{ID: "account", Tenant: a, EncryptedRefreshToken: []byte("ciphertext")}
	if err := repo.SaveAccount(context.Background(), a, account); err != nil {
		t.Fatal(err)
	}
	if _, err := repo.GetAccount(context.Background(), b, account.ID); err != ErrNotFound {
		t.Fatalf("cross-tenant account lookup = %v, want ErrNotFound", err)
	}
	tx := OAuthTransaction{ID: "tx", Tenant: a, StateHash: []byte("hash"), ExpiresAt: time.Now().Add(time.Minute)}
	if err := repo.CreateOAuthTransaction(context.Background(), a, tx); err != nil {
		t.Fatal(err)
	}
	if _, err := repo.ConsumeOAuthTransaction(context.Background(), b, tx.StateHash); err != ErrNotFound {
		t.Fatalf("cross-tenant transaction lookup = %v, want ErrNotFound", err)
	}
	if _, err := repo.ConsumeOAuthTransaction(context.Background(), a, tx.StateHash); err != nil {
		t.Fatal(err)
	}
	if _, err := repo.ConsumeOAuthTransaction(context.Background(), a, tx.StateHash); err != ErrReplay {
		t.Fatalf("replayed transaction = %v, want ErrReplay", err)
	}
}

func TestMemoryRepositoryScopesRuntimeOAuthClientsToRuntimeFence(t *testing.T) {
	repo := NewMemoryRepository()
	tenant := Tenant{UserID: "user-a", WorkspaceID: "workspace-a"}
	client := RuntimeOAuthClient{ClientID: "client-a", Tenant: tenant, RuntimeID: "runtime-a", RuntimeGeneration: 4, SecretHash: []byte("hash"), Scopes: []string{"gmail.search"}}
	if err := repo.UpsertRuntimeOAuthClient(context.Background(), client); err != nil {
		t.Fatal(err)
	}
	if _, err := repo.GetRuntimeOAuthClientForRuntime(context.Background(), Tenant{UserID: "user-b", WorkspaceID: "workspace-b"}, client.RuntimeID, client.RuntimeGeneration); err != ErrNotFound {
		t.Fatalf("cross-tenant runtime client lookup = %v, want ErrNotFound", err)
	}
	stored, err := repo.GetRuntimeOAuthClient(context.Background(), client.ClientID)
	if err != nil || stored.Tenant != tenant || stored.RuntimeGeneration != 4 || len(stored.Scopes) != 1 {
		t.Fatalf("stored client = %#v, error = %v", stored, err)
	}
	next := RuntimeOAuthClient{ClientID: "client-b", Tenant: tenant, RuntimeID: client.RuntimeID, RuntimeGeneration: 5, SecretHash: []byte("next-hash"), Scopes: []string{"gmail.search"}}
	if err := repo.UpsertRuntimeOAuthClient(context.Background(), next); err != nil {
		t.Fatal(err)
	}
	if _, err := repo.GetRuntimeOAuthClient(context.Background(), client.ClientID); err != ErrNotFound {
		t.Fatalf("stale runtime client lookup = %v, want ErrNotFound", err)
	}
	if _, err := repo.GetRuntimeOAuthClient(context.Background(), next.ClientID); err != nil {
		t.Fatalf("active runtime client lookup = %v", err)
	}
}
