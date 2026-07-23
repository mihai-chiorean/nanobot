package store

import (
	"context"
	"database/sql"
	"fmt"
	"os"
	"sync"
	"testing"
	"time"

	_ "github.com/jackc/pgx/v5/stdlib"
)

func TestPostgresRegistersOneRuntimeClientUnderConcurrency(t *testing.T) {
	databaseURL := os.Getenv("ZIGGY_CONNECTORS_TEST_DATABASE_URL")
	if databaseURL == "" {
		t.Skip("ZIGGY_CONNECTORS_TEST_DATABASE_URL is not set")
	}
	db, err := sql.Open("pgx", databaseURL)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	db.SetMaxOpenConns(16)
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	if err := db.PingContext(ctx); err != nil {
		t.Fatal(err)
	}
	repository, err := NewPostgresRepository(db)
	if err != nil {
		t.Fatal(err)
	}
	suffix := fmt.Sprintf("%d", time.Now().UnixNano())
	tenant := Tenant{UserID: "integration-user-" + suffix, WorkspaceID: "integration-workspace-" + suffix}
	runtimeID := "integration-runtime-" + suffix
	t.Cleanup(func() {
		_, _ = db.ExecContext(context.Background(), `
DELETE FROM ziggy_connector_runtime_oauth_clients
WHERE user_id=$1 AND workspace_id=$2 AND runtime_id=$3`,
			tenant.UserID, tenant.WorkspaceID, runtimeID)
	})

	const contenders = 16
	results := make(chan RuntimeOAuthClient, contenders)
	created := make(chan bool, contenders)
	errs := make(chan error, contenders)
	var group sync.WaitGroup
	for index := 0; index < contenders; index++ {
		group.Add(1)
		go func(index int) {
			defer group.Done()
			client := RuntimeOAuthClient{
				ClientID:          fmt.Sprintf("integration-client-%s-%d", suffix, index),
				Tenant:            tenant,
				RuntimeID:         runtimeID,
				RuntimeGeneration: int64(index + 1),
				SecretHash:        []byte(fmt.Sprintf("integration-hash-%d", index)),
				Scopes:            []string{"gmail.status", "gmail.search", "gmail.read"},
				CreatedAt:         time.Now().UTC(),
			}
			active, wasCreated, err := repository.RegisterRuntimeOAuthClient(ctx, client)
			results <- active
			created <- wasCreated
			errs <- err
		}(index)
	}
	group.Wait()
	close(results)
	close(created)
	close(errs)

	for err := range errs {
		if err != nil {
			t.Fatal(err)
		}
	}
	createdCount := 0
	for wasCreated := range created {
		if wasCreated {
			createdCount++
		}
	}
	var activeID string
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
	var rowCount int
	if err := db.QueryRowContext(ctx, `
SELECT count(*) FROM ziggy_connector_runtime_oauth_clients
WHERE user_id=$1 AND workspace_id=$2 AND runtime_id=$3`,
		tenant.UserID, tenant.WorkspaceID, runtimeID).Scan(&rowCount); err != nil {
		t.Fatal(err)
	}
	if rowCount != 1 {
		t.Fatalf("stored %d active clients, want 1", rowCount)
	}
}
