package routing

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"testing"
	"time"

	"github.com/mihai-chiorean/nanobot/services/ziggy-control/internal/identity"
	"github.com/mihai-chiorean/nanobot/services/ziggy-control/internal/tenant"
)

type durableResolver struct {
	allocation tenant.Allocation
	active     bool
}

func (r *durableResolver) Resolve(context.Context, identity.Principal) (tenant.Allocation, error) {
	return r.allocation, nil
}

func (r *durableResolver) ResolveActive(context.Context, string, string) (tenant.Allocation, error) {
	if !r.active || r.allocation.UpstreamURL == "" || len(r.allocation.UpstreamBootstrapSecret) < 32 {
		return tenant.Allocation{}, errors.New("runtime unavailable")
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
	if err := router.RememberCredentials(context.Background(), route, []string{"transport-token"}, time.Minute); err != nil {
		t.Fatal(err)
	}
	if _, ok := router.ResolveCredential(context.Background(), "transport-token"); !ok {
		t.Fatal("active credential did not resolve")
	}
	resolver.active = false
	if _, ok := router.ResolveCredential(context.Background(), "transport-token"); ok {
		t.Fatal("disabled credential resolved")
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

var _ tenant.Resolver = (*durableResolver)(nil)
