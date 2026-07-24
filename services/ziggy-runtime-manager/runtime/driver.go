// Package runtime defines the only lifecycle surface a future control plane may use.
package runtime

import (
	"context"
	"errors"
	"time"
)

var (
	ErrUnknownWorkspace   = errors.New("runtime workspace is not allocated")
	ErrInvalidWorkspace   = errors.New("runtime workspace ID is invalid")
	ErrInvalidGeneration  = errors.New("runtime generation must be positive")
	ErrStaleGeneration    = errors.New("runtime request generation is stale")
	ErrGenerationConflict = errors.New("runtime generation conflicts with a live runtime")
	ErrNotFound           = errors.New("runtime not found")
)

// Request carries identity and a monotonic fence only. The manager policy, rather
// than a caller, selects image, mounts, environment, resources, network and ports.
type Request struct {
	WorkspaceID string
	Generation  uint64
}

// Status contains no credentials, configuration, tenant content, or runtime logs.
type Status struct {
	WorkspaceID string
	Generation  uint64
	State       State
	Endpoint    string
	ObservedAt  time.Time
}

type State string

const (
	StateAbsent   State = "absent"
	StateStarting State = "starting"
	StateRunning  State = "running"
	StateDraining State = "draining"
	StateStopped  State = "stopped"
	StateFailed   State = "failed"
)

// Driver is intentionally narrow. Lease acquisition, turn ownership, and routing
// remain PostgreSQL/control-plane responsibilities and are not implemented here.
type Driver interface {
	EnsureRunning(context.Context, Request) (Status, error)
	Health(context.Context, Request) (Status, error)
	Drain(context.Context, Request) (Status, error)
	Stop(context.Context, Request) (Status, error)
	// Delete removes only generation-scoped runtime artifacts. It preserves the
	// tenant workspace volume for a replacement generation.
	Delete(context.Context, Request) error
	// DeleteTenantData is an explicit destructive operation for the persistent
	// workspace volume and must not be used for ordinary generation replacement.
	DeleteTenantData(context.Context, Request) error
}

func (r Request) Valid() bool { return r.WorkspaceID != "" && r.Generation > 0 }
