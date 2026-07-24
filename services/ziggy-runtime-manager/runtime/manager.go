package runtime

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sync"

	"github.com/mihai-chiorean/nanobot/services/ziggy-runtime-manager/internal/atomicfile"
)

const workspaceStateVersion = 1

type workspaceState struct {
	Version           int    `json:"version"`
	WorkspaceID       string `json:"workspace_id"`
	Generation        uint64 `json:"generation"`
	RuntimeDeleted    bool   `json:"runtime_deleted"`
	DataDeletePending bool   `json:"tenant_data_delete_pending"`
	TenantDataDeleted bool   `json:"tenant_data_deleted"`
}

// Manager is the durable, single-process lifecycle authority in front of a
// concrete runtime driver. It retains generation tombstones after all Podman
// artifacts are gone and holds a workspace lock across every driver call.
type Manager struct {
	driver    Driver
	validator RequestValidator
	dir       string

	locksMu sync.Mutex
	locks   map[string]chan struct{}

	readState  func(string) (workspaceState, bool, error)
	writeState func(workspaceState) error
}

func NewManager(driver Driver, stateDir string) (*Manager, error) {
	if driver == nil {
		return nil, errors.New("runtime driver is required")
	}
	validator, ok := driver.(RequestValidator)
	if !ok {
		return nil, errors.New("runtime driver request validator is required")
	}
	if stateDir == "" {
		return nil, errors.New("runtime state directory is required")
	}
	if err := os.MkdirAll(stateDir, 0o700); err != nil {
		return nil, err
	}
	if err := os.Chmod(stateDir, 0o700); err != nil {
		return nil, err
	}
	info, err := os.Lstat(stateDir)
	if err != nil {
		return nil, err
	}
	if !info.IsDir() || info.Mode()&os.ModeSymlink != 0 || info.Mode().Perm() != 0o700 {
		return nil, errors.New("runtime state path must be a mode-0700 directory")
	}

	manager := &Manager{
		driver:    driver,
		validator: validator,
		dir:       stateDir,
		locks:     make(map[string]chan struct{}),
	}
	manager.readState = manager.readWorkspaceState
	manager.writeState = manager.writeWorkspaceState
	return manager, nil
}

func (m *Manager) EnsureRunning(ctx context.Context, request Request) (Status, error) {
	if err := m.validateRequest(request); err != nil {
		return Status{}, err
	}
	unlock, err := m.lockWorkspace(ctx, request.WorkspaceID)
	if err != nil {
		return Status{}, err
	}
	defer unlock()

	state, found, err := m.readState(request.WorkspaceID)
	if err != nil {
		return Status{}, err
	}
	if found {
		if state.DataDeletePending || state.TenantDataDeleted {
			return Status{}, ErrTenantDataDeleted
		}
		switch {
		case request.Generation < state.Generation:
			return Status{}, ErrStaleGeneration
		case request.Generation == state.Generation && state.RuntimeDeleted:
			return Status{}, ErrStaleGeneration
		case request.Generation > state.Generation && !state.RuntimeDeleted:
			return Status{}, ErrGenerationConflict
		case request.Generation == state.Generation:
			return m.driver.EnsureRunning(ctx, request)
		}
	}

	next := workspaceState{
		Version:     workspaceStateVersion,
		WorkspaceID: request.WorkspaceID,
		Generation:  request.Generation,
	}
	if err := m.writeState(next); err != nil {
		return Status{}, fmt.Errorf("persist runtime generation fence: %w", err)
	}
	return m.driver.EnsureRunning(ctx, request)
}

func (m *Manager) Health(ctx context.Context, request Request) (Status, error) {
	return m.currentStatusCall(ctx, request, m.driver.Health)
}

func (m *Manager) Drain(ctx context.Context, request Request) (Status, error) {
	return m.currentStatusCall(ctx, request, m.driver.Drain)
}

func (m *Manager) Stop(ctx context.Context, request Request) (Status, error) {
	return m.currentStatusCall(ctx, request, m.driver.Stop)
}

func (m *Manager) Delete(ctx context.Context, request Request) error {
	if err := m.validateRequest(request); err != nil {
		return err
	}
	unlock, err := m.lockWorkspace(ctx, request.WorkspaceID)
	if err != nil {
		return err
	}
	defer unlock()

	state, found, err := m.readState(request.WorkspaceID)
	if err != nil {
		return err
	}
	if err := compareGeneration(state, found, request); err != nil {
		return err
	}
	if state.RuntimeDeleted {
		return nil
	}
	if err := m.driver.Delete(ctx, request); err != nil {
		return err
	}
	state.RuntimeDeleted = true
	if err := m.writeState(state); err != nil {
		return fmt.Errorf("persist runtime deletion tombstone: %w", err)
	}
	return nil
}

