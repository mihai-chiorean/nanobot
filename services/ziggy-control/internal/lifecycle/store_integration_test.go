package lifecycle

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgconn"
	_ "github.com/jackc/pgx/v5/stdlib"
	"github.com/mihai-chiorean/nanobot/services/ziggy-control/internal/identity"
	"github.com/mihai-chiorean/nanobot/services/ziggy-control/internal/tenant"
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
	if err := store.MarkDeleted(ctx, userID, "receipt-before-pending"); !errors.Is(err, ErrInvalidTransition) {
		t.Fatalf("direct deletion error = %v", err)
	}
	if err := store.ActivateRuntime(ctx, activation); err != nil {
		t.Fatal(err)
	}
	if _, err := store.ResolveActive(ctx, userID, allocation.WorkspaceID); err != nil {
		t.Fatalf("active runtime resolution = %v", err)
	}
	if _, err := db.ExecContext(ctx, `UPDATE ziggy_tenant_runtime_allocations SET metadata='{"runtime_token":"sensitive"}'::jsonb WHERE user_id=$1`, userID); err != nil {
		t.Fatal(err)
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
	if err := store.MarkDeleted(ctx, userID, " "); !errors.Is(err, ErrDestructionReceiptRequired) {
		t.Fatalf("empty destruction receipt error = %v", err)
	}
	receipt := "destruction-job-" + suffix
	if err := store.MarkDeleted(ctx, userID, receipt); err != nil {
		t.Fatal(err)
	}
	if err := store.MarkDeleted(ctx, userID, receipt); err != nil {
		t.Fatalf("idempotent deletion retry = %v", err)
	}
	if err := store.MarkDeleted(ctx, userID, receipt+"-different"); !errors.Is(err, ErrDestructionReceiptMismatch) {
		t.Fatalf("mismatched deletion retry error = %v", err)
	}
	var status, expectedEmail, storedReceipt, runtimeState, upstreamURL, bootstrapSecret, metadata, storedRuntimeID string
	var clerkSubject sql.NullString
	if err := db.QueryRowContext(ctx, `SELECT
    u.lifecycle_status, u.expected_email, u.clerk_subject, u.destruction_receipt,
    r.allocation_state, r.upstream_url, r.upstream_bootstrap_secret, r.metadata::text, r.runtime_id
FROM ziggy_tenant_users u
JOIN ziggy_tenant_runtime_allocations r ON r.user_id=u.user_id
WHERE u.user_id=$1`, userID).Scan(
		&status, &expectedEmail, &clerkSubject, &storedReceipt,
		&runtimeState, &upstreamURL, &bootstrapSecret, &metadata, &storedRuntimeID,
	); err != nil {
		t.Fatal(err)
	}
	wantReceipt, err := destructionReceiptDigest(receipt)
	if err != nil {
		t.Fatal(err)
	}
	if status != "deleted" || expectedEmail != deletedEmailTombstone(userID) || clerkSubject.Valid || storedReceipt != wantReceipt {
		t.Fatalf("deleted user tombstone = status %q email %q subject %+v receipt %q", status, expectedEmail, clerkSubject, storedReceipt)
	}
	if runtimeState != "deleted" || upstreamURL != "" || bootstrapSecret != "" || metadata != "{}" || storedRuntimeID != runtimeID {
		t.Fatalf("deleted runtime tombstone = state %q url %q secret %q metadata %q runtime %q", runtimeState, upstreamURL, bootstrapSecret, metadata, storedRuntimeID)
	}
	var deletionEvents int
	if err := db.QueryRowContext(ctx, `SELECT count(*) FROM ziggy_tenant_lifecycle_events WHERE user_id=$1 AND event_type='user_deleted'`, userID).Scan(&deletionEvents); err != nil {
		t.Fatal(err)
	}
	if deletionEvents != 1 {
		t.Fatalf("user_deleted events = %d, want 1", deletionEvents)
	}
	if err := store.MarkDeletionPending(ctx, userID); !errors.Is(err, ErrInvalidTransition) {
		t.Fatalf("deleted pending transition error = %v", err)
	}
}

