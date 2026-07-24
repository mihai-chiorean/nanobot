package lifecycle

import (
	"context"
	"crypto/rand"
	"database/sql"
	"encoding/hex"
	"errors"
	"fmt"
	"net/mail"
	"strings"
	"time"

	"github.com/mihai-chiorean/nanobot/services/ziggy-control/internal/identity"
	"github.com/mihai-chiorean/nanobot/services/ziggy-control/internal/tenant"
)

const defaultQueryTimeout = 2 * time.Second

var (
	ErrNotAuthorized      = errors.New("tenant is not authorized")
	ErrRuntimeUnavailable = errors.New("tenant runtime is unavailable")
)

type RuntimeAllocation struct {
	UserID                  string
	WorkspaceID             string
	RuntimeID               string
	Generation              int64
	State                   string
	UpstreamURL             string
	UpstreamBootstrapSecret string
}

// RuntimeProvisioner is deliberately narrow. A later runtime manager owns the
// implementation; this package only persists and returns allocation intent.
type RuntimeProvisioner interface {
	EnsureRuntime(context.Context, RuntimeAllocation) error
}

type Store struct {
	db           *sql.DB
	queryTimeout time.Duration
	now          func() time.Time
	newID        func(string) (string, error)
}

func NewStore(db *sql.DB) (*Store, error) {
	if db == nil {
		return nil, errors.New("tenant database handle is required")
	}
	return &Store{db: db, queryTimeout: defaultQueryTimeout, now: time.Now, newID: randomID}, nil
}

func (s *Store) Ready(ctx context.Context) error {
	ctx, cancel := s.operationContext(ctx)
	defer cancel()
	if err := s.db.PingContext(ctx); err != nil {
		return err
	}
	var present bool
	err := s.db.QueryRowContext(ctx, `SELECT
  to_regclass('public.ziggy_tenant_users') IS NOT NULL
  AND to_regclass('public.ziggy_tenant_workspaces') IS NOT NULL
  AND to_regclass('public.ziggy_tenant_runtime_allocations') IS NOT NULL
  AND to_regclass('public.ziggy_tenant_lifecycle_events') IS NOT NULL`).Scan(&present)
	if err != nil {
		return err
	}
	if !present {
		return errors.New("tenant lifecycle migrations are incomplete")
	}
	return nil
}

// Resolve is the first authorized bootstrap transition. It binds a verified
// subject to the pre-admitted email, activates the user, and creates exactly
// one personal workspace plus pending runtime allocation.
func (s *Store) Resolve(ctx context.Context, principal identity.Principal) (tenant.Allocation, error) {
	email, subject, err := canonicalPrincipal(principal)
	if err != nil {
		return tenant.Allocation{}, ErrNotAuthorized
	}
	ctx, cancel := s.operationContext(ctx)
	defer cancel()
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return tenant.Allocation{}, err
	}
	defer tx.Rollback()

	var userID, status string
	var bound sql.NullString
	err = tx.QueryRowContext(ctx, `SELECT user_id, lifecycle_status, clerk_subject
FROM ziggy_tenant_users WHERE expected_email=$1 FOR UPDATE`, email).Scan(&userID, &status, &bound)
	if errors.Is(err, sql.ErrNoRows) {
		return tenant.Allocation{}, ErrNotAuthorized
	}
	if err != nil || !bootstrapAllowed(status) || (bound.Valid && bound.String != subject) {
		return tenant.Allocation{}, authorizationError(err)
	}

	var subjectOwner string
	err = tx.QueryRowContext(ctx, `SELECT user_id FROM ziggy_tenant_users WHERE clerk_subject=$1 FOR UPDATE`, subject).Scan(&subjectOwner)
	if err == nil && subjectOwner != userID {
		return tenant.Allocation{}, ErrNotAuthorized
	}
	if err != nil && !errors.Is(err, sql.ErrNoRows) {
		return tenant.Allocation{}, err
	}
	if !bound.Valid {
		if _, err := tx.ExecContext(ctx, `UPDATE ziggy_tenant_users SET clerk_subject=$2, updated_at=$3 WHERE user_id=$1 AND clerk_subject IS NULL`, userID, subject, s.now().UTC()); err != nil {
			return tenant.Allocation{}, authorizationError(err)
		}
		if err := appendEvent(ctx, tx, userID, "", "subject_bound", "bootstrap", 0, s.now()); err != nil {
			return tenant.Allocation{}, err
		}
	}

	workspaceID, err := s.workspaceForUpdate(ctx, tx, userID)
	if err != nil {
		return tenant.Allocation{}, err
	}
	runtime, err := s.runtimeForUpdate(ctx, tx, userID, workspaceID)
	if err != nil {
		return tenant.Allocation{}, err
	}
	if status == "invited" {
		if _, err := tx.ExecContext(ctx, `UPDATE ziggy_tenant_users SET lifecycle_status='active', activated_at=$2, updated_at=$2 WHERE user_id=$1 AND lifecycle_status='invited'`, userID, s.now().UTC()); err != nil {
			return tenant.Allocation{}, err
		}
		if err := appendEvent(ctx, tx, userID, workspaceID, "user_activated", "bootstrap", runtime.Generation, s.now()); err != nil {
			return tenant.Allocation{}, err
		}
	}
	if err := tx.Commit(); err != nil {
		return tenant.Allocation{}, err
	}
	return allocation(userID, workspaceID, runtime), nil
}

