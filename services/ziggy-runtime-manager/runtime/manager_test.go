package runtime

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"sync"
	"testing"
	"time"
)

func TestManagerPersistsGenerationTombstoneAcrossRestart(t *testing.T) {
	stateDir := t.TempDir()
	firstDriver := &recordingDriver{}
	first := newTestManager(t, firstDriver, stateDir)
	generationSeven := Request{WorkspaceID: "tenant-alpha", Generation: 7}
	if _, err := first.EnsureRunning(context.Background(), generationSeven); err != nil {
		t.Fatal(err)
	}
	if err := first.Delete(context.Background(), generationSeven); err != nil {
		t.Fatal(err)
	}

	secondDriver := &recordingDriver{}
	second := newTestManager(t, secondDriver, stateDir)
	for _, call := range []struct {
		name string
		run  func() error
	}{
		{"ensure", func() error {
			_, err := second.EnsureRunning(context.Background(), generationSeven)
			return err
		}},
		{"delete", func() error {
			return second.Delete(context.Background(), Request{WorkspaceID: "tenant-alpha", Generation: 6})
		}},
		{"delete tenant data", func() error {
			return second.DeleteTenantData(context.Background(), Request{WorkspaceID: "tenant-alpha", Generation: 6})
		}},
	} {
		t.Run(call.name, func(t *testing.T) {
			if err := call.run(); !errors.Is(err, ErrStaleGeneration) {
				t.Fatalf("error=%v want stale generation", err)
			}
		})
	}
	if secondDriver.totalCalls() != 0 {
		t.Fatalf("stale request reached runtime driver: %+v", secondDriver)
	}
}

func TestManagerSerializesConcurrentGenerations(t *testing.T) {
	driver := &recordingDriver{
		ensureStarted: make(chan struct{}),
		ensureRelease: make(chan struct{}),
	}
	manager := newTestManager(t, driver, t.TempDir())
	firstErr := make(chan error, 1)
	go func() {
		_, err := manager.EnsureRunning(context.Background(), Request{WorkspaceID: "tenant-alpha", Generation: 7})
		firstErr <- err
	}()
	<-driver.ensureStarted

	secondErr := make(chan error, 1)
	go func() {
		_, err := manager.EnsureRunning(context.Background(), Request{WorkspaceID: "tenant-alpha", Generation: 8})
		secondErr <- err
	}()
	assertNoResult(t, secondErr)
	close(driver.ensureRelease)
	if err := <-firstErr; err != nil {
		t.Fatal(err)
	}
	if err := <-secondErr; !errors.Is(err, ErrGenerationConflict) {
		t.Fatalf("error=%v want generation conflict", err)
	}
	if driver.maxActiveCalls() != 1 || driver.ensureCallCount() != 1 {
		t.Fatalf("concurrent generations reached driver: %+v", driver)
	}
}

func TestManagerSerializesEnsureDeleteOverlap(t *testing.T) {
	driver := &recordingDriver{
		ensureStarted: make(chan struct{}),
		ensureRelease: make(chan struct{}),
	}
	manager := newTestManager(t, driver, t.TempDir())
	request := Request{WorkspaceID: "tenant-alpha", Generation: 7}
	ensureErr := make(chan error, 1)
	go func() {
		_, err := manager.EnsureRunning(context.Background(), request)
		ensureErr <- err
	}()
	<-driver.ensureStarted

	deleteErr := make(chan error, 1)
	go func() { deleteErr <- manager.Delete(context.Background(), request) }()
	assertNoResult(t, deleteErr)
	if driver.deleteCallCount() != 0 {
		t.Fatal("delete overlapped ensure side effects")
	}
	close(driver.ensureRelease)
	if err := <-ensureErr; err != nil {
		t.Fatal(err)
	}
	if err := <-deleteErr; err != nil {
		t.Fatal(err)
	}
	if driver.maxActiveCalls() != 1 || driver.deleteCallCount() != 1 {
		t.Fatalf("lifecycle side effects overlapped: %+v", driver)
	}
}

func TestManagerRejectsStaleDestructiveReplay(t *testing.T) {
	driver := &recordingDriver{}
	manager := newTestManager(t, driver, t.TempDir())
	generationSeven := Request{WorkspaceID: "tenant-alpha", Generation: 7}
	generationEight := Request{WorkspaceID: "tenant-alpha", Generation: 8}
	if _, err := manager.EnsureRunning(context.Background(), generationSeven); err != nil {
		t.Fatal(err)
	}
	if err := manager.Delete(context.Background(), generationSeven); err != nil {
		t.Fatal(err)
	}
	if _, err := manager.EnsureRunning(context.Background(), generationEight); err != nil {
		t.Fatal(err)
	}

	for _, call := range []struct {
		name string
		run  func() error
	}{
		{"delete", func() error { return manager.Delete(context.Background(), generationSeven) }},
		{"delete tenant data", func() error {
			return manager.DeleteTenantData(context.Background(), generationSeven)
		}},
	} {
		t.Run(call.name, func(t *testing.T) {
			if err := call.run(); !errors.Is(err, ErrStaleGeneration) {
				t.Fatalf("error=%v want stale generation", err)
			}
		})
	}
	if driver.deleteCallCount() != 1 || driver.deleteTenantDataCallCount() != 0 {
		t.Fatalf("stale destructive replay reached driver: %+v", driver)
	}
}

