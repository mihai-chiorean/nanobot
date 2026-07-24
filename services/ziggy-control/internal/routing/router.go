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

const (
	defaultCredentialCapacity = int64(10_000)
	maximumCredentialTTL      = 24 * time.Hour
)

type credentialRoute struct {
	route     httpapi.TenantRoute
	expiresAt time.Time
}

type Router struct {
	registry *tenant.Registry
	byUserID map[string]httpapi.TenantRoute
	runtimes map[[sha256.Size]byte]httpapi.TenantRoute
	fallback httpapi.TenantRoute
	now      func() time.Time
	entries  sync.Map
	count    atomic.Int64
	capacity int64
}

func New(registry *tenant.Registry, logger *slog.Logger, observability *telemetry.Recorder) (*Router, error) {
	if registry == nil {
		return nil, errors.New("tenant registry is required")
	}
	if logger == nil {
		return nil, errors.New("logger is required")
	}
	router := &Router{
		registry: registry,
		byUserID: make(map[string]httpapi.TenantRoute),
		runtimes: make(map[[sha256.Size]byte]httpapi.TenantRoute),
		now:      time.Now,
		capacity: defaultCredentialCapacity,
	}
	allocations := registry.Allocations()
	bootstrapSecrets := make(map[string]struct{}, len(allocations))
	for _, allocation := range allocations {
		if len(allocation.UpstreamBootstrapSecret) < 32 {
			return nil, fmt.Errorf(
				"tenant %q upstream_bootstrap_secret must contain at least 32 characters",
				allocation.UserID,
			)
		}
		if _, exists := bootstrapSecrets[allocation.UpstreamBootstrapSecret]; exists {
			return nil, fmt.Errorf("tenant %q reuses an upstream bootstrap secret", allocation.UserID)
		}
		bootstrapSecrets[allocation.UpstreamBootstrapSecret] = struct{}{}
		upstream, err := config.ParsePrivateUpstream(allocation.UpstreamURL)
		if err != nil {
			return nil, fmt.Errorf("tenant %q upstream: %w", allocation.UserID, err)
		}
		route := httpapi.TenantRoute{
			UserID:                  allocation.UserID,
			WorkspaceID:             allocation.WorkspaceID,
			UpstreamBootstrapSecret: allocation.UpstreamBootstrapSecret,
			Proxy:                   httpapi.NewReverseProxy(upstream, logger, observability),
		}
		router.byUserID[allocation.UserID] = route
		if allocation.Status == "active" {
			router.runtimes[sha256.Sum256([]byte(allocation.UpstreamBootstrapSecret))] = route
		}
		if allocation.LegacyDefault {
			router.fallback = route
		}
	}
	if router.fallback.Proxy == nil {
		return nil, errors.New("tenant default runtime is required")
	}
	return router, nil
}

func (router *Router) ResolveRuntimeCredential(credential string) (httpapi.TenantRoute, bool) {
	credential = strings.TrimSpace(credential)
	if credential == "" {
		return httpapi.TenantRoute{}, false
	}
	route, ok := router.runtimes[sha256.Sum256([]byte(credential))]
	return route, ok
}

func (router *Router) ResolvePrincipal(ctx context.Context, principal identity.Principal) (httpapi.TenantRoute, error) {
	allocation, err := router.registry.Resolve(ctx, principal)
	if err != nil {
		return httpapi.TenantRoute{}, err
	}
	route, ok := router.byUserID[allocation.UserID]
	if !ok || route.WorkspaceID != allocation.WorkspaceID || route.Proxy == nil {
		return httpapi.TenantRoute{}, tenant.ErrNotAuthorized
	}
	return route, nil
}

func (router *Router) ResolveCredential(_ context.Context, credential string) (httpapi.TenantRoute, error) {
	credential = strings.TrimSpace(credential)
	if credential == "" {
		return httpapi.TenantRoute{}, tenant.ErrNotAuthorized
	}
	key := sha256.Sum256([]byte(credential))
	value, ok := router.entries.Load(key)
	if !ok {
		return httpapi.TenantRoute{}, tenant.ErrNotAuthorized
	}
	entry, ok := value.(credentialRoute)
	if !ok || !router.now().Before(entry.expiresAt) {
		if _, deleted := router.entries.LoadAndDelete(key); deleted {
			router.count.Add(-1)
		}
		return httpapi.TenantRoute{}, tenant.ErrNotAuthorized
	}
	return entry.route, nil
}

func (router *Router) RememberCredentials(_ context.Context, route httpapi.TenantRoute, credentials []string, ttl time.Duration) error {
	known, ok := router.byUserID[route.UserID]
	if !ok || known.WorkspaceID != route.WorkspaceID {
		return errors.New("unknown tenant route")
	}
	if ttl <= 0 {
		return errors.New("credential lifetime must be positive")
	}
	if ttl > maximumCredentialTTL {
		ttl = maximumCredentialTTL
	}
	router.cleanupExpired()
	if router.count.Load()+int64(len(credentials)) > router.capacity {
		return errors.New("credential routing capacity reached")
	}
	expiresAt := router.now().Add(ttl)
	for _, credential := range credentials {
		credential = strings.TrimSpace(credential)
		if credential == "" {
			return errors.New("empty transport credential")
		}
		key := sha256.Sum256([]byte(credential))
		entry := credentialRoute{route: known, expiresAt: expiresAt}
		if existing, loaded := router.entries.LoadOrStore(key, entry); loaded {
			current, valid := existing.(credentialRoute)
			if !valid || current.route.UserID != known.UserID || current.route.WorkspaceID != known.WorkspaceID {
				return errors.New("transport credential collision")
			}
			router.entries.Store(key, entry)
			continue
		}
		router.count.Add(1)
	}
	return nil
}

func (router *Router) Default() httpapi.TenantRoute {
	return router.fallback
}

func (router *Router) TenantCount() int {
	return len(router.byUserID)
}

func (router *Router) cleanupExpired() {
	now := router.now()
	router.entries.Range(func(key, value any) bool {
		entry, ok := value.(credentialRoute)
		if !ok || !now.Before(entry.expiresAt) {
			if _, deleted := router.entries.LoadAndDelete(key); deleted {
				router.count.Add(-1)
			}
		}
		return true
	})
}