func (m *Manager) DeleteTenantData(ctx context.Context, request Request) error {
	if err := m.validateRequest(request); err != nil {
		return err
	}
	unlock, err := m.lockWorkspace(ctx, request.WorkspaceID)
	if err != nil {
		return err
	}
	defer unlock()

	state, found, err := m.readState(request.WorkspaceID)
	if err != nil {
		return err
	}
	if err := compareGeneration(state, found, request); err != nil {
		return err
	}
	if !state.RuntimeDeleted {
		return errors.New("runtime generation must be deleted before tenant data deletion")
	}
	if state.TenantDataDeleted {
		return nil
	}
	if !state.DataDeletePending {
		state.DataDeletePending = true
		if err := m.writeState(state); err != nil {
			return fmt.Errorf("persist terminal tenant data deletion intent: %w", err)
		}
	}
	if err := m.driver.DeleteTenantData(ctx, request); err != nil {
		return err
	}
	state.DataDeletePending = false
	state.TenantDataDeleted = true
	if err := m.writeState(state); err != nil {
		return fmt.Errorf("persist tenant data deletion tombstone: %w", err)
	}
	return nil
}

func (m *Manager) currentStatusCall(
	ctx context.Context,
	request Request,
	call func(context.Context, Request) (Status, error),
) (Status, error) {
	if err := m.validateRequest(request); err != nil {
		return Status{}, err
	}
	unlock, err := m.lockWorkspace(ctx, request.WorkspaceID)
	if err != nil {
		return Status{}, err
	}
	defer unlock()

	state, found, err := m.readState(request.WorkspaceID)
	if err != nil {
		return Status{}, err
	}
	if err := compareGeneration(state, found, request); err != nil {
		return Status{}, err
	}
	if state.RuntimeDeleted {
		return Status{}, ErrNotFound
	}
	return call(ctx, request)
}

func validateManagerRequest(request Request) error {
	if !workspaceIDPattern.MatchString(request.WorkspaceID) {
		return ErrInvalidWorkspace
	}
	if request.Generation == 0 {
		return ErrInvalidGeneration
	}
	return nil
}

func (m *Manager) validateRequest(request Request) error {
	if err := validateManagerRequest(request); err != nil {
		return err
	}
	return m.validator.ValidateRequest(request)
}

func compareGeneration(state workspaceState, found bool, request Request) error {
	if !found {
		return ErrNotFound
	}
	if request.Generation < state.Generation {
		return ErrStaleGeneration
	}
	if request.Generation > state.Generation {
		return ErrGenerationConflict
	}
	return nil
}

func (m *Manager) lockWorkspace(ctx context.Context, workspaceID string) (func(), error) {
	m.locksMu.Lock()
	lock, ok := m.locks[workspaceID]
	if !ok {
		lock = make(chan struct{}, 1)
		m.locks[workspaceID] = lock
	}
	m.locksMu.Unlock()

	select {
	case lock <- struct{}{}:
		return func() { <-lock }, nil
	case <-ctx.Done():
		return nil, ctx.Err()
	}
}

func (m *Manager) statePath(workspaceID string) string {
	return filepath.Join(m.dir, workspaceID+".json")
}

func (m *Manager) readWorkspaceState(workspaceID string) (workspaceState, bool, error) {
	path := m.statePath(workspaceID)
	info, err := os.Lstat(path)
	if errors.Is(err, os.ErrNotExist) {
		return workspaceState{}, false, nil
	}
	if err != nil {
		return workspaceState{}, false, err
	}
	if !info.Mode().IsRegular() || info.Mode().Perm() != 0o600 {
		return workspaceState{}, false, errors.New("runtime state file must be regular and mode 0600")
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		return workspaceState{}, false, err
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	var state workspaceState
	if err := decoder.Decode(&state); err != nil {
		return workspaceState{}, false, errors.New("runtime state file is invalid")
	}
	var trailing any
	if err := decoder.Decode(&trailing); err != io.EOF {
		return workspaceState{}, false, errors.New("runtime state file has trailing content")
	}
	if state.Version != workspaceStateVersion ||
		state.WorkspaceID != workspaceID ||
		state.Generation == 0 ||
		((state.DataDeletePending || state.TenantDataDeleted) && !state.RuntimeDeleted) ||
		(state.DataDeletePending && state.TenantDataDeleted) {
		return workspaceState{}, false, errors.New("runtime state file failed validation")
	}
	return state, true, nil
}

func (m *Manager) writeWorkspaceState(state workspaceState) error {
	raw, err := json.Marshal(state)
	if err != nil {
		return err
	}
	raw = append(raw, '\n')
	return atomicfile.Write(m.statePath(state.WorkspaceID), raw, 0o600)
}
