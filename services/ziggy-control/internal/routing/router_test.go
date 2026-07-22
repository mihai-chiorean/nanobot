package routing

import (
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/mihai-chiorean/nanobot/services/ziggy-control/internal/identity"
	"github.com/mihai-chiorean/nanobot/services/ziggy-control/internal/tenant"
)

func TestRouterKeepsCredentialsBoundToTheirRuntime(t *testing.T) {
	ownerRuntime := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusNoContent)
	}))
	defer ownerRuntime.Close()
	testerRuntime := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusNoContent)
	}))
	defer testerRuntime.Close()

	registry := loadTestRegistry(t, ownerRuntime.URL, testerRuntime.URL)
	router, err := New(registry, slog.New(slog.NewTextHandler(io.Discard, nil)), nil)
	if err != nil {
		t.Fatal(err)
	}
	owner, err := router.ResolvePrincipal(context.Background(), identity.Principal{Subject: "clerk_owner", Email: "owner@example.com"})
	if err != nil {
		t.Fatal(err)
	}
	tester, err := router.ResolvePrincipal(context.Background(), identity.Principal{Subject: "clerk_tester", Email: "tester@example.com"})
	if err != nil {
		t.Fatal(err)
	}
	if owner.WorkspaceID == tester.WorkspaceID {
		t.Fatal("owner and tester resolved to the same workspace")
	}
	if err := router.RememberCredentials(owner, []string{"owner-token"}, time.Minute); err != nil {
		t.Fatal(err)
	}
	if err := router.RememberCredentials(tester, []string{"tester-token"}, time.Minute); err != nil {
		t.Fatal(err)
	}

	resolvedOwner, ok := router.ResolveCredential("owner-token")
	if !ok || resolvedOwner.WorkspaceID != owner.WorkspaceID {
		t.Fatalf("owner token route = %+v, %v", resolvedOwner, ok)
	}
	resolvedTester, ok := router.ResolveCredential("tester-token")
	if !ok || resolvedTester.WorkspaceID != tester.WorkspaceID {
		t.Fatalf("tester token route = %+v, %v", resolvedTester, ok)
	}
	if _, ok := router.ResolveCredential("unknown-token"); ok {
		t.Fatal("unknown token resolved")
	}
}

func TestRouterExpiresCredentialWithoutAReaperGoroutine(t *testing.T) {
	runtime := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {}))
	defer runtime.Close()
	registry := loadTestRegistry(t, runtime.URL, "http://127.0.0.1:10002")
	router, err := New(registry, slog.New(slog.NewTextHandler(io.Discard, nil)), nil)
	if err != nil {
		t.Fatal(err)
	}
	now := time.Date(2026, 7, 21, 12, 0, 0, 0, time.UTC)
	router.now = func() time.Time { return now }
	owner, err := router.ResolvePrincipal(context.Background(), identity.Principal{Subject: "clerk_owner", Email: "owner@example.com"})
	if err != nil {
		t.Fatal(err)
	}
	if err := router.RememberCredentials(owner, []string{"short-token"}, time.Minute); err != nil {
		t.Fatal(err)
	}
	now = now.Add(2 * time.Minute)
	if _, ok := router.ResolveCredential("short-token"); ok {
		t.Fatal("expired token resolved")
	}
	if router.count.Load() != 0 {
		t.Fatalf("credential count = %d", router.count.Load())
	}
}

func loadTestRegistry(t *testing.T, ownerURL, testerURL string) *tenant.Registry {
	t.Helper()
	directory := t.TempDir()
	manifestPath := filepath.Join(directory, "tenants.json")
	bindingsPath := filepath.Join(directory, "bindings.json")
	document := map[string]any{
		"version": 1,
		"tenants": []map[string]any{
			{
				"user_id": "usr_owner", "workspace_id": "ws_owner", "email": "owner@example.com",
				"clerk_subject": "clerk_owner", "upstream_url": ownerURL, "status": "active", "legacy_default": true,
			},
			{
				"user_id": "usr_tester", "workspace_id": "ws_tester", "email": "tester@example.com",
				"clerk_subject": "clerk_tester", "upstream_url": testerURL, "status": "active",
			},
		},
	}
	contents, err := json.Marshal(document)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(manifestPath, contents, 0o600); err != nil {
		t.Fatal(err)
	}
	registry, err := tenant.Load(manifestPath, bindingsPath)
	if err != nil {
		t.Fatal(err)
	}
	return registry
}