func TestManagerRejectsUnknownWorkspaceBeforeStateOrLockAllocation(t *testing.T) {
	stateDir := t.TempDir()
	driver := &recordingDriver{validateErr: ErrUnknownWorkspace}
	manager := newTestManager(t, driver, stateDir)
	request := Request{WorkspaceID: "tenant-unknown", Generation: 7}
	if _, err := manager.EnsureRunning(context.Background(), request); !errors.Is(err, ErrUnknownWorkspace) {
		t.Fatalf("error=%v want unknown workspace", err)
	}
	entries, err := os.ReadDir(stateDir)
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != 0 {
		t.Fatalf("unknown workspace created durable state: %+v", entries)
	}
	manager.locksMu.Lock()
	lockCount := len(manager.locks)
	manager.locksMu.Unlock()
	if lockCount != 0 {
		t.Fatalf("unknown workspace grew keyed lock map to %d entries", lockCount)
	}
	if driver.totalCalls() != 0 {
		t.Fatal("unknown workspace reached lifecycle side effects")
	}
}

func TestManagerTenantDataDeletionIsTerminal(t *testing.T) {
	driver := &recordingDriver{}
	manager := newTestManager(t, driver, t.TempDir())
	generationSeven := Request{WorkspaceID: "tenant-alpha", Generation: 7}
	if _, err := manager.EnsureRunning(context.Background(), generationSeven); err != nil {
		t.Fatal(err)
	}
	if err := manager.Delete(context.Background(), generationSeven); err != nil {
		t.Fatal(err)
	}
	if err := manager.DeleteTenantData(context.Background(), generationSeven); err != nil {
		t.Fatal(err)
	}
	if _, err := manager.EnsureRunning(context.Background(), Request{WorkspaceID: "tenant-alpha", Generation: 8}); !errors.Is(err, ErrTenantDataDeleted) {
		t.Fatalf("error=%v want terminal tenant data deletion", err)
	}
	if driver.ensureCallCount() != 1 {
		t.Fatal("terminal tenant was resurrected through concrete driver")
	}
	state, found, err := manager.readState("tenant-alpha")
	if err != nil || !found {
		t.Fatalf("state found=%t err=%v", found, err)
	}
	if state.Generation != 7 || !state.RuntimeDeleted || !state.TenantDataDeleted {
		t.Fatalf("terminal tombstone was overwritten: %+v", state)
	}
}

func TestManagerTerminalIntentSurvivesDataDeletionCompletionWriteFailure(t *testing.T) {
	driver := &recordingDriver{}
	manager := newTestManager(t, driver, t.TempDir())
	generationSeven := Request{WorkspaceID: "tenant-alpha", Generation: 7}
	if _, err := manager.EnsureRunning(context.Background(), generationSeven); err != nil {
		t.Fatal(err)
	}
	if err := manager.Delete(context.Background(), generationSeven); err != nil {
		t.Fatal(err)
	}
	durableWrite := manager.writeState
	manager.writeState = func(state workspaceState) error {
		if state.TenantDataDeleted {
			return errors.New("disk unavailable after volume deletion")
		}
		return durableWrite(state)
	}
	if err := manager.DeleteTenantData(context.Background(), generationSeven); err == nil {
		t.Fatal("tenant data deletion succeeded without a completion tombstone")
	}
	state, found, err := manager.readState("tenant-alpha")
	if err != nil || !found {
		t.Fatalf("state found=%t err=%v", found, err)
	}
	if !state.DataDeletePending || state.TenantDataDeleted {
		t.Fatalf("terminal deletion intent was not retained: %+v", state)
	}
	if _, err := manager.EnsureRunning(context.Background(), Request{WorkspaceID: "tenant-alpha", Generation: 8}); !errors.Is(err, ErrTenantDataDeleted) {
		t.Fatalf("error=%v want terminal tenant data deletion", err)
	}
	if driver.ensureCallCount() != 1 {
		t.Fatal("pending terminal deletion allowed tenant resurrection")
	}
}

