package tenant

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/mail"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/mihai-chiorean/nanobot/services/ziggy-control/internal/identity"
)

const manifestVersion = 1

var (
	ErrNotAuthorized = errors.New("tenant is not authorized")
	idPattern        = regexp.MustCompile(`^[a-z0-9][a-z0-9_-]{2,63}$`)
)

type Allocation struct {
	UserID                  string `json:"user_id"`
	WorkspaceID             string `json:"workspace_id"`
	Email                   string `json:"email"`
	ClerkSubject            string `json:"clerk_subject,omitempty"`
	UpstreamURL             string `json:"upstream_url"`
	UpstreamBootstrapSecret string `json:"upstream_bootstrap_secret"`
	Status                  string `json:"status"`
	LegacyDefault           bool   `json:"legacy_default,omitempty"`
}

type manifest struct {
	Version int          `json:"version"`
	Tenants []Allocation `json:"tenants"`
}

type binding struct {
	UserID       string    `json:"user_id"`
	ClerkSubject string    `json:"clerk_subject"`
	BoundAt      time.Time `json:"bound_at"`
}

type bindingFile struct {
	Version  int       `json:"version"`
	Bindings []binding `json:"bindings"`
}

type snapshot struct {
	byUserID  map[string]Allocation
	byEmail   map[string]Allocation
	bySubject map[string]Allocation
	bindings  map[string]binding
	all       []Allocation
	fallback  Allocation
}

type Registry struct {
	bindingsPath string
	now          func() time.Time
	bindMu       sync.Mutex
	state        atomic.Pointer[snapshot]
}

func Load(manifestPath, bindingsPath string) (*Registry, error) {
	contents, err := os.ReadFile(manifestPath)
	if err != nil {
		return nil, fmt.Errorf("read tenant manifest: %w", err)
	}
	var document manifest
	if err := decodeStrict(contents, &document); err != nil {
		return nil, fmt.Errorf("decode tenant manifest: %w", err)
	}
	if document.Version != manifestVersion {
		return nil, fmt.Errorf("tenant manifest version %d is unsupported", document.Version)
	}

	bindings, err := loadBindings(bindingsPath)
	if err != nil {
		return nil, err
	}
	state, err := buildSnapshot(document.Tenants, bindings)
	if err != nil {
		return nil, err
	}
	registry := &Registry{bindingsPath: bindingsPath, now: time.Now}
	registry.state.Store(state)
	return registry, nil
}

func (registry *Registry) Resolve(_ context.Context, principal identity.Principal) (Allocation, error) {
	state := registry.state.Load()
	if state == nil {
		return Allocation{}, ErrNotAuthorized
	}
	email, err := canonicalEmail(principal.Email)
	if err != nil || strings.TrimSpace(principal.Subject) == "" {
		return Allocation{}, ErrNotAuthorized
	}
	subject := strings.TrimSpace(principal.Subject)

	if allocation, ok := state.bySubject[subject]; ok {
		if allocation.Email != email || allocation.Status != "active" {
			return Allocation{}, ErrNotAuthorized
		}
		return allocation, nil
	}

	allocation, ok := state.byEmail[email]
	if !ok || allocation.Status != "active" {
		return Allocation{}, ErrNotAuthorized
	}
	if allocation.ClerkSubject != "" && allocation.ClerkSubject != subject {
		return Allocation{}, ErrNotAuthorized
	}
	if existing, ok := state.bindings[allocation.UserID]; ok && existing.ClerkSubject != subject {
		return Allocation{}, ErrNotAuthorized
	}
	return registry.bindSubject(allocation, subject)
}

// ResolveActive supports the same server-side credential recheck contract as
// the durable resolver. Manifest mode remains immutable until restart.
func (registry *Registry) ResolveActive(_ context.Context, userID, workspaceID string) (Allocation, error) {
	state := registry.state.Load()
	if state == nil {
		return Allocation{}, ErrNotAuthorized
	}
	allocation, ok := state.byUserID[strings.TrimSpace(userID)]
	if !ok || allocation.WorkspaceID != strings.TrimSpace(workspaceID) || allocation.Status != "active" {
		return Allocation{}, ErrNotAuthorized
	}
	return allocation, nil
}

func (registry *Registry) Allocations() []Allocation {
	state := registry.state.Load()
	if state == nil {
		return nil
	}
	return append([]Allocation(nil), state.all...)
}