func TestPostgresRuntimeActivationRejectsSharedEndpointOrSecret(t *testing.T) {
	db, store, ctx := integrationStore(t)
	suffix := fmt.Sprintf("%d", time.Now().UnixNano())
	firstID, first := pendingActivation(t, db, store, ctx, "first-"+suffix+"@example.com", "clerk-first-"+suffix)
	secondID, second := pendingActivation(t, db, store, ctx, "second-"+suffix+"@example.com", "clerk-second-"+suffix)
	thirdID, third := pendingActivation(t, db, store, ctx, "third-"+suffix+"@example.com", "clerk-third-"+suffix)
	t.Cleanup(func() { cleanupUser(db, firstID) })
	t.Cleanup(func() { cleanupUser(db, secondID) })
	t.Cleanup(func() { cleanupUser(db, thirdID) })

	first.UpstreamURL = "http://127.0.0.1:8765"
	first.UpstreamBootstrapSecret = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
	if err := store.ActivateRuntime(ctx, first); err != nil {
		t.Fatal(err)
	}
	second.UpstreamURL = first.UpstreamURL
	second.UpstreamBootstrapSecret = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
	if err := store.ActivateRuntime(ctx, second); !errors.Is(err, ErrActivationConflict) {
		t.Fatalf("shared upstream URL activation error = %v", err)
	}
	third.UpstreamURL = "http://127.0.0.1:8766"
	third.UpstreamBootstrapSecret = first.UpstreamBootstrapSecret
	if err := store.ActivateRuntime(ctx, third); !errors.Is(err, ErrActivationConflict) {
		t.Fatalf("shared bootstrap secret activation error = %v", err)
	}

	var firstState, firstURL, firstSecret string
	if err := db.QueryRowContext(ctx, `SELECT allocation_state, upstream_url, upstream_bootstrap_secret FROM ziggy_tenant_runtime_allocations WHERE user_id=$1`, firstID).Scan(&firstState, &firstURL, &firstSecret); err != nil {
		t.Fatal(err)
	}
	if firstState != "active" || firstURL != first.UpstreamURL || firstSecret != first.UpstreamBootstrapSecret {
		t.Fatalf("first runtime changed after conflict: state=%q url=%q secret=%q", firstState, firstURL, firstSecret)
	}
	for _, userID := range []string{secondID, thirdID} {
		var state, upstreamURL, secret string
		if err := db.QueryRowContext(ctx, `SELECT allocation_state, upstream_url, upstream_bootstrap_secret FROM ziggy_tenant_runtime_allocations WHERE user_id=$1`, userID).Scan(&state, &upstreamURL, &secret); err != nil {
			t.Fatal(err)
		}
		if state != "pending" || upstreamURL != "" || secret != "" {
			t.Fatalf("conflicting runtime %q changed: state=%q url=%q secret=%q", userID, state, upstreamURL, secret)
		}
	}
}

func TestPostgresReadyRequiresCompleteSchema(t *testing.T) {
	db, _, ctx := integrationStore(t)
	defer func() {
		if err := applyMigration(ctx, db); err != nil {
			t.Errorf("restore lifecycle migrations: %v", err)
		}
	}()
	if _, err := db.ExecContext(ctx, `DROP INDEX ziggy_tenant_runtime_allocations_upstream_url_unique, ziggy_tenant_runtime_allocations_bootstrap_secret_unique`); err != nil {
		t.Fatal(err)
	}

	store, err := NewStore(db)
	if err != nil {
		t.Fatal(err)
	}
	if err := store.Ready(ctx); err == nil {
		t.Fatal("Ready succeeded with only migration 001 applied")
	}
	if err := applyMigration(ctx, db); err != nil {
		t.Fatal(err)
	}
	if err := store.Ready(ctx); err != nil {
		t.Fatalf("Ready after complete migrations = %v", err)
	}
}

