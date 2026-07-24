package lifecycle

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sync"
	"testing"
	"time"

	_ "github.com/jackc/pgx/v5/stdlib"
	"github.com/mihai-chiorean/nanobot/services/ziggy-control/internal/identity"
)

func TestPostgresBootstrapLifecycleConcurrencyAndRevocation(t *testing.T) {
	databaseURL := os.Getenv("ZIGGY_CONTROL_TEST_DATABASE_URL")
	if databaseURL == "" {
		t.Skip("ZIGGY_CONTROL_TEST_DATABASE_URL is not set")
	}
	db, err := sql.Open("pgx", databaseURL)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	db.SetMaxOpenConns(24)
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	if err := applyMigration(ctx, db); err != nil {
		t.Fatal(err)
	}
	store, err := NewStore(db)
	if err != nil {
		t.Fatal(err)
	}
	if err := store.Ready(ctx); err != nil {
		t.Fatal(err)
	}

	suffix := fmt.Sprintf("%d", time.Now().UnixNano())
	email := "tenant-" + suffix + "@example.com"
	userID, err := store.Invite(ctx, email)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { cleanupUser(db, userID) })
	principal := identity.Principal{Subject: "clerk-" + suffix, Email: email}

	const contenders = 16
	allocations := make(chan string, contenders)
	errs := make(chan error, contenders)
	var group sync.WaitGroup
	for range contenders {
		group.Add(1)
		go func() {
			defer group.Done()
			allocation, err := store.Resolve(ctx, principal)
			if err == nil {
				allocations <- allocation.WorkspaceID
			}
			errs <- err
		}()
	}
	group.Wait()
	close(allocations)
	close(errs)
	var workspaceID string
	for err := range errs {
		if err != nil {
			t.Fatal(err)
		}
	}
	for workspace := range allocations {
		if workspaceID == "" {
			workspaceID = workspace
		}
		if workspace != workspaceID {
			t.Fatalf("bootstrap created multiple workspaces: %q and %q", workspaceID, workspace)
		}
	}
	var workspaces, runtimes int
	if err := db.QueryRowContext(ctx, `SELECT count(*) FROM ziggy_tenant_workspaces WHERE user_id=$1`, userID).Scan(&workspaces); err != nil {
		t.Fatal(err)
	}
	if err := db.QueryRowContext(ctx, `SELECT count(*) FROM ziggy_tenant_runtime_allocations WHERE user_id=$1`, userID).Scan(&runtimes); err != nil {
		t.Fatal(err)
	}
	if workspaces != 1 || runtimes != 1 {
		t.Fatalf("workspace/runtime count = %d/%d, want 1/1", workspaces, runtimes)
	}

	if _, err := db.ExecContext(ctx, `UPDATE ziggy_tenant_runtime_allocations SET allocation_state='active', upstream_url='http://127.0.0.1:8765', upstream_bootstrap_secret='01234567890123456789012345678901' WHERE user_id=$1`, userID); err != nil {
		t.Fatal(err)
	}
	if _, err := store.ResolveActive(ctx, userID, workspaceID); err != nil {
		t.Fatalf("active allocation = %v", err)
	}
	if err := store.Disable(ctx, userID); err != nil {
		t.Fatal(err)
	}
	if _, err := store.Resolve(ctx, principal); !errors.Is(err, ErrNotAuthorized) {
		t.Fatalf("disabled bootstrap error = %v", err)
	}
	if _, err := store.ResolveActive(ctx, userID, workspaceID); !errors.Is(err, ErrNotAuthorized) {
		t.Fatalf("disabled credential error = %v", err)
	}

	attackerEmail := "attacker-" + suffix + "@example.com"
	attackerID, err := store.Invite(ctx, attackerEmail)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { cleanupUser(db, attackerID) })
	if _, err := store.Resolve(ctx, identity.Principal{Subject: principal.Subject, Email: attackerEmail}); !errors.Is(err, ErrNotAuthorized) {
		t.Fatalf("subject takeover error = %v", err)
	}
	if _, err := store.Resolve(ctx, identity.Principal{Subject: "unknown-" + suffix, Email: "unknown-" + suffix + "@example.com"}); !errors.Is(err, ErrNotAuthorized) {
		t.Fatalf("uninvited bootstrap error = %v", err)
	}
}

func applyMigration(ctx context.Context, db *sql.DB) error {
	contents, err := os.ReadFile(filepath.Join("..", "..", "migrations", "001_tenant_lifecycle_foundation.sql"))
	if err != nil {
		return err
	}
	_, err = db.ExecContext(ctx, string(contents))
	return err
}

func cleanupUser(db *sql.DB, userID string) {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	_, _ = db.ExecContext(ctx, `DELETE FROM ziggy_tenant_lifecycle_events WHERE user_id=$1`, userID)
	_, _ = db.ExecContext(ctx, `DELETE FROM ziggy_tenant_runtime_allocations WHERE user_id=$1`, userID)
	_, _ = db.ExecContext(ctx, `DELETE FROM ziggy_tenant_workspaces WHERE user_id=$1`, userID)
	_, _ = db.ExecContext(ctx, `DELETE FROM ziggy_tenant_users WHERE user_id=$1`, userID)
}