func (registry *Registry) Default() Allocation {
	state := registry.state.Load()
	if state == nil {
		return Allocation{}
	}
	return state.fallback
}

var _ Resolver = (*Registry)(nil)

func (registry *Registry) bindSubject(allocation Allocation, subject string) (Allocation, error) {
	registry.bindMu.Lock()
	defer registry.bindMu.Unlock()

	current := registry.state.Load()
	if current == nil {
		return Allocation{}, ErrNotAuthorized
	}
	if existing, ok := current.bySubject[subject]; ok {
		if existing.UserID == allocation.UserID {
			return existing, nil
		}
		return Allocation{}, ErrNotAuthorized
	}
	if existing, ok := current.bindings[allocation.UserID]; ok {
		if existing.ClerkSubject == subject {
			return current.byUserID[allocation.UserID], nil
		}
		return Allocation{}, ErrNotAuthorized
	}

	nextBindings := make(map[string]binding, len(current.bindings)+1)
	for userID, existing := range current.bindings {
		nextBindings[userID] = existing
	}
	nextBindings[allocation.UserID] = binding{
		UserID:       allocation.UserID,
		ClerkSubject: subject,
		BoundAt:      registry.now().UTC(),
	}
	if err := persistBindings(registry.bindingsPath, nextBindings); err != nil {
		return Allocation{}, fmt.Errorf("persist tenant identity binding: %w", err)
	}

	next, err := buildSnapshot(current.all, nextBindings)
	if err != nil {
		return Allocation{}, err
	}
	registry.state.Store(next)
	return next.byUserID[allocation.UserID], nil
}

func buildSnapshot(allocations []Allocation, bindings map[string]binding) (*snapshot, error) {
	if len(allocations) == 0 {
		return nil, errors.New("tenant manifest must contain at least one tenant")
	}
	result := &snapshot{
		byUserID:  make(map[string]Allocation, len(allocations)),
		byEmail:   make(map[string]Allocation, len(allocations)),
		bySubject: make(map[string]Allocation, len(allocations)),
		bindings:  make(map[string]binding, len(bindings)),
		all:       make([]Allocation, 0, len(allocations)),
	}
	workspaceIDs := make(map[string]struct{}, len(allocations))
	upstreams := make(map[string]struct{}, len(allocations))
	defaultCount := 0

	for _, candidate := range allocations {
		allocation := candidate
		allocation.UserID = strings.TrimSpace(allocation.UserID)
		allocation.WorkspaceID = strings.TrimSpace(allocation.WorkspaceID)
		allocation.ClerkSubject = strings.TrimSpace(allocation.ClerkSubject)
		allocation.UpstreamURL = strings.TrimSpace(allocation.UpstreamURL)
		allocation.UpstreamBootstrapSecret = strings.TrimSpace(allocation.UpstreamBootstrapSecret)
		allocation.Status = strings.ToLower(strings.TrimSpace(allocation.Status))
		email, err := canonicalEmail(allocation.Email)
		if err != nil {
			return nil, fmt.Errorf("tenant %q email: %w", allocation.UserID, err)
		}
		allocation.Email = email
		if !idPattern.MatchString(allocation.UserID) {
			return nil, fmt.Errorf("tenant user_id %q is invalid", allocation.UserID)
		}
		if !idPattern.MatchString(allocation.WorkspaceID) {
			return nil, fmt.Errorf("tenant workspace_id %q is invalid", allocation.WorkspaceID)
		}
		if allocation.UpstreamURL == "" {
			return nil, fmt.Errorf("tenant %q upstream_url is required", allocation.UserID)
		}
		if allocation.Status != "active" && allocation.Status != "disabled" {
			return nil, fmt.Errorf("tenant %q status must be active or disabled", allocation.UserID)
		}
		if _, exists := result.byUserID[allocation.UserID]; exists {
			return nil, fmt.Errorf("duplicate tenant user_id %q", allocation.UserID)
		}
		if _, exists := result.byEmail[allocation.Email]; exists {
			return nil, fmt.Errorf("duplicate tenant email %q", allocation.Email)
		}
		if _, exists := workspaceIDs[allocation.WorkspaceID]; exists {
			return nil, fmt.Errorf("duplicate tenant workspace_id %q", allocation.WorkspaceID)
		}
		if _, exists := upstreams[allocation.UpstreamURL]; exists {
			return nil, fmt.Errorf("duplicate tenant upstream_url %q", allocation.UpstreamURL)
		}
		if allocation.ClerkSubject != "" {
			if _, exists := result.bySubject[allocation.ClerkSubject]; exists {
				return nil, fmt.Errorf("duplicate tenant clerk_subject")
			}
			result.bySubject[allocation.ClerkSubject] = allocation
		}
		if allocation.LegacyDefault {
			defaultCount++
			result.fallback = allocation
		}
		result.byUserID[allocation.UserID] = allocation
		result.byEmail[allocation.Email] = allocation
		workspaceIDs[allocation.WorkspaceID] = struct{}{}
		upstreams[allocation.UpstreamURL] = struct{}{}
		result.all = append(result.all, allocation)
	}
	if defaultCount != 1 {
		return nil, fmt.Errorf("tenant manifest must contain exactly one legacy_default tenant")
	}

	for userID, persisted := range bindings {
		allocation, exists := result.byUserID[userID]
		if !exists {
			return nil, fmt.Errorf("identity binding references unknown user_id %q", userID)
		}
		if persisted.UserID != userID || strings.TrimSpace(persisted.ClerkSubject) == "" {
			return nil, fmt.Errorf("identity binding for user_id %q is invalid", userID)
		}
		if allocation.ClerkSubject != "" && allocation.ClerkSubject != persisted.ClerkSubject {
			return nil, fmt.Errorf("identity binding conflicts with pinned subject for user_id %q", userID)
		}
		if other, exists := result.bySubject[persisted.ClerkSubject]; exists && other.UserID != userID {
			return nil, fmt.Errorf("identity subject is bound to multiple tenants")
		}
		result.bindings[userID] = persisted
		result.bySubject[persisted.ClerkSubject] = allocation
	}
	return result, nil
}

