package routing

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/mihai-chiorean/nanobot/services/ziggy-control/internal/identity"
	"github.com/mihai-chiorean/nanobot/services/ziggy-control/internal/tenant"
)

type durableResolver struct {
	allocation  tenant.Allocation
	active      bool
	activeCalls atomic.Int32
}

func (r *durableResolver) Resolve(context.Context, identity.Principal) (tenant.Allocation, error) {
	return r.allocation, nil
}

func (r *durableResolver) ResolveActive(context.Context, string, string) (tenant.Allocation, error) {
	r.activeCalls.Add(1)
	if !r.active || r.allocation.UpstreamURL == "" || len(r.allocation.UpstreamBootstrapSecret) < 32 {
		return tenant.Allocation{}, tenant.ErrNotAuthorized
	}
	return r.allocation, nil
}

func TestDurableRouterRechecksRememberedCredentials(t *testing.T) {
	resolver := &durableResolver{active: true, allocation: tenant.Allocation{
		UserID: "usr_test", WorkspaceID: "ws_test", Status: "active",
		UpstreamURL: "http://127.0.0.1:8765", UpstreamBootstrapSecret: "01234567890123456789012345678901",
	}}
	router, err := NewDurable(resolver, slog.New(slog.NewTextHandler(io.Discard, nil)), nil)
	if err != nil {
		t.Fatal(err)
	}
	route, err := router.ResolvePrincipal(context.Background(), identity.Principal{Subject: "clerk_test", Email: "test@example.com"})
	if err != nil {
		t.Fatal(err)
	}
	if err := router.RememberCredentials(context.Background(), route, []string{"transport-token", "transport-token-2"}, time.Minute); err != nil {
		t.Fatal(err)
	}
	activeCalls := resolver.activeCalls.Load()
	if userID, ok := router.AdmissionIdentity("transport-token"); !ok || userID != "usr_test" {
		t.Fatalf("admission identity = %q/%v, want usr_test/true", userID, ok)
	}
	if resolver.activeCalls.Load() != activeCalls {
		t.Fatal("admission identity performed a durable resolver call")
	}
	if _, err := router.ResolveCredential(context.Background(), "transport-token"); err != nil {
		t.Fatalf("active credential did not resolve: %v", err)
	}
	resolver.active = false
	if _, err := router.ResolveCredential(context.Background(), "transport-token"); err == nil {
		t.Fatal("disabled credential resolved")
	}
	if got := router.count.Load(); got != 0 {
		t.Fatalf("credential count after revocation = %d, want 0", got)
	}
	if entries := syncMapSize(&router.entries); entries != 0 {
		t.Fatalf("credential cache size after revocation = %d, want 0", entries)
	}
	if routes := syncMapSize(&router.routes); routes != 0 {
		t.Fatalf("route cache size after revocation = %d, want 0", routes)
	}
}

func TestDurableRouterExplicitTenantEvictionScrubsAllCachedState(t *testing.T) {
	resolver := &durableResolver{active: true, allocation: tenant.Allocation{
		UserID: "usr_test", WorkspaceID: "ws_test", Status: "active",
		UpstreamURL: "http://127.0.0.1:8765", UpstreamBootstrapSecret: "01234567890123456789012345678901",
	}}
	router, err := NewDurable(resolver, slog.New(slog.NewTextHandler(io.Discard, nil)), nil)
	if err != nil {
		t.Fatal(err)
	}
	route, err := router.ResolvePrincipal(context.Background(), identity.Principal{})
	if err != nil {
		t.Fatal(err)
	}
	if err := router.RememberCredentials(context.Background(), route, []string{"one", "two"}, time.Minute); err != nil {
		t.Fatal(err)
	}
	router.EvictTenant(route.UserID, route.WorkspaceID)
	if got := router.count.Load(); got != 0 {
		t.Fatalf("credential count after explicit eviction = %d, want 0", got)
	}
	if entries := syncMapSize(&router.entries); entries != 0 {
		t.Fatalf("credential cache size after explicit eviction = %d, want 0", entries)
	}
	if routes := syncMapSize(&router.routes); routes != 0 {
		t.Fatalf("route cache size after explicit eviction = %d, want 0", routes)
	}
	if _, err := router.route(resolver.allocation); !errors.Is(err, tenant.ErrNotAuthorized) {
		t.Fatalf("revoked route recreation error = %v, want not authorized", err)
	}
	if routes := syncMapSize(&router.routes); routes != 0 {
		t.Fatalf("route cache was repopulated after revocation: %d", routes)
	}
}

func TestDurableRouterLeavesPendingRuntimeUnroutable(t *testing.T) {
	resolver := &durableResolver{active: true, allocation: tenant.Allocation{UserID: "usr_test", WorkspaceID: "ws_test", Status: "active"}}
	router, err := NewDurable(resolver, slog.New(slog.NewTextHandler(io.Discard, nil)), nil)
	if err != nil {
		t.Fatal(err)
	}
	route, err := router.ResolvePrincipal(context.Background(), identity.Principal{})
	if err != nil {
		t.Fatal(err)
	}
	if route.Proxy != nil {
		t.Fatal("pending runtime received a proxy")
	}
	if err := router.RememberCredentials(context.Background(), route, []string{"token"}, time.Minute); err == nil {
		t.Fatal("pending runtime credentials were remembered")
	}
}

func TestDurableRouterConcurrentCredentialCapacity(t *testing.T) {
	resolver := &durableResolver{active: true, allocation: tenant.Allocation{
		UserID: "usr_test", WorkspaceID: "ws_test", Status: "active",
		UpstreamURL: "http://127.0.0.1:8765", UpstreamBootstrapSecret: "01234567890123456789012345678901",
	}}
	router, err := NewDurable(resolver, slog.New(slog.NewTextHandler(io.Discard, nil)), nil)
	if err != nil {
		t.Fatal(err)
	}
	router.capacity = 8
	route, err := router.ResolvePrincipal(context.Background(), identity.Principal{Subject: "clerk_test", Email: "test@example.com"})
	if err != nil {
		t.Fatal(err)
	}

	const contenders = 32
	start := make(chan struct{})
	errs := make(chan error, contenders)
	var group sync.WaitGroup
	for index := range contenders {
		group.Add(1)
		go func(index int) {
			defer group.Done()
			<-start
			errs <- router.RememberCredentials(context.Background(), route, []string{fmt.Sprintf("credential-%d", index)}, time.Minute)
		}(index)
	}
	close(start)
	group.Wait()
	close(errs)
	successes := 0
	for err := range errs {
		if err == nil {
			successes++
			continue
		}
		if err.Error() != "credential routing capacity reached" {
			t.Fatalf("RememberCredentials() error = %v", err)
		}
	}
	if successes != int(router.capacity) {
		t.Fatalf("successful admissions = %d, want %d", successes, router.capacity)
	}
	if got := router.count.Load(); got != router.capacity {
		t.Fatalf("credential count = %d, want %d", got, router.capacity)
	}
	entries := 0
	router.entries.Range(func(_, _ any) bool {
		entries++
		return true
	})
	if entries != int(router.capacity) {
		t.Fatalf("stored credentials = %d, want %d", entries, router.capacity)
	}
}

var _ tenant.Resolver = (*durableResolver)(nil)

func syncMapSize(entries *sync.Map) int {
	size := 0
	entries.Range(func(_, _ any) bool {
		size++
		return true
	})
	return size
}
