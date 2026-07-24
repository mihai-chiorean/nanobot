package routing

import (
	"context"
	"crypto/sha256"
	"errors"
	"fmt"
	"log/slog"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/mihai-chiorean/nanobot/services/ziggy-control/internal/config"
	"github.com/mihai-chiorean/nanobot/services/ziggy-control/internal/httpapi"
	"github.com/mihai-chiorean/nanobot/services/ziggy-control/internal/identity"
	"github.com/mihai-chiorean/nanobot/services/ziggy-control/internal/telemetry"
	"github.com/mihai-chiorean/nanobot/services/ziggy-control/internal/tenant"
)

// DurableRouter resolves every remembered credential back through the durable
// lifecycle source. That makes disable and deletion take effect immediately
// instead of waiting for a token-cache expiry.
type DurableRouter struct {
	resolver  tenant.Resolver
	logger    *slog.Logger
	telemetry *telemetry.Recorder
	now       func() time.Time
	entries   sync.Map
	routes    sync.Map
	revoked   sync.Map
	count     atomic.Int64
	capacity  int64
}

type durableCredentialRoute struct {
	userID      string
	workspaceID string
	expiresAt   time.Time
}

func NewDurable(resolver tenant.Resolver, logger *slog.Logger, observability *telemetry.Recorder) (*DurableRouter, error) {
	if resolver == nil {
		return nil, errors.New("tenant resolver is required")
	}
	if logger == nil {
		return nil, errors.New("logger is required")
	}
	return &DurableRouter{resolver: resolver, logger: logger, telemetry: observability, now: time.Now, capacity: defaultCredentialCapacity}, nil
}

func (r *DurableRouter) ResolvePrincipal(ctx context.Context, principal identity.Principal) (httpapi.TenantRoute, error) {
	allocation, err := r.resolver.Resolve(ctx, principal)
	if err != nil {
		return httpapi.TenantRoute{}, err
	}
	return r.route(allocation)
}

func (r *DurableRouter) ResolveCredential(ctx context.Context, credential string) (httpapi.TenantRoute, error) {
	key, entry, ok := r.resolveRememberedCredential(credential)
	if !ok {
		return httpapi.TenantRoute{}, tenant.ErrNotAuthorized
	}
	allocation, err := r.resolver.ResolveActive(ctx, entry.userID, entry.workspaceID)
	if err != nil {
		if errors.Is(err, tenant.ErrNotAuthorized) {
			r.evictTenant(entry.userID, entry.workspaceID)
		}
		return httpapi.TenantRoute{}, err
	}
	route, err := r.route(allocation)
	if err != nil {
		if errors.Is(err, tenant.ErrNotAuthorized) {
			r.evictTenant(entry.userID, entry.workspaceID)
		}
		return httpapi.TenantRoute{}, err
	}
	if route.UserID != entry.userID || route.WorkspaceID != entry.workspaceID {
		r.deleteCredential(key)
		return httpapi.TenantRoute{}, tenant.ErrNotAuthorized
	}
	return route, nil
}

func (r *DurableRouter) AdmissionIdentity(credential string) (string, bool) {
	_, entry, ok := r.resolveRememberedCredential(credential)
	if !ok {
		return "", false
	}
	return entry.userID, strings.TrimSpace(entry.userID) != ""
}

func (r *DurableRouter) RememberCredentials(ctx context.Context, route httpapi.TenantRoute, credentials []string, ttl time.Duration) error {
	if ttl <= 0 {
		return errors.New("credential lifetime must be positive")
	}
	if ttl > maximumCredentialTTL {
		ttl = maximumCredentialTTL
	}
	allocation, err := r.resolver.ResolveActive(ctx, route.UserID, route.WorkspaceID)
	if err != nil {
		if errors.Is(err, tenant.ErrNotAuthorized) {
			r.evictTenant(route.UserID, route.WorkspaceID)
		}
		return errors.New("tenant is no longer active")
	}
	if allocation.UserID != route.UserID || allocation.WorkspaceID != route.WorkspaceID {
		r.evictTenant(route.UserID, route.WorkspaceID)
		return errors.New("tenant route changed")
	}
	if r.isRevoked(route.UserID, route.WorkspaceID) {
		return errors.New("tenant is no longer active")
	}
	r.cleanupExpired()
	expiresAt := r.now().Add(ttl)
	for _, credential := range credentials {
		credential = strings.TrimSpace(credential)
		if credential == "" {
			return errors.New("empty transport credential")
		}
		key := sha256.Sum256([]byte(credential))
		entry := durableCredentialRoute{userID: route.UserID, workspaceID: route.WorkspaceID, expiresAt: expiresAt}
		if existing, loaded := r.entries.Load(key); loaded {
			current, valid := existing.(durableCredentialRoute)
			if !valid || current.userID != route.UserID || current.workspaceID != route.WorkspaceID {
				return errors.New("transport credential collision")
			}
			r.entries.Store(key, entry)
			if r.isRevoked(route.UserID, route.WorkspaceID) {
				r.deleteCredential(key)
				return errors.New("tenant is no longer active")
			}
			continue
		}
		if !r.reserveCredentialSlot() {
			return errors.New("credential routing capacity reached")
		}
		actual, loaded := r.entries.LoadOrStore(key, entry)
		if !loaded {
			if r.isRevoked(route.UserID, route.WorkspaceID) {
				r.deleteCredential(key)
				return errors.New("tenant is no longer active")
			}
			continue
		}
		r.count.Add(-1)
		current, valid := actual.(durableCredentialRoute)
		if !valid || current.userID != route.UserID || current.workspaceID != route.WorkspaceID {
			return errors.New("transport credential collision")
		}
		r.entries.Store(key, entry)
		if r.isRevoked(route.UserID, route.WorkspaceID) {
			r.deleteCredential(key)
			return errors.New("tenant is no longer active")
		}
	}
	return nil
}

