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
	db, store, ctx := integrationStore(t)

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

func TestPostgresInviteIsConcurrentIdempotent(t *testing.T) {
	db, store, ctx := integrationStore(t)
	suffix := fmt.Sprintf("%d", time.Now().UnixNano())
	email := "invite-" + suffix + "@example.com"

	const contenders = 24
	ids := make(chan string, contenders)
	errs := make(chan error, contenders)
	var group sync.WaitGroup
	for range contenders {
		group.Add(1)
		go func() {
			defer group.Done()
			id, err := store.Invite(ctx, email)
			ids <- id
			errs <- err
		}()
	}
	group.Wait()
	close(ids)
	close(errs)
	var userID string
	for err := range errs {
		if err != nil {
			t.Fatal(err)
		}
	}
	for id := range ids {
		if userID == "" {
			userID = id
		}
		if id != userID {
			t.Fatalf("concurrent Invite returned %q and %q", userID, id)
		}
	}
	t.Cleanup(func() { cleanupUser(db, userID) })
	var users, invitationEvents int
	if err := db.QueryRowContext(ctx, `SELECT count(*) FROM ziggy_tenant_users WHERE expected_email=$1`, email).Scan(&users); err != nil {
		t.Fatal(err)
	}
	if err := db.QueryRowContext(ctx, `SELECT count(*) FROM ziggy_tenant_lifecycle_events WHERE user_id=$1 AND event_type='user_invited'`, userID).Scan(&invitationEvents); err != nil {
		t.Fatal(err)
	}
	if users != 1 || invitationEvents != 1 {
		t.Fatalf("users/invitation events = %d/%d, want 1/1", users, invitationEvents)
	}
}

func TestPostgresRuntimeActivationAndDeletionTransitions(t *testing.T) {
	db, store, ctx := integrationStore(t)
	suffix := fmt.Sprintf("%d", time.Now().UnixNano())
	email := "lifecycle-" + suffix + "@example.com"
	userID, err := store.Invite(ctx, email)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { cleanupUser(db, userID) })
	principal := identity.Principal{Subject: "clerk-lifecycle-" + suffix, Email: email}
	allocation, err := store.Resolve(ctx, principal)
	if err != nil {
		t.Fatal(err)
	}
	var runtimeID string
	var generation int64
	if err := db.QueryRowContext(ctx, `SELECT runtime_id, generation FROM ziggy_tenant_runtime_allocations WHERE user_id=$1`, userID).Scan(&runtimeID, &generation); err != nil {
		t.Fatal(err)
	}
	activation := RuntimeActivation{
		UserID: userID, WorkspaceID: allocation.WorkspaceID, RuntimeID: runtimeID, ExpectedGeneration: generation,
		UpstreamURL: "http://127.0.0.1:8765", UpstreamBootstrapSecret: "01234567890123456789012345678901",
	}
	wrongGeneration := activation
	wrongGeneration.ExpectedGeneration++
	if err := store.ActivateRuntime(ctx, wrongGeneration); !errors.Is(err, ErrActivationMismatch) {
		t.Fatalf("wrong generation activation error = %v", err)
	}
	if err := store.MarkDeleted(ctx, userID); !errors.Is(err, ErrInvalidTransition) {
		t.Fatalf("direct deletion error = %v", err)
	}
	if err := store.ActivateRuntime(ctx, activation); err != nil {
		t.Fatal(err)
	}
	if _, err := store.ResolveActive(ctx, userID, allocation.WorkspaceID); err != nil {
		t.Fatalf("active runtime resolution = %v", err)
	}
	if err := store.MarkDeletionPending(ctx, userID); err != nil {
		t.Fatal(err)
	}
	if _, err := store.Resolve(ctx, principal); !errors.Is(err, ErrNotAuthorized) {
		t.Fatalf("deletion pending bootstrap error = %v", err)
	}
	if err := store.ActivateRuntime(ctx, activation); !errors.Is(err, ErrActivationMismatch) {
		t.Fatalf("repeat activation error = %v", err)
	}
	if err := store.MarkDeleted(ctx, userID); err != nil {
		t.Fatal(err)
	}
	var status, runtimeState string
	if err := db.QueryRowContext(ctx, `SELECT u.lifecycle_status, r.allocation_state FROM ziggy_tenant_users u JOIN ziggy_tenant_runtime_allocations r ON r.user_id=u.user_id WHERE u.user_id=$1`, userID).Scan(&status, &runtimeState); err != nil {
		t.Fatal(err)
	}
	if status != "deleted" || runtimeState != "deleted" {
		t.Fatalf("deleted status/runtime = %q/%q", status, runtimeState)
	}
	if err := store.MarkDeletionPending(ctx, userID); !errors.Is(err, ErrInvalidTransition) {
		t.Fatalf("deleted pending transition error = %v", err)
	}
}

func integrationStore(t *testing.T) (*sql.DB, *Store, context.Context) {
	t.Helper()
	databaseURL := os.Getenv("ZIGGY_CONTROL_TEST_DATABASE_URL")
	if databaseURL == "" {
		t.Skip("ZIGGY_CONTROL_TEST_DATABASE_URL is not set")
	}
	db, err := sql.Open("pgx", databaseURL)
	if err != nil {
		t.Fatal(err)
	}
	db.SetMaxOpenConns(32)
	t.Cleanup(func() { _ = db.Close() })
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	t.Cleanup(cancel)
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
	return db, store, ctx
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