func TestPostgresReadyRejectsWrongSchemaIdentity(t *testing.T) {
	db, store, ctx := integrationStore(t)
	if _, err := db.ExecContext(ctx, `UPDATE ziggy_schema_identity SET migration_set_sha256=repeat('0',64) WHERE component=$1`, schemaIdentityComponent); err != nil {
		t.Fatal(err)
	}
	defer func() {
		if err := applyMigrationFile(ctx, db, "004_schema_identity.sql"); err != nil {
			t.Errorf("restore schema identity: %v", err)
		}
	}()
	if err := store.Ready(ctx); err == nil {
		t.Fatal("Ready succeeded with a mismatched schema identity")
	}
	if err := applyMigrationFile(ctx, db, "004_schema_identity.sql"); err != nil {
		t.Fatal(err)
	}
	if err := store.Ready(ctx); err != nil {
		t.Fatalf("Ready after restoring schema identity = %v", err)
	}
}

func TestPostgresAdmissionTenantCountTracksAdmittedStates(t *testing.T) {
	db, store, ctx := integrationStore(t)
	before, err := store.AdmissionTenantCount(ctx)
	if err != nil {
		t.Fatal(err)
	}
	suffix := fmt.Sprintf("%d", time.Now().UnixNano())
	firstID, err := store.Invite(ctx, "count-a-"+suffix+"@example.com")
	if err != nil {
		t.Fatal(err)
	}
	secondID, err := store.Invite(ctx, "count-b-"+suffix+"@example.com")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { cleanupUser(db, firstID) })
	t.Cleanup(func() { cleanupUser(db, secondID) })
	afterInvites, err := store.AdmissionTenantCount(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if afterInvites != before+2 {
		t.Fatalf("admission tenant count after invites = %d, want %d", afterInvites, before+2)
	}
	if err := store.Disable(ctx, firstID); err != nil {
		t.Fatal(err)
	}
	afterDisable, err := store.AdmissionTenantCount(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if afterDisable != before+1 {
		t.Fatalf("admission tenant count after disable = %d, want %d", afterDisable, before+1)
	}
}

func TestPostgresLegacyImportDryRunAndRepeat(t *testing.T) {
	db, store, ctx := integrationStore(t)
	suffix := fmt.Sprintf("%d", time.Now().UnixNano())
	userID := "usr_import_" + suffix
	workspaceID := "ws_import_" + suffix
	registry := legacyImportRegistry(t, []map[string]any{legacyTenant(userID, workspaceID, "import-"+suffix+"@example.com", "http://127.0.0.1:8765", "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", true)}, []map[string]any{{"user_id": userID, "clerk_subject": "clerk-import-" + suffix, "bound_at": time.Now().UTC()}})
	allocations := registry.ImportAllocations()

	dryRun, err := store.ImportLegacyAllocations(ctx, allocations, true)
	if err != nil {
		t.Fatal(err)
	}
	if dryRun.UsersCreated != 1 || dryRun.WorkspacesCreated != 1 || dryRun.RuntimesCreated != 1 {
		t.Fatalf("dry run result = %+v", dryRun)
	}
	var count int
	if err := db.QueryRowContext(ctx, `SELECT count(*) FROM ziggy_tenant_users WHERE user_id=$1`, userID).Scan(&count); err != nil {
		t.Fatal(err)
	}
	if count != 0 {
		t.Fatalf("dry run persisted %d users", count)
	}

	first, err := store.ImportLegacyAllocations(ctx, allocations, false)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { cleanupUser(db, userID) })
	if first.UsersCreated != 1 || first.WorkspacesCreated != 1 || first.RuntimesCreated != 1 {
		t.Fatalf("first import result = %+v", first)
	}
	second, err := store.ImportLegacyAllocations(ctx, allocations, false)
	if err != nil {
		t.Fatal(err)
	}
	if second.UsersExisting != 1 || second.WorkspacesExisting != 1 || second.RuntimesExisting != 1 || second.UsersCreated != 0 || second.WorkspacesCreated != 0 || second.RuntimesCreated != 0 {
		t.Fatalf("repeat import result = %+v", second)
	}
	var subject, runtimeState string
	if err := db.QueryRowContext(ctx, `SELECT u.clerk_subject, r.allocation_state FROM ziggy_tenant_users u JOIN ziggy_tenant_runtime_allocations r ON r.user_id=u.user_id WHERE u.user_id=$1`, userID).Scan(&subject, &runtimeState); err != nil {
		t.Fatal(err)
	}
	if subject != "clerk-import-"+suffix || runtimeState != "active" {
		t.Fatalf("imported subject/runtime = %q/%q", subject, runtimeState)
	}
}