func TestManagerStateWriteFailureFailsClosed(t *testing.T) {
	driver := &recordingDriver{}
	manager := newTestManager(t, driver, t.TempDir())
	manager.writeState = func(workspaceState) error { return errors.New("disk unavailable") }
	if _, err := manager.EnsureRunning(context.Background(), Request{WorkspaceID: "tenant-alpha", Generation: 7}); err == nil {
		t.Fatal("ensure succeeded without a durable generation fence")
	}
	if driver.totalCalls() != 0 {
		t.Fatal("state write failure reached runtime driver")
	}
}

func TestManagerRejectsCorruptStateWithoutSideEffects(t *testing.T) {
	stateDir := t.TempDir()
	path := filepath.Join(stateDir, "tenant-alpha.json")
	if err := os.WriteFile(path, []byte(`{"version":1,"workspace_id":"tenant-alpha","generation":7} trailing`), 0o600); err != nil {
		t.Fatal(err)
	}
	driver := &recordingDriver{}
	manager := newTestManager(t, driver, stateDir)
	if _, err := manager.Health(context.Background(), Request{WorkspaceID: "tenant-alpha", Generation: 7}); err == nil {
		t.Fatal("corrupt state was accepted")
	}
	if driver.totalCalls() != 0 {
		t.Fatal("corrupt state reached runtime driver")
	}
}

func newTestManager(t *testing.T, driver Driver, stateDir string) *Manager {
	t.Helper()
	manager, err := NewManager(driver, stateDir)
	if err != nil {
		t.Fatal(err)
	}
	return manager
}

func assertNoResult(t *testing.T, result <-chan error) {
	t.Helper()
	select {
	case err := <-result:
		t.Fatalf("operation completed before workspace lock released: %v", err)
	case <-time.After(30 * time.Millisecond):
	}
}

type recordingDriver struct {
	mu sync.Mutex

	validateErr       error
	active            int
	maxActive         int
	ensureCalls       int
	deleteCalls       int
	deleteTenantCalls int
	ensureStarted     chan struct{}
	ensureRelease     chan struct{}
}

func (d *recordingDriver) ValidateRequest(Request) error {
	return d.validateErr
}

func (d *recordingDriver) begin() {
	d.mu.Lock()
	d.active++
	if d.active > d.maxActive {
		d.maxActive = d.active
	}
	d.mu.Unlock()
}

func (d *recordingDriver) end() {
	d.mu.Lock()
	d.active--
	d.mu.Unlock()
}

func (d *recordingDriver) EnsureRunning(_ context.Context, request Request) (Status, error) {
	d.begin()
	defer d.end()
	d.mu.Lock()
	d.ensureCalls++
	d.mu.Unlock()
	if d.ensureStarted != nil {
		select {
		case <-d.ensureStarted:
		default:
			close(d.ensureStarted)
		}
	}
	if d.ensureRelease != nil {
		<-d.ensureRelease
	}
	return Status{WorkspaceID: request.WorkspaceID, Generation: request.Generation, State: StateRunning}, nil
}

func (d *recordingDriver) Health(_ context.Context, request Request) (Status, error) {
	d.begin()
	defer d.end()
	return Status{WorkspaceID: request.WorkspaceID, Generation: request.Generation, State: StateRunning}, nil
}

func (d *recordingDriver) Drain(_ context.Context, request Request) (Status, error) {
	d.begin()
	defer d.end()
	return Status{WorkspaceID: request.WorkspaceID, Generation: request.Generation, State: StateDraining}, nil
}

func (d *recordingDriver) Stop(_ context.Context, request Request) (Status, error) {
	d.begin()
	defer d.end()
	return Status{WorkspaceID: request.WorkspaceID, Generation: request.Generation, State: StateStopped}, nil
}

func (d *recordingDriver) Delete(context.Context, Request) error {
	d.begin()
	defer d.end()
	d.mu.Lock()
	d.deleteCalls++
	d.mu.Unlock()
	return nil
}

func (d *recordingDriver) DeleteTenantData(context.Context, Request) error {
	d.begin()
	defer d.end()
	d.mu.Lock()
	d.deleteTenantCalls++
	d.mu.Unlock()
	return nil
}

func (d *recordingDriver) maxActiveCalls() int {
	d.mu.Lock()
	defer d.mu.Unlock()
	return d.maxActive
}

func (d *recordingDriver) ensureCallCount() int {
	d.mu.Lock()
	defer d.mu.Unlock()
	return d.ensureCalls
}

func (d *recordingDriver) deleteCallCount() int {
	d.mu.Lock()
	defer d.mu.Unlock()
	return d.deleteCalls
}

func (d *recordingDriver) deleteTenantDataCallCount() int {
	d.mu.Lock()
	defer d.mu.Unlock()
	return d.deleteTenantCalls
}

func (d *recordingDriver) totalCalls() int {
	d.mu.Lock()
	defer d.mu.Unlock()
	return d.ensureCalls + d.deleteCalls + d.deleteTenantCalls
}