func loadBindings(filename string) (map[string]binding, error) {
	contents, err := os.ReadFile(filename)
	if errors.Is(err, os.ErrNotExist) {
		return make(map[string]binding), nil
	}
	if err != nil {
		return nil, fmt.Errorf("read tenant identity bindings: %w", err)
	}
	var document bindingFile
	if err := decodeStrict(contents, &document); err != nil {
		return nil, fmt.Errorf("decode tenant identity bindings: %w", err)
	}
	if document.Version != manifestVersion {
		return nil, fmt.Errorf("tenant identity binding version %d is unsupported", document.Version)
	}
	result := make(map[string]binding, len(document.Bindings))
	for _, item := range document.Bindings {
		if _, exists := result[item.UserID]; exists {
			return nil, fmt.Errorf("duplicate identity binding for user_id %q", item.UserID)
		}
		result[item.UserID] = item
	}
	return result, nil
}

func persistBindings(filename string, bindings map[string]binding) error {
	document := bindingFile{Version: manifestVersion, Bindings: make([]binding, 0, len(bindings))}
	for _, item := range bindings {
		document.Bindings = append(document.Bindings, item)
	}
	contents, err := json.MarshalIndent(document, "", "  ")
	if err != nil {
		return err
	}
	contents = append(contents, '\n')
	directory := filepath.Dir(filename)
	if err := os.MkdirAll(directory, 0o700); err != nil {
		return err
	}
	temporary, err := os.CreateTemp(directory, ".tenant-bindings-*")
	if err != nil {
		return err
	}
	temporaryName := temporary.Name()
	defer os.Remove(temporaryName)
	if err := temporary.Chmod(0o600); err != nil {
		_ = temporary.Close()
		return err
	}
	if _, err := temporary.Write(contents); err != nil {
		_ = temporary.Close()
		return err
	}
	if err := temporary.Sync(); err != nil {
		_ = temporary.Close()
		return err
	}
	if err := temporary.Close(); err != nil {
		return err
	}
	if err := os.Rename(temporaryName, filename); err != nil {
		return err
	}
	dir, err := os.Open(directory)
	if err != nil {
		return err
	}
	defer dir.Close()
	return dir.Sync()
}

func decodeStrict(contents []byte, target any) error {
	decoder := json.NewDecoder(strings.NewReader(string(contents)))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(target); err != nil {
		return err
	}
	var trailing any
	if err := decoder.Decode(&trailing); !errors.Is(err, io.EOF) {
		return errors.New("unexpected trailing JSON value")
	}
	return nil
}

func canonicalEmail(value string) (string, error) {
	email := strings.ToLower(strings.TrimSpace(value))
	parsed, err := mail.ParseAddress(email)
	if err != nil || parsed.Address != email {
		return "", errors.New("invalid email address")
	}
	return email, nil
}