func TestPostgresLegacyImportConflictRollsBackBatch(t *testing.T) {
	db, store, ctx := integrationStore(t)
	suffix := fmt.Sprintf("%d", time.Now().UnixNano())
	firstUserID := "usr_batch_one_" + suffix
	secondUserID := "usr_batch_two_" + suffix
	registry := legacyImportRegistry(t, []map[string]any{
		legacyTenant(firstUserID, "ws_batch_one_"+suffix, "batch-one-"+suffix+"@example.com", "http://127.0.0.1:8765", "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", true),
		legacyTenant(secondUserID, "ws_batch_two_"+suffix, "batch-two-"+suffix+"@example.com", "http://127.0.0.1:8766", "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", false),
	}, nil)
	if _, err := db.ExecContext(ctx, `INSERT INTO ziggy_tenant_users (user_id, expected_email, lifecycle_status, created_at, updated_at) VALUES ($1,$2,'active',now(),now())`, secondUserID, "conflict-"+suffix+"@example.com"); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { cleanupUser(db, secondUserID) })
	if _, err := store.ImportLegacyAllocations(ctx, registry.ImportAllocations(), false); !errors.Is(err, ErrImportConflict) {
		t.Fatalf("conflicting import error = %v", err)
	}
	var firstCount int
	if err := db.QueryRowContext(ctx, `SELECT count(*) FROM ziggy_tenant_users WHERE user_id=$1`, firstUserID).Scan(&firstCount); err != nil {
		t.Fatal(err)
	}
	if firstCount != 0 {
		t.Fatalf("batch conflict left %d imported first users", firstCount)
	}
}

func TestPostgresConcurrentIdenticalLegacyImportsConverge(t *testing.T) {
	db, store, ctx := integrationStore(t)
	suffix := fmt.Sprintf("%d", time.Now().UnixNano())
	firstUserID := "usr_concurrent_a_" + suffix
	secondUserID := "usr_concurrent_b_" + suffix
	basePort := 10000 + int(time.Now().UnixNano()%40000)
	registry := legacyImportRegistry(t, []map[string]any{
		legacyTenant(firstUserID, "ws_concurrent_a_"+suffix, "concurrent-a-"+suffix+"@example.com", fmt.Sprintf("http://127.0.0.1:%d", basePort), "a"+suffix+"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", true),
		legacyTenant(secondUserID, "ws_concurrent_b_"+suffix, "concurrent-b-"+suffix+"@example.com", fmt.Sprintf("http://127.0.0.1:%d", basePort+1), "b"+suffix+"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", false),
	}, nil)
	t.Cleanup(func() { cleanupUser(db, firstUserID) })
	t.Cleanup(func() { cleanupUser(db, secondUserID) })

	first := registry.ImportAllocations()
	second := append([]tenant.Allocation(nil), first...)
	second[0], second[1] = second[1], second[0]
	start := make(chan struct{})
	results := make(chan ImportResult, 2)
	errs := make(chan error, 2)
	var group sync.WaitGroup
	for _, allocations := range [][]tenant.Allocation{first, second} {
		group.Add(1)
		go func(allocations []tenant.Allocation) {
			defer group.Done()
			<-start
			result, err := store.ImportLegacyAllocations(ctx, allocations, false)
			results <- result
			errs <- err
		}(allocations)
	}
	close(start)
	group.Wait()
	close(results)
	close(errs)
	for err := range errs {
		if err != nil {
			t.Fatalf("concurrent import: %v", err)
		}
	}
	var created, existing int
	for result := range results {
		created += result.UsersCreated + result.WorkspacesCreated + result.RuntimesCreated
		existing += result.UsersExisting + result.WorkspacesExisting + result.RuntimesExisting
	}
	if created != 6 || existing != 6 {
		t.Fatalf("aggregate created/existing = %d/%d, want 6/6", created, existing)
	}
	if second[0].UserID != secondUserID || second[1].UserID != firstUserID {
		t.Fatal("import reordered the caller's allocation slice")
	}
	var events int
	if err := db.QueryRowContext(ctx, `SELECT count(*) FROM ziggy_tenant_lifecycle_events WHERE user_id IN ($1,$2)`, firstUserID, secondUserID).Scan(&events); err != nil {
		t.Fatal(err)
	}
	if events != 6 {
		t.Fatalf("import events = %d, want 6", events)
	}
}

