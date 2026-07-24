package lifecycle

import (
	"context"
	"database/sql"
	"errors"
	"sort"
	"strings"
	"time"

	"github.com/jackc/pgx/v5/pgconn"
	"github.com/mihai-chiorean/nanobot/services/ziggy-control/internal/config"
	"github.com/mihai-chiorean/nanobot/services/ziggy-control/internal/tenant"
)

const (
	legacyImportAdvisoryLock = int64(0x5a49474759494d50)
	legacyImportMaxAttempts  = 4
)

// ImportResult contains counts only. It deliberately contains no tenant
// identities, runtime endpoints, or credentials so command output is safe.
type ImportResult struct {
	Tenants            int
	UsersCreated       int
	UsersExisting      int
	WorkspacesCreated  int
	WorkspacesExisting int
	RuntimesCreated    int
	RuntimesExisting   int
}

// ImportLegacyAllocations imports a snapshot produced by tenant.ImportAllocations.
// Existing rows are accepted only when they exactly match the legacy source.
// Dry runs execute the same checks and then roll the transaction back.
func (s *Store) ImportLegacyAllocations(ctx context.Context, allocations []tenant.Allocation, dryRun bool) (ImportResult, error) {
	if len(allocations) == 0 {
		return ImportResult{}, ErrImportConflict
	}
	ordered := append([]tenant.Allocation(nil), allocations...)
	for _, allocation := range ordered {
		if err := validateImportAllocation(allocation); err != nil {
			return ImportResult{}, err
		}
	}
	sort.Slice(ordered, func(i, j int) bool {
		if ordered[i].UserID != ordered[j].UserID {
			return ordered[i].UserID < ordered[j].UserID
		}
		if ordered[i].WorkspaceID != ordered[j].WorkspaceID {
			return ordered[i].WorkspaceID < ordered[j].WorkspaceID
		}
		return ordered[i].Email < ordered[j].Email
	})

	ctx, cancel := s.operationContext(ctx)
	defer cancel()
	for attempt := 0; attempt < legacyImportMaxAttempts; attempt++ {
		result, err := s.importLegacyAllocationsOnce(ctx, ordered, dryRun)
		if err == nil || !retryableImportError(err) {
			return result, err
		}
		if attempt == legacyImportMaxAttempts-1 {
			return ImportResult{}, err
		}
		timer := time.NewTimer(time.Duration(attempt+1) * 10 * time.Millisecond)
		select {
		case <-ctx.Done():
			timer.Stop()
			return ImportResult{}, ctx.Err()
		case <-timer.C:
		}
	}
	return ImportResult{}, errors.New("tenant import retry attempts exhausted")
}

func (s *Store) importLegacyAllocationsOnce(ctx context.Context, allocations []tenant.Allocation, dryRun bool) (ImportResult, error) {
	tx, err := s.db.BeginTx(ctx, &sql.TxOptions{Isolation: sql.LevelSerializable})
	if err != nil {
		return ImportResult{}, err
	}
	defer tx.Rollback()
	if _, err := tx.ExecContext(ctx, `SELECT pg_advisory_xact_lock($1)`, legacyImportAdvisoryLock); err != nil {
		return ImportResult{}, err
	}

	result := ImportResult{Tenants: len(allocations)}
	for _, allocation := range allocations {
		if err := s.importAllocation(ctx, tx, allocation, &result); err != nil {
			return ImportResult{}, err
		}
	}
	if dryRun {
		return result, nil
	}
	if err := tx.Commit(); err != nil {
		return ImportResult{}, importError(err)
	}
	return result, nil
}