func (s *Store) ResolveActive(ctx context.Context, userID, workspaceID string) (tenant.Allocation, error) {
	ctx, cancel := s.operationContext(ctx)
	defer cancel()
	var status string
	var runtime RuntimeAllocation
	err := s.db.QueryRowContext(ctx, `SELECT u.lifecycle_status, r.runtime_id, r.generation, r.allocation_state, r.upstream_url, r.upstream_bootstrap_secret
FROM ziggy_tenant_users u
JOIN ziggy_tenant_workspaces w ON w.user_id=u.user_id
JOIN ziggy_tenant_runtime_allocations r ON r.workspace_id=w.workspace_id
WHERE u.user_id=$1 AND w.workspace_id=$2`, userID, workspaceID).Scan(&status, &runtime.RuntimeID, &runtime.Generation, &runtime.State, &runtime.UpstreamURL, &runtime.UpstreamBootstrapSecret)
	if errors.Is(err, sql.ErrNoRows) || status != "active" {
		return tenant.Allocation{}, ErrNotAuthorized
	}
	if err != nil {
		return tenant.Allocation{}, err
	}
	runtime.UserID, runtime.WorkspaceID = userID, workspaceID
	if runtime.State != "active" || strings.TrimSpace(runtime.UpstreamURL) == "" || len(runtime.UpstreamBootstrapSecret) < 32 {
		return tenant.Allocation{}, ErrRuntimeUnavailable
	}
	return allocation(userID, workspaceID, runtime), nil
}

// Invite is idempotent for an existing unbound admission with the same email.
// No network API calls it; an operator lifecycle tool can use it later.
func (s *Store) Invite(ctx context.Context, expectedEmail string) (string, error) {
	email, err := canonicalEmail(expectedEmail)
	if err != nil {
		return "", err
	}
	ctx, cancel := s.operationContext(ctx)
	defer cancel()
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return "", err
	}
	defer tx.Rollback()
	var existingID string
	err = tx.QueryRowContext(ctx, `SELECT user_id FROM ziggy_tenant_users WHERE expected_email=$1 FOR UPDATE`, email).Scan(&existingID)
	if err == nil {
		if err := tx.Commit(); err != nil {
			return "", err
		}
		return existingID, nil
	}
	if !errors.Is(err, sql.ErrNoRows) {
		return "", err
	}
	userID, err := s.newID("usr")
	if err != nil {
		return "", err
	}
	if _, err := tx.ExecContext(ctx, `INSERT INTO ziggy_tenant_users (user_id, expected_email, lifecycle_status, created_at, updated_at) VALUES ($1,$2,'invited',$3,$3)`, userID, email, s.now().UTC()); err != nil {
		return "", err
	}
	if err := appendEvent(ctx, tx, userID, "", "user_invited", "operator", 0, s.now()); err != nil {
		return "", err
	}
	if err := tx.Commit(); err != nil {
		return "", err
	}
	return userID, nil
}

func (s *Store) Disable(ctx context.Context, userID string) error {
	ctx, cancel := s.operationContext(ctx)
	defer cancel()
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	err = tx.QueryRowContext(ctx, `SELECT user_id FROM ziggy_tenant_users WHERE user_id=$1 FOR UPDATE`, userID).Scan(&userID)
	if errors.Is(err, sql.ErrNoRows) {
		return ErrNotAuthorized
	}
	if err != nil {
		return err
	}
	result, err := tx.ExecContext(ctx, `UPDATE ziggy_tenant_users SET lifecycle_status='disabled', disabled_at=$2, updated_at=$2 WHERE user_id=$1 AND lifecycle_status <> 'deleted'`, userID, s.now().UTC())
	if err != nil {
		return err
	}
	if changed, _ := result.RowsAffected(); changed == 0 {
		return ErrNotAuthorized
	}
	var workspace sql.NullString
	err = tx.QueryRowContext(ctx, `SELECT workspace_id FROM ziggy_tenant_workspaces WHERE user_id=$1 FOR UPDATE`, userID).Scan(&workspace)
	if err != nil && !errors.Is(err, sql.ErrNoRows) {
		return err
	}
	if _, err := tx.ExecContext(ctx, `UPDATE ziggy_tenant_runtime_allocations SET allocation_state='disabled', updated_at=$2 WHERE user_id=$1`, userID, s.now().UTC()); err != nil {
		return err
	}
	if err := appendEvent(ctx, tx, userID, workspace.String, "user_disabled", "operator", 0, s.now()); err != nil {
		return err
	}
	return tx.Commit()
}