func TestImportRetryClassification(t *testing.T) {
	for _, test := range []struct {
		code string
		want bool
	}{
		{code: "40001", want: true},
		{code: "40P01", want: true},
		{code: "23505", want: false},
		{code: "23503", want: false},
	} {
		if got := retryableImportError(&pgconn.PgError{Code: test.code}); got != test.want {
			t.Errorf("SQLSTATE %s retryable = %v, want %v", test.code, got, test.want)
		}
	}
}

func TestPostgresLeastPrivilegeGrantArtifact(t *testing.T) {
	db, _, ctx := integrationStore(t)
	var canCreateRole bool
	if err := db.QueryRowContext(ctx, `SELECT rolcreaterole OR rolsuper FROM pg_roles WHERE rolname=current_user`).Scan(&canCreateRole); err != nil {
		t.Fatal(err)
	}
	if !canCreateRole {
		t.Skip("database role cannot create disposable grant-test roles")
	}
	suffix := fmt.Sprintf("%d", time.Now().UnixNano())
	controlRole := "ziggy_control_test_" + suffix
	importRole := "ziggy_import_test_" + suffix
	for _, role := range []string{controlRole, importRole} {
		if _, err := db.ExecContext(ctx, `CREATE ROLE `+quoteSQLIdentifier(role)); err != nil {
			t.Fatal(err)
		}
		role := role
		t.Cleanup(func() {
			_, _ = db.ExecContext(context.Background(), `DROP OWNED BY `+quoteSQLIdentifier(role))
			_, _ = db.ExecContext(context.Background(), `DROP ROLE `+quoteSQLIdentifier(role))
		})
	}
	var databaseName string
	if err := db.QueryRowContext(ctx, `SELECT current_database()`).Scan(&databaseName); err != nil {
		t.Fatal(err)
	}
	contents, err := os.ReadFile(filepath.Join("..", "..", "deploy", "postgres", "least-privilege-grants.sql"))
	if err != nil {
		t.Fatal(err)
	}
	grants := strings.TrimPrefix(string(contents), "\\set ON_ERROR_STOP on\n")
	grants = strings.ReplaceAll(grants, `:"database_name"`, quoteSQLIdentifier(databaseName))
	grants = strings.ReplaceAll(grants, `:"control_role"`, quoteSQLIdentifier(controlRole))
	grants = strings.ReplaceAll(grants, `:"import_role"`, quoteSQLIdentifier(importRole))
	if _, err := db.ExecContext(ctx, grants); err != nil {
		t.Fatal(err)
	}

	assertPrivilege(t, db, ctx, `SELECT has_table_privilege($1, 'ziggy_tenant_users', 'SELECT')`, controlRole, true)
	assertPrivilege(t, db, ctx, `SELECT has_column_privilege($1, 'ziggy_tenant_users', 'expected_email', 'UPDATE')`, controlRole, true)
	assertPrivilege(t, db, ctx, `SELECT has_table_privilege($1, 'ziggy_tenant_users', 'DELETE')`, controlRole, false)
	assertPrivilege(t, db, ctx, `SELECT has_column_privilege($1, 'ziggy_tenant_lifecycle_events', 'event_type', 'INSERT')`, controlRole, true)
	assertPrivilege(t, db, ctx, `SELECT has_table_privilege($1, 'ziggy_tenant_lifecycle_events', 'UPDATE')`, controlRole, false)
	assertPrivilege(t, db, ctx, `SELECT has_table_privilege($1, 'ziggy_schema_identity', 'SELECT')`, controlRole, true)
	assertPrivilege(t, db, ctx, `SELECT has_table_privilege($1, 'ziggy_schema_identity', 'UPDATE')`, controlRole, false)

	assertPrivilege(t, db, ctx, `SELECT has_table_privilege($1, 'ziggy_tenant_users', 'SELECT')`, importRole, true)
	assertPrivilege(t, db, ctx, `SELECT has_column_privilege($1, 'ziggy_tenant_users', 'expected_email', 'INSERT')`, importRole, true)
	assertPrivilege(t, db, ctx, `SELECT has_column_privilege($1, 'ziggy_tenant_users', 'expected_email', 'UPDATE')`, importRole, false)
	assertPrivilege(t, db, ctx, `SELECT has_table_privilege($1, 'ziggy_tenant_runtime_allocations', 'UPDATE')`, importRole, false)
	assertPrivilege(t, db, ctx, `SELECT has_table_privilege($1, 'ziggy_tenant_lifecycle_events', 'SELECT')`, importRole, false)
	assertPrivilege(t, db, ctx, `SELECT has_column_privilege($1, 'ziggy_tenant_lifecycle_events', 'event_type', 'INSERT')`, importRole, true)
	assertPrivilege(t, db, ctx, `SELECT has_table_privilege($1, 'ziggy_schema_identity', 'UPDATE')`, importRole, false)
}

