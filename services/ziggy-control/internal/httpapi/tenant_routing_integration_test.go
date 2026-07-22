package httpapi_test

import (
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/mihai-chiorean/nanobot/services/ziggy-control/internal/httpapi"
	"github.com/mihai-chiorean/nanobot/services/ziggy-control/internal/identity"
	"github.com/mihai-chiorean/nanobot/services/ziggy-control/internal/routing"
	"github.com/mihai-chiorean/nanobot/services/ziggy-control/internal/tenant"
)

type alwaysReady struct{}

func (alwaysReady) Check(context.Context) error { return nil }

func TestTenantBootstrapPinsEveryCapabilityToOneRuntime(t *testing.T) {
	ownerRequests := 0
	testerRequests := 0
	ownerRuntime := tenantRuntime(t, "owner-token", "owner", &ownerRequests)
	defer ownerRuntime.Close()
	testerRuntime := tenantRuntime(t, "tester-token", "tester", &testerRequests)
	defer testerRuntime.Close()

	registry := integrationRegistry(t, ownerRuntime.URL, testerRuntime.URL)
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	tenantRouter, err := routing.New(registry, logger, nil)
	if err != nil {
		t.Fatal(err)
	}
	ownerURL, err := url.Parse(ownerRuntime.URL)
	if err != nil {
		t.Fatal(err)
	}
	authenticate := httpapi.Middleware(func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			principal := identity.Principal{Subject: "clerk_tester", Email: "tester@example.com"}
			if r.Header.Get("X-Test-User") == "owner" {
				principal = identity.Principal{Subject: "clerk_owner", Email: "owner@example.com"}
			}
			next.ServeHTTP(w, r.WithContext(identity.NewContext(r.Context(), principal)))
		})
	})
	handler, err := httpapi.New(httpapi.Config{
		Authenticate:   authenticate,
		Proxy:          httpapi.NewReverseProxy(ownerURL, logger, nil),
		TenantRouter:   tenantRouter,
		Readiness:      alwaysReady{},
		Logger:         logger,
		OwnerEmail:     "owner@example.com",
		OwnerSubject:   "clerk_owner",
		MaxRequestBody: 1 << 20,
		Version:        "test",
	})
	if err != nil {
		t.Fatal(err)
	}

	testerBootstrap := httptest.NewRecorder()
	handler.ServeHTTP(testerBootstrap, httptest.NewRequest(http.MethodGet, "/auth/bootstrap", nil))
	if testerBootstrap.Code != http.StatusOK || !strings.Contains(testerBootstrap.Body.String(), "tester-token") {
		t.Fatalf("tester bootstrap = %d %s", testerBootstrap.Code, testerBootstrap.Body.String())
	}

	ownerBootstrapRequest := httptest.NewRequest(http.MethodGet, "/auth/bootstrap", nil)
	ownerBootstrapRequest.Header.Set("X-Test-User", "owner")
	ownerBootstrap := httptest.NewRecorder()
	handler.ServeHTTP(ownerBootstrap, ownerBootstrapRequest)
	if ownerBootstrap.Code != http.StatusOK || !strings.Contains(ownerBootstrap.Body.String(), "owner-token") {
		t.Fatalf("owner bootstrap = %d %s", ownerBootstrap.Code, ownerBootstrap.Body.String())
	}

	testerAPIRequest := httptest.NewRequest(http.MethodGet, "/api/sessions?workspace_id=ws_owner", nil)
	testerAPIRequest.Header.Set("Authorization", "Bearer tester-token")
	testerAPI := httptest.NewRecorder()
	handler.ServeHTTP(testerAPI, testerAPIRequest)
	if testerAPI.Code != http.StatusOK || testerAPI.Body.String() != "tester" {
		t.Fatalf("tester API = %d %q", testerAPI.Code, testerAPI.Body.String())
	}

	ownerAPIRequest := httptest.NewRequest(http.MethodGet, "/api/sessions?workspace_id=ws_tester", nil)
	ownerAPIRequest.Header.Set("Authorization", "Bearer owner-token")
	ownerAPI := httptest.NewRecorder()
	handler.ServeHTTP(ownerAPI, ownerAPIRequest)
	if ownerAPI.Code != http.StatusOK || ownerAPI.Body.String() != "owner" {
		t.Fatalf("owner API = %d %q", ownerAPI.Code, ownerAPI.Body.String())
	}

	unknownRequest := httptest.NewRequest(http.MethodGet, "/api/sessions", nil)
	unknownRequest.Header.Set("Authorization", "Bearer unknown-token")
	unknown := httptest.NewRecorder()
	handler.ServeHTTP(unknown, unknownRequest)
	if unknown.Code != http.StatusUnauthorized {
		t.Fatalf("unknown token status = %d", unknown.Code)
	}
	if ownerRequests != 2 || testerRequests != 2 {
		t.Fatalf("runtime requests: owner=%d tester=%d", ownerRequests, testerRequests)
	}
}

func tenantRuntime(t *testing.T, token, name string, requests *int) *httptest.Server {
	t.Helper()
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		(*requests)++
		switch r.URL.Path {
		case "/auth/bootstrap":
			_ = json.NewEncoder(w).Encode(map[string]any{
				"token": token, "expires_in": 300, "ws_path": "/",
			})
		case "/api/sessions":
			_, _ = io.WriteString(w, name)
		default:
			http.NotFound(w, r)
		}
	}))
}

func integrationRegistry(t *testing.T, ownerURL, testerURL string) *tenant.Registry {
	t.Helper()
	directory := t.TempDir()
	manifestPath := filepath.Join(directory, "tenants.json")
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
	registry, err := tenant.Load(manifestPath, filepath.Join(directory, "bindings.json"))
	if err != nil {
		t.Fatal(err)
	}
	return registry
}