func (s *Store) workspaceForUpdate(ctx context.Context, tx *sql.Tx, userID string) (string, error) {
	var workspaceID string
	err := tx.QueryRowContext(ctx, `SELECT workspace_id FROM ziggy_tenant_workspaces WHERE user_id=$1 FOR UPDATE`, userID).Scan(&workspaceID)
	if err == nil {
		return workspaceID, nil
	}
	if !errors.Is(err, sql.ErrNoRows) {
		return "", err
	}
	workspaceID, err = s.newID("ws")
	if err != nil {
		return "", err
	}
	if _, err := tx.ExecContext(ctx, `INSERT INTO ziggy_tenant_workspaces (workspace_id, user_id, created_at) VALUES ($1,$2,$3)`, workspaceID, userID, s.now().UTC()); err != nil {
		return "", err
	}
	if err := appendEvent(ctx, tx, userID, workspaceID, "workspace_created", "bootstrap", 0, s.now()); err != nil {
		return "", err
	}
	return workspaceID, nil
}

func (s *Store) runtimeForUpdate(ctx context.Context, tx *sql.Tx, userID, workspaceID string) (RuntimeAllocation, error) {
	var runtime RuntimeAllocation
	err := tx.QueryRowContext(ctx, `SELECT runtime_id, generation, allocation_state, upstream_url, upstream_bootstrap_secret FROM ziggy_tenant_runtime_allocations WHERE workspace_id=$1 FOR UPDATE`, workspaceID).Scan(&runtime.RuntimeID, &runtime.Generation, &runtime.State, &runtime.UpstreamURL, &runtime.UpstreamBootstrapSecret)
	if err == nil {
		runtime.UserID, runtime.WorkspaceID = userID, workspaceID
		return runtime, nil
	}
	if !errors.Is(err, sql.ErrNoRows) {
		return RuntimeAllocation{}, err
	}
	runtimeID, err := s.newID("rt")
	if err != nil {
		return RuntimeAllocation{}, err
	}
	runtime = RuntimeAllocation{UserID: userID, WorkspaceID: workspaceID, RuntimeID: runtimeID, Generation: 1, State: "pending"}
	if _, err := tx.ExecContext(ctx, `INSERT INTO ziggy_tenant_runtime_allocations (workspace_id, user_id, runtime_id, generation, allocation_state, created_at, updated_at) VALUES ($1,$2,$3,1,'pending',$4,$4)`, workspaceID, userID, runtimeID, s.now().UTC()); err != nil {
		return RuntimeAllocation{}, err
	}
	if err := appendEvent(ctx, tx, userID, workspaceID, "runtime_allocation_created", "bootstrap", runtime.Generation, s.now()); err != nil {
		return RuntimeAllocation{}, err
	}
	return runtime, nil
}

func allocation(userID, workspaceID string, runtime RuntimeAllocation) tenant.Allocation {
	return tenant.Allocation{UserID: userID, WorkspaceID: workspaceID, UpstreamURL: runtime.UpstreamURL, UpstreamBootstrapSecret: runtime.UpstreamBootstrapSecret, Status: "active"}
}

func appendEvent(ctx context.Context, tx *sql.Tx, userID, workspaceID, eventType, actor string, generation int64, now time.Time) error {
	var workspace any
	if workspaceID != "" {
		workspace = workspaceID
	}
	var runtimeGeneration any
	if generation > 0 {
		runtimeGeneration = generation
	}
	_, err := tx.ExecContext(ctx, `INSERT INTO ziggy_tenant_lifecycle_events (user_id, workspace_id, event_type, actor_kind, runtime_generation, occurred_at) VALUES ($1,$2,$3,$4,$5,$6)`, userID, workspace, eventType, actor, runtimeGeneration, now.UTC())
	return err
}

func (s *Store) operationContext(ctx context.Context) (context.Context, context.CancelFunc) {
	if _, ok := ctx.Deadline(); ok {
		return context.WithCancel(ctx)
	}
	return context.WithTimeout(ctx, s.queryTimeout)
}

func canonicalPrincipal(principal identity.Principal) (string, string, error) {
	email, err := canonicalEmail(principal.Email)
	if err != nil {
		return "", "", err
	}
	subject := strings.TrimSpace(principal.Subject)
	if subject == "" || len(subject) > 255 {
		return "", "", errors.New("invalid Clerk subject")
	}
	return email, subject, nil
}

func canonicalEmail(value string) (string, error) {
	value = strings.ToLower(strings.TrimSpace(value))
	parsed, err := mail.ParseAddress(value)
	if err != nil || parsed.Address != value || len(value) > 320 {
		return "", errors.New("invalid email")
	}
	return value, nil
}

func bootstrapAllowed(status string) bool { return status == "invited" || status == "active" }

func authorizationError(err error) error {
	if err == nil || errors.Is(err, sql.ErrNoRows) {
		return ErrNotAuthorized
	}
	return err
}

func randomID(prefix string) (string, error) {
	bytes := make([]byte, 16)
	if _, err := rand.Read(bytes); err != nil {
		return "", fmt.Errorf("generate %s identifier: %w", prefix, err)
	}
	return prefix + "_" + hex.EncodeToString(bytes), nil
}

var _ tenant.Resolver = (*Store)(nil)