func (r *DurableRouter) Default() httpapi.TenantRoute { return httpapi.TenantRoute{} }

func (r *DurableRouter) EvictTenant(userID, workspaceID string) {
	r.evictTenant(strings.TrimSpace(userID), strings.TrimSpace(workspaceID))
}

func (r *DurableRouter) route(allocation tenant.Allocation) (httpapi.TenantRoute, error) {
	if allocation.Status != "active" || strings.TrimSpace(allocation.UserID) == "" || strings.TrimSpace(allocation.WorkspaceID) == "" {
		return httpapi.TenantRoute{}, tenant.ErrNotAuthorized
	}
	if r.isRevoked(allocation.UserID, allocation.WorkspaceID) {
		return httpapi.TenantRoute{}, tenant.ErrNotAuthorized
	}
	route := httpapi.TenantRoute{UserID: allocation.UserID, WorkspaceID: allocation.WorkspaceID}
	if strings.TrimSpace(allocation.UpstreamURL) == "" || len(allocation.UpstreamBootstrapSecret) < 32 {
		return route, nil
	}
	key := sha256.Sum256([]byte(allocation.UserID + "\x00" + allocation.WorkspaceID + "\x00" + allocation.UpstreamURL + "\x00" + allocation.UpstreamBootstrapSecret))
	if existing, ok := r.routes.Load(key); ok {
		if cached, ok := existing.(httpapi.TenantRoute); ok {
			return cached, nil
		}
	}
	upstream, err := config.ParsePrivateUpstream(allocation.UpstreamURL)
	if err != nil {
		return httpapi.TenantRoute{}, fmt.Errorf("tenant runtime: %w", err)
	}
	route.UpstreamBootstrapSecret = allocation.UpstreamBootstrapSecret
	route.Proxy = httpapi.NewReverseProxy(upstream, r.logger, r.telemetry)
	actual, _ := r.routes.LoadOrStore(key, route)
	if r.isRevoked(allocation.UserID, allocation.WorkspaceID) {
		r.routes.Delete(key)
		return httpapi.TenantRoute{}, tenant.ErrNotAuthorized
	}
	return actual.(httpapi.TenantRoute), nil
}

func (r *DurableRouter) cleanupExpired() {
	now := r.now()
	r.entries.Range(func(key, value any) bool {
		entry, ok := value.(durableCredentialRoute)
		if !ok || !now.Before(entry.expiresAt) {
			r.deleteCredential(key)
		}
		return true
	})
}

func (r *DurableRouter) resolveRememberedCredential(credential string) ([sha256.Size]byte, durableCredentialRoute, bool) {
	credential = strings.TrimSpace(credential)
	if credential == "" {
		return [sha256.Size]byte{}, durableCredentialRoute{}, false
	}
	key := sha256.Sum256([]byte(credential))
	value, ok := r.entries.Load(key)
	if !ok {
		return key, durableCredentialRoute{}, false
	}
	entry, ok := value.(durableCredentialRoute)
	if !ok || !r.now().Before(entry.expiresAt) {
		r.deleteCredential(key)
		return key, durableCredentialRoute{}, false
	}
	if r.isRevoked(entry.userID, entry.workspaceID) {
		r.deleteCredential(key)
		return key, durableCredentialRoute{}, false
	}
	return key, entry, true
}

func (r *DurableRouter) deleteCredential(key any) {
	if _, deleted := r.entries.LoadAndDelete(key); deleted {
		r.count.Add(-1)
	}
}

func (r *DurableRouter) evictTenant(userID, workspaceID string) {
	if userID == "" || workspaceID == "" {
		return
	}
	r.revoked.Store(tenantRouteIdentity{userID: userID, workspaceID: workspaceID}, struct{}{})
	r.entries.Range(func(key, value any) bool {
		entry, ok := value.(durableCredentialRoute)
		if !ok || (entry.userID == userID && entry.workspaceID == workspaceID) {
			r.deleteCredential(key)
		}
		return true
	})
	r.routes.Range(func(key, value any) bool {
		route, ok := value.(httpapi.TenantRoute)
		if !ok || (route.UserID == userID && route.WorkspaceID == workspaceID) {
			r.routes.Delete(key)
		}
		return true
	})
}

type tenantRouteIdentity struct {
	userID      string
	workspaceID string
}

func (r *DurableRouter) isRevoked(userID, workspaceID string) bool {
	_, revoked := r.revoked.Load(tenantRouteIdentity{userID: userID, workspaceID: workspaceID})
	return revoked
}

func (r *DurableRouter) reserveCredentialSlot() bool {
	for {
		current := r.count.Load()
		if current >= r.capacity {
			return false
		}
		if r.count.CompareAndSwap(current, current+1) {
			return true
		}
	}
}

var _ httpapi.TenantRouter = (*DurableRouter)(nil)