func (s *Store) importAllocation(ctx context.Context, tx *sql.Tx, allocation tenant.Allocation, result *ImportResult) error {
	var existingEmail, existingStatus string
	var existingSubject sql.NullString
	err := tx.QueryRowContext(ctx, `SELECT expected_email, lifecycle_status, clerk_subject
FROM ziggy_tenant_users WHERE user_id=$1 FOR UPDATE`, allocation.UserID).Scan(&existingEmail, &existingStatus, &existingSubject)
	switch {
	case errors.Is(err, sql.ErrNoRows):
		if err := assertUnclaimedUserFields(ctx, tx, allocation); err != nil {
			return err
		}
		if _, err := tx.ExecContext(ctx, `INSERT INTO ziggy_tenant_users
 (user_id, expected_email, clerk_subject, lifecycle_status, created_at, updated_at, activated_at, disabled_at)
VALUES ($1,$2,$3,$4,now(),now(),CASE WHEN $4='active' THEN now() END,CASE WHEN $4='disabled' THEN now() END)`,
			allocation.UserID, allocation.Email, nullableString(allocation.ClerkSubject), allocation.Status); err != nil {
			return importError(err)
		}
		if err := appendEvent(ctx, tx, allocation.UserID, "", "legacy_manifest_user_imported", "migration", 0, s.now()); err != nil {
			return err
		}
		result.UsersCreated++
	case err != nil:
		return err
	case existingEmail != allocation.Email || existingStatus != allocation.Status || !sameNullableString(existingSubject, allocation.ClerkSubject):
		return ErrImportConflict
	default:
		result.UsersExisting++
	}

	var workspaceUserID string
	err = tx.QueryRowContext(ctx, `SELECT user_id FROM ziggy_tenant_workspaces WHERE workspace_id=$1 FOR UPDATE`, allocation.WorkspaceID).Scan(&workspaceUserID)
	switch {
	case errors.Is(err, sql.ErrNoRows):
		var existingWorkspaceID string
		err = tx.QueryRowContext(ctx, `SELECT workspace_id FROM ziggy_tenant_workspaces WHERE user_id=$1 FOR UPDATE`, allocation.UserID).Scan(&existingWorkspaceID)
		if err == nil {
			return ErrImportConflict
		}
		if !errors.Is(err, sql.ErrNoRows) {
			return err
		}
		if _, err := tx.ExecContext(ctx, `INSERT INTO ziggy_tenant_workspaces (workspace_id, user_id, created_at) VALUES ($1,$2,now())`, allocation.WorkspaceID, allocation.UserID); err != nil {
			return importError(err)
		}
		if err := appendEvent(ctx, tx, allocation.UserID, allocation.WorkspaceID, "legacy_manifest_workspace_imported", "migration", 0, s.now()); err != nil {
			return err
		}
		result.WorkspacesCreated++
	case err != nil:
		return err
	case workspaceUserID != allocation.UserID:
		return ErrImportConflict
	default:
		result.WorkspacesExisting++
	}

	expectedRuntimeID := legacyRuntimeID(allocation.WorkspaceID)
	expectedState := runtimeStateForStatus(allocation.Status)
	var runtimeUserID, runtimeID, state, upstreamURL, secret string
	var generation int64
	err = tx.QueryRowContext(ctx, `SELECT user_id, runtime_id, generation, allocation_state, upstream_url, upstream_bootstrap_secret
FROM ziggy_tenant_runtime_allocations WHERE workspace_id=$1 FOR UPDATE`, allocation.WorkspaceID).Scan(&runtimeUserID, &runtimeID, &generation, &state, &upstreamURL, &secret)
	switch {
	case errors.Is(err, sql.ErrNoRows):
		var existingRuntimeWorkspace string
		err = tx.QueryRowContext(ctx, `SELECT workspace_id FROM ziggy_tenant_runtime_allocations WHERE user_id=$1 FOR UPDATE`, allocation.UserID).Scan(&existingRuntimeWorkspace)
		if err == nil {
			return ErrImportConflict
		}
		if !errors.Is(err, sql.ErrNoRows) {
			return err
		}
		if _, err := tx.ExecContext(ctx, `INSERT INTO ziggy_tenant_runtime_allocations
 (workspace_id, user_id, runtime_id, generation, allocation_state, upstream_url, upstream_bootstrap_secret, created_at, updated_at)
VALUES ($1,$2,$3,1,$4,$5,$6,now(),now())`, allocation.WorkspaceID, allocation.UserID, expectedRuntimeID, expectedState, allocation.UpstreamURL, allocation.UpstreamBootstrapSecret); err != nil {
			return importError(err)
		}
		if err := appendEvent(ctx, tx, allocation.UserID, allocation.WorkspaceID, "legacy_manifest_runtime_imported", "migration", 1, s.now()); err != nil {
			return err
		}
		result.RuntimesCreated++
	case err != nil:
		return err
	case runtimeUserID != allocation.UserID || runtimeID != expectedRuntimeID || generation != 1 || state != expectedState || upstreamURL != allocation.UpstreamURL || secret != allocation.UpstreamBootstrapSecret:
		return ErrImportConflict
	default:
		result.RuntimesExisting++
	}
	return nil
}

func assertUnclaimedUserFields(ctx context.Context, tx *sql.Tx, allocation tenant.Allocation) error {
	var existingUserID string
	err := tx.QueryRowContext(ctx, `SELECT user_id FROM ziggy_tenant_users WHERE expected_email=$1 FOR UPDATE`, allocation.Email).Scan(&existingUserID)
	if err == nil {
		return ErrImportConflict
	}
	if !errors.Is(err, sql.ErrNoRows) {
		return err
	}
	if allocation.ClerkSubject == "" {
		return nil
	}
	err = tx.QueryRowContext(ctx, `SELECT user_id FROM ziggy_tenant_users WHERE clerk_subject=$1 FOR UPDATE`, allocation.ClerkSubject).Scan(&existingUserID)
	if err == nil {
		return ErrImportConflict
	}
	if !errors.Is(err, sql.ErrNoRows) {
		return err
	}
	return nil
}

func validateImportAllocation(allocation tenant.Allocation) error {
	if strings.TrimSpace(allocation.UserID) == "" || strings.TrimSpace(allocation.WorkspaceID) == "" || strings.TrimSpace(allocation.Email) == "" || len(allocation.UpstreamBootstrapSecret) < 32 || (allocation.Status != "active" && allocation.Status != "disabled") {
		return ErrImportConflict
	}
	if _, err := config.ParsePrivateUpstream(allocation.UpstreamURL); err != nil {
		return ErrImportConflict
	}
	return nil
}

func runtimeStateForStatus(status string) string {
	if status == "active" {
		return "active"
	}
	return "disabled"
}

func legacyRuntimeID(workspaceID string) string { return "rt_" + workspaceID }

func nullableString(value string) any {
	if value == "" {
		return nil
	}
	return value
}

func sameNullableString(existing sql.NullString, expected string) bool {
	return existing.Valid == (expected != "") && (!existing.Valid || existing.String == expected)
}

func importError(err error) error {
	var pgErr *pgconn.PgError
	if errors.As(err, &pgErr) && pgErr.Code == "23505" {
		return ErrImportConflict
	}
	return err
}

func retryableImportError(err error) bool {
	var pgErr *pgconn.PgError
	return errors.As(err, &pgErr) && (pgErr.Code == "40001" || pgErr.Code == "40P01")
}
