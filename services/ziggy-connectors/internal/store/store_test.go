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