func legacyImportRegistry(t *testing.T, tenants []map[string]any, bindings []map[string]any) *tenant.Registry {
	t.Helper()
	directory := t.TempDir()
	manifestPath := filepath.Join(directory, "tenants.json")
	bindingsPath := filepath.Join(directory, "bindings.json")
	writeImportJSON(t, manifestPath, map[string]any{"version": 1, "tenants": tenants})
	writeImportJSON(t, bindingsPath, map[string]any{"version": 1, "bindings": bindings})
	registry, err := tenant.Load(manifestPath, bindingsPath)
	if err != nil {
		t.Fatal(err)
	}
	return registry
}

func legacyTenant(userID, workspaceID, email, upstreamURL, secret string, legacyDefault bool) map[string]any {
	return map[string]any{
		"user_id": userID, "workspace_id": workspaceID, "email": email,
		"upstream_url": upstreamURL, "upstream_bootstrap_secret": secret,
		"status": "active", "legacy_default": legacyDefault,
	}
}

func writeImportJSON(t *testing.T, path string, value any) {
	t.Helper()
	contents, err := json.Marshal(value)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, contents, 0o600); err != nil {
		t.Fatal(err)
	}
}

func pendingActivation(t *testing.T, db *sql.DB, store *Store, ctx context.Context, email, subject string) (string, RuntimeActivation) {
	t.Helper()
	userID, err := store.Invite(ctx, email)
	if err != nil {
		t.Fatal(err)
	}
	allocation, err := store.Resolve(ctx, identity.Principal{Subject: subject, Email: email})
	if err != nil {
		t.Fatal(err)
	}
	var runtimeID string
	var generation int64
	if err := db.QueryRowContext(ctx, `SELECT runtime_id, generation FROM ziggy_tenant_runtime_allocations WHERE user_id=$1`, userID).Scan(&runtimeID, &generation); err != nil {
		t.Fatal(err)
	}
	return userID, RuntimeActivation{UserID: userID, WorkspaceID: allocation.WorkspaceID, RuntimeID: runtimeID, ExpectedGeneration: generation}
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
	for _, filename := range []string{
		"001_tenant_lifecycle_foundation.sql",
		"002_runtime_allocation_isolation.sql",
		"003_terminal_deletion_receipt.sql",
		"004_schema_identity.sql",
	} {
		if err := applyMigrationFile(ctx, db, filename); err != nil {
			return err
		}
	}
	return nil
}

func quoteSQLIdentifier(value string) string {
	return `"` + strings.ReplaceAll(value, `"`, `""`) + `"`
}

func assertPrivilege(t *testing.T, db *sql.DB, ctx context.Context, query, role string, want bool) {
	t.Helper()
	var got bool
	if err := db.QueryRowContext(ctx, query, role).Scan(&got); err != nil {
		t.Fatal(err)
	}
	if got != want {
		t.Fatalf("%s for %s = %v, want %v", query, role, got, want)
	}
}

func applyMigrationFile(ctx context.Context, db *sql.DB, filename string) error {
	contents, err := os.ReadFile(filepath.Join("..", "..", "migrations", filename))
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
