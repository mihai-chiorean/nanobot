package tenant

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"sync"
	"testing"

	"github.com/mihai-chiorean/nanobot/services/ziggy-control/internal/identity"
)

func TestRegistryBindsVerifiedIdentityOnceAndPersistsIt(t *testing.T) {
	manifestPath, bindingsPath := writeTestManifest(t)
	registry, err := Load(manifestPath, bindingsPath)
	if err != nil {
		t.Fatal(err)
	}

	tester := identity.Principal{Subject: "clerk_tester", Email: "tester@example.com"}
	allocation, err := registry.Resolve(context.Background(), tester)
	if err != nil {
		t.Fatal(err)
	}
	if allocation.UserID != "usr_tester" || allocation.WorkspaceID != "ws_tester" {
		t.Fatalf("allocation = %+v", allocation)
	}
	info, err := os.Stat(bindingsPath)
	if err != nil {
		t.Fatal(err)
	}
	if info.Mode().Perm() != 0o600 {
		t.Fatalf("bindings mode = %o", info.Mode().Perm())
	}

	reloaded, err := Load(manifestPath, bindingsPath)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := reloaded.Resolve(context.Background(), tester); err != nil {
		t.Fatalf("resolve persisted subject: %v", err)
	}
	if _, err := reloaded.Resolve(context.Background(), identity.Principal{
		Subject: "clerk_attacker",
		Email:   "tester@example.com",
	}); !errors.Is(err, ErrNotAuthorized) {
		t.Fatalf("subject replacement error = %v", err)
	}
}

func TestRegistryDeniesCrossTenantIdentityCombinations(t *testing.T) {
	manifestPath, bindingsPath := writeTestManifest(t)
	registry, err := Load(manifestPath, bindingsPath)
	if err != nil {
		t.Fatal(err)
	}

	tests := []identity.Principal{
		{Subject: "clerk_owner", Email: "tester@example.com"},
		{Subject: "clerk_unknown", Email: "unknown@example.com"},
		{Subject: "", Email: "tester@example.com"},
	}
	for _, principal := range tests {
		if _, err := registry.Resolve(context.Background(), principal); !errors.Is(err, ErrNotAuthorized) {
			t.Errorf("principal %+v error = %v", principal, err)
		}
	}
}

func TestRegistryConcurrentFirstLoginHasOneBinding(t *testing.T) {
	manifestPath, bindingsPath := writeTestManifest(t)
	registry, err := Load(manifestPath, bindingsPath)
	if err != nil {
		t.Fatal(err)
	}

	principal := identity.Principal{Subject: "clerk_tester", Email: "tester@example.com"}
	var wait sync.WaitGroup
	errorsFound := make(chan error, 16)
	for range 16 {
		wait.Add(1)
		go func() {
			defer wait.Done()
			_, err := registry.Resolve(context.Background(), principal)
			errorsFound <- err
		}()
	}
	wait.Wait()
	close(errorsFound)
	for err := range errorsFound {
		if err != nil {
			t.Errorf("resolve error = %v", err)
		}
	}

	contents, err := os.ReadFile(bindingsPath)
	if err != nil {
		t.Fatal(err)
	}
	var document bindingFile
	if err := json.Unmarshal(contents, &document); err != nil {
		t.Fatal(err)
	}
	if len(document.Bindings) != 1 || document.Bindings[0].ClerkSubject != principal.Subject {
		t.Fatalf("bindings = %+v", document.Bindings)
	}
}

func TestRegistryRejectsDuplicateWorkspace(t *testing.T) {
	directory := t.TempDir()
	manifestPath := filepath.Join(directory, "tenants.json")
	document := manifest{Version: manifestVersion, Tenants: []Allocation{
		{UserID: "usr_one", WorkspaceID: "ws_shared", Email: "one@example.com", UpstreamURL: "http://127.0.0.1:10001", Status: "active", LegacyDefault: true},
		{UserID: "usr_two", WorkspaceID: "ws_shared", Email: "two@example.com", UpstreamURL: "http://127.0.0.1:10002", Status: "active"},
	}}
	writeJSON(t, manifestPath, document)
	if _, err := Load(manifestPath, filepath.Join(directory, "bindings.json")); err == nil {
		t.Fatal("Load error = nil")
	}
}

func writeTestManifest(t *testing.T) (string, string) {
	t.Helper()
	directory := t.TempDir()
	manifestPath := filepath.Join(directory, "tenants.json")
	bindingsPath := filepath.Join(directory, "state", "bindings.json")
	document := manifest{Version: manifestVersion, Tenants: []Allocation{
		{
			UserID:        "usr_owner",
			WorkspaceID:   "ws_owner",
			Email:         "owner@example.com",
			ClerkSubject:  "clerk_owner",
			UpstreamURL:   "http://127.0.0.1:10001",
			Status:        "active",
			LegacyDefault: true,
		},
		{
			UserID:      "usr_tester",
			WorkspaceID: "ws_tester",
			Email:       "tester@example.com",
			UpstreamURL: "http://127.0.0.1:10002",
			Status:      "active",
		},
	}}
	writeJSON(t, manifestPath, document)
	return manifestPath, bindingsPath
}

func writeJSON(t *testing.T, filename string, value any) {
	t.Helper()
	contents, err := json.Marshal(value)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filename, contents, 0o600); err != nil {
		t.Fatal(err)
	}
}
