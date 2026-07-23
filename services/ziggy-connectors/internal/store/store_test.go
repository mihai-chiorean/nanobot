package store

import (
	"context"
	"fmt"
	"sync"
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
	if _, created, err := repo.RegisterRuntimeOAuthClient(context.Background(), client); err != nil || !created {
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
	active, created, err := repo.RegisterRuntimeOAuthClient(context.Background(), next)
	if err != nil {
		t.Fatal(err)
	}
	if created || active.ClientID != client.ClientID {
		t.Fatalf("replacement registration = %#v, created = %t", active, created)
	}
	if _, err := repo.GetRuntimeOAuthClient(context.Background(), next.ClientID); err != ErrNotFound {
		t.Fatalf("replacement client lookup = %v, want ErrNotFound", err)
	}
}

func TestRuntimeOAuthLockKeyAcceptsTenantSeparator(t *testing.T) {
	tenant := Tenant{UserID: "user-a", WorkspaceID: "workspace-a"}
	if runtimeOAuthLockKey(tenant, "runtime-a") == runtimeOAuthLockKey(tenant, "runtime-b") {
		t.Fatal("runtime lock keys collided")
	}
}

func TestMemoryRepositoryRegistersOneRuntimeClientUnderConcurrency(t *testing.T) {
	repo := NewMemoryRepository()
	tenant := Tenant{UserID: "user-a", WorkspaceID: "workspace-a"}
	const contenders = 32
	results := make(chan RuntimeOAuthClient, contenders)
	created := make(chan bool, contenders)
	errs := make(chan error, contenders)
	var group sync.WaitGroup
	for index := 0; index < contenders; index++ {
		group.Add(1)
		go func(index int) {
			defer group.Done()
			client := RuntimeOAuthClient{
				ClientID:          fmt.Sprintf("client-%d", index),
				Tenant:            tenant,
				RuntimeID:         "runtime-a",
				RuntimeGeneration: int64(index + 1),
				SecretHash:        []byte(fmt.Sprintf("hash-%d", index)),
				Scopes:            []string{"gmail.read"},
			}
			active, wasCreated, err := repo.RegisterRuntimeOAuthClient(context.Background(), client)
			results <- active
			created <- wasCreated
			errs <- err
		}(index)
	}
	group.Wait()
	close(results)
	close(created)
	close(errs)

	var activeID string
	createdCount := 0
	for err := range errs {
		if err != nil {
			t.Fatal(err)
		}
	}
	for wasCreated := range created {
		if wasCreated {
			createdCount++
		}
	}
	for active := range results {
		if activeID == "" {
			activeID = active.ClientID
		}
		if active.ClientID != activeID {
			t.Fatalf("observed multiple active clients: %q and %q", activeID, active.ClientID)
		}
	}
	if createdCount != 1 {
		t.Fatalf("created %d clients, want 1", createdCount)
	}
}
