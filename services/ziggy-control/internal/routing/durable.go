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
	count     atomic.Int64
	capacity  int64
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

func (r *DurableRouter) ResolveCredential(ctx context.Context, credential string) (httpapi.TenantRoute, bool) {
	credential = strings.TrimSpace(credential)
	if credential == "" {
		return httpapi.TenantRoute{}, false
	}
	key := sha256.Sum256([]byte(credential))
	value, ok := r.entries.Load(key)
	if !ok {
		return httpapi.TenantRoute{}, false
	}
	entry, ok := value.(credentialRoute)
	if !ok || !r.now().Before(entry.expiresAt) {
		if _, deleted := r.entries.LoadAndDelete(key); deleted {
			r.count.Add(-1)
		}
		return httpapi.TenantRoute{}, false
	}
	allocation, err := r.resolver.ResolveActive(ctx, entry.route.UserID, entry.route.WorkspaceID)
	if err != nil {
		return httpapi.TenantRoute{}, false
	}
	route, err := r.route(allocation)
	if err != nil {
		return httpapi.TenantRoute{}, false
	}
	return route, true
}

func (r *DurableRouter) RememberCredentials(ctx context.Context, route httpapi.TenantRoute, credentials []string, ttl time.Duration) error {
	if ttl <= 0 {
		return errors.New("credential lifetime must be positive")
	}
	if ttl > maximumCredentialTTL {
		ttl = maximumCredentialTTL
	}
	if _, err := r.resolver.ResolveActive(ctx, route.UserID, route.WorkspaceID); err != nil {
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
		entry := credentialRoute{route: route, expiresAt: expiresAt}
		if existing, loaded := r.entries.Load(key); loaded {
			current, valid := existing.(credentialRoute)
			if !valid || current.route.UserID != route.UserID || current.route.WorkspaceID != route.WorkspaceID {
				return errors.New("transport credential collision")
			}
			r.entries.Store(key, entry)
			continue
		}
		if !r.reserveCredentialSlot() {
			return errors.New("credential routing capacity reached")
		}
		actual, loaded := r.entries.LoadOrStore(key, entry)
		if !loaded {
			continue
		}
		r.count.Add(-1)
		current, valid := actual.(credentialRoute)
		if !valid || current.route.UserID != route.UserID || current.route.WorkspaceID != route.WorkspaceID {
			return errors.New("transport credential collision")
		}
		r.entries.Store(key, entry)
	}
	return nil
}

func (r *DurableRouter) Default() httpapi.TenantRoute { return httpapi.TenantRoute{} }

func (r *DurableRouter) route(allocation tenant.Allocation) (httpapi.TenantRoute, error) {
	if allocation.Status != "active" || strings.TrimSpace(allocation.UserID) == "" || strings.TrimSpace(allocation.WorkspaceID) == "" {
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
	return actual.(httpapi.TenantRoute), nil
}

func (r *DurableRouter) cleanupExpired() {
	now := r.now()
	r.entries.Range(func(key, value any) bool {
		entry, ok := value.(credentialRoute)
		if !ok || !now.Before(entry.expiresAt) {
			if _, deleted := r.entries.LoadAndDelete(key); deleted {
				r.count.Add(-1)
			}
		}
		return true
	})
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
