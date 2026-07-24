package httpapi

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"net/url"
	"sync/atomic"
	"testing"
	"time"

	"github.com/mihai-chiorean/nanobot/services/ziggy-control/internal/identity"
)

type workCredentialRouter struct {
	routes map[string]TenantRoute
}

func (router workCredentialRouter) ResolvePrincipal(context.Context, identity.Principal) (TenantRoute, error) {
	return TenantRoute{}, errors.New("principal resolution is not used by Work")
}

func (router workCredentialRouter) ResolveCredential(_ context.Context, credential string) (TenantRoute, bool) {
	route, ok := router.routes[credential]
	return route, ok
}

func (router workCredentialRouter) RememberCredentials(context.Context, TenantRoute, []string, time.Duration) error {
	return nil
}

func (router workCredentialRouter) Default() TenantRoute { return TenantRoute{} }

func TestWorkProxyUsesRememberedTenantBearerAndStripsSpoofedIdentity(t *testing.T) {
	key := []byte("01234567890123456789012345678901")
	var requests atomic.Int32
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requests.Add(1)
		if r.URL.Path != "/api/work/runs/next" || r.URL.RawQuery != "workspace_id=ignored" {
			t.Errorf("Work path = %q?%s", r.URL.Path, r.URL.RawQuery)
		}
		if got := r.Header.Get("Authorization"); got != "" {
			t.Errorf("Work Authorization = %q", got)
		}
		for _, header := range []string{"Cookie", "X-Ziggy-Forged", "X-Ziggy-Subject", "X-Ziggy-Email"} {
			if got := r.Header.Get(header); got != "" {
				t.Errorf("Work %s = %q", header, got)
			}
		}
		payload := r.Header.Get("X-Ziggy-Principal")
		supplied, err := base64.RawURLEncoding.DecodeString(r.Header.Get("X-Ziggy-Principal-Signature"))
		if err != nil {
			t.Errorf("decode Work signature: %v", err)
		}
		mac := hmac.New(sha256.New, key)
		_, _ = mac.Write([]byte(payload))
		if !hmac.Equal(supplied, mac.Sum(nil)) {
			t.Error("Work principal signature does not verify")
		}
		var principal struct {
			UserID      string `json:"user_id"`
			WorkspaceID string `json:"workspace_id"`
			ExpiresAt   int64  `json:"expires_at"`
		}
		decoded, err := base64.RawURLEncoding.DecodeString(payload)
		if err != nil || json.Unmarshal(decoded, &principal) != nil {
			t.Fatalf("Work principal payload = %q, error = %v", decoded, err)
		}
		if principal.UserID != "user-a" || principal.WorkspaceID != "workspace-a" {
			t.Errorf("Work principal = %+v", principal)
		}
		if principal.ExpiresAt <= time.Now().Unix() || principal.ExpiresAt > time.Now().Add(2*time.Minute).Unix() {
			t.Errorf("Work principal expiry = %d", principal.ExpiresAt)
		}
		w.Header().Set("Content-Type", "text/event-stream")
		_, _ = io.WriteString(w, "data: work-a\n\n")
	}))
	defer upstream.Close()
	target, err := url.Parse(upstream.URL)
	if err != nil {
		t.Fatal(err)
	}
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	workSigner, err := NewWorkSigner(key)
	if err != nil {
		t.Fatal(err)
	}
	workRouter := workCredentialRouter{routes: map[string]TenantRoute{
		"transport-a": {UserID: "user-a", WorkspaceID: "workspace-a"},
	}}
	var clerkCalls atomic.Int32
	authenticate := Middleware(func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			clerkCalls.Add(1)
			writeError(w, http.StatusInternalServerError, "Clerk must not run for Work")
		})
	})
	handler, err := New(Config{
		Authenticate:   authenticate,
		Proxy:          http.HandlerFunc(func(http.ResponseWriter, *http.Request) { t.Error("Nanobot fallback was used") }),
		TenantRouter:   workRouter,
		WorkProxy:      NewWorkProxy(target, logger, nil),
		WorkSigner:     workSigner,
		Readiness:      checkerFunc(func(context.Context) error { return nil }),
		Logger:         logger,
		MaxRequestBody: 1 << 20,
		Version:        "test",
	})
	if err != nil {
		t.Fatal(err)
	}
	request := httptest.NewRequest(http.MethodGet, "/api/work/runs/next?workspace_id=ignored", nil)
	request.Header.Set("Accept", "text/event-stream")
	request.Header.Set("Authorization", "Bearer transport-a")
	request.Header.Set("Cookie", "session=foreign")
	request.Header.Set("X-Ziggy-Principal", "spoofed")
	request.Header.Set("X-Ziggy-Forged", "spoofed")
	request.Header.Set("X-Ziggy-Subject", "spoofed")
	request.Header.Set("X-Ziggy-Email", "spoofed")
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, request)
	if response.Code != http.StatusOK || response.Body.String() != "data: work-a\n\n" {
		t.Fatalf("Work response = %d %q", response.Code, response.Body.String())
	}
	if got := response.Header().Get("Content-Type"); got != "text/event-stream" {
		t.Errorf("Work Content-Type = %q", got)
	}
	if requests.Load() != 1 || clerkCalls.Load() != 0 {
		t.Errorf("upstream requests = %d, Clerk calls = %d", requests.Load(), clerkCalls.Load())
	}
}

func TestWorkProxyKeepsTwoRememberedTenantsIsolated(t *testing.T) {
	key := []byte("01234567890123456789012345678901")
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var principal struct {
			UserID      string `json:"user_id"`
			WorkspaceID string `json:"workspace_id"`
		}
		payload, err := base64.RawURLEncoding.DecodeString(r.Header.Get("X-Ziggy-Principal"))
		if err != nil || json.Unmarshal(payload, &principal) != nil {
			t.Fatalf("invalid Work principal: %q", payload)
		}
		if r.Header.Get("Authorization") != "" || r.Header.Get("Cookie") != "" {
			t.Error("tenant transport credentials crossed the Work boundary")
		}
		_, _ = io.WriteString(w, principal.UserID+"/"+principal.WorkspaceID)
	}))
	defer upstream.Close()
	target, _ := url.Parse(upstream.URL)
	workSigner, _ := NewWorkSigner(key)
	handler, err := New(Config{
		Authenticate: Middleware(func(next http.Handler) http.Handler { return next }),
		Proxy:        http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(http.StatusTeapot) }),
		TenantRouter: workCredentialRouter{routes: map[string]TenantRoute{
			"transport-a": {UserID: "user-a", WorkspaceID: "workspace-a"},
			"transport-b": {UserID: "user-b", WorkspaceID: "workspace-b"},
		}},
		WorkProxy:      NewWorkProxy(target, slog.Default(), nil),
		WorkSigner:     workSigner,
		Readiness:      checkerFunc(func(context.Context) error { return nil }),
		Logger:         slog.Default(),
		MaxRequestBody: 1 << 20,
	})
	if err != nil {
		t.Fatal(err)
	}
	for _, test := range []struct {
		credential string
		want       string
	}{
		{credential: "transport-a", want: "user-a/workspace-a"},
		{credential: "transport-b", want: "user-b/workspace-b"},
	} {
		request := httptest.NewRequest(http.MethodGet, "/api/work/runs", nil)
		request.Header.Set("Authorization", "Bearer "+test.credential)
		request.Header.Set("Cookie", "tenant=spoofed")
		response := httptest.NewRecorder()
		handler.ServeHTTP(response, request)
		if response.Code != http.StatusOK || response.Body.String() != test.want {
			t.Errorf("credential %q response = %d %q", test.credential, response.Code, response.Body.String())
		}
	}
}

func TestWorkProxyRejectsMissingAndUnknownBearerWithoutOwnerFallback(t *testing.T) {
	var upstreamRequests atomic.Int32
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		upstreamRequests.Add(1)
		w.WriteHeader(http.StatusNoContent)
	}))
	defer upstream.Close()
	target, _ := url.Parse(upstream.URL)
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	signer, _ := NewWorkSigner([]byte("01234567890123456789012345678901"))
	handler, err := New(Config{
		Authenticate: Middleware(func(next http.Handler) http.Handler { return next }),
		Proxy:        http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(http.StatusTeapot) }),
		TenantRouter: workCredentialRouter{routes: map[string]TenantRoute{
			"known": {UserID: "user-a", WorkspaceID: "workspace-a"},
		}},
		WorkProxy:      NewWorkProxy(target, logger, nil),
		WorkSigner:     signer,
		Readiness:      checkerFunc(func(context.Context) error { return nil }),
		Logger:         logger,
		MaxRequestBody: 1 << 20,
	})
	if err != nil {
		t.Fatal(err)
	}
	for _, authorization := range []string{"", "Bearer unknown", "Basic known"} {
		request := httptest.NewRequest(http.MethodGet, "/api/work/runs", nil)
		if authorization != "" {
			request.Header.Set("Authorization", authorization)
		}
		response := httptest.NewRecorder()
		handler.ServeHTTP(response, request)
		if response.Code != http.StatusUnauthorized {
			t.Errorf("Authorization %q status = %d", authorization, response.Code)
		}
	}
	queryToken := httptest.NewRequest(http.MethodGet, "/api/work/runs?token=known", nil)
	queryResponse := httptest.NewRecorder()
	handler.ServeHTTP(queryResponse, queryToken)
	if queryResponse.Code != http.StatusUnauthorized {
		t.Errorf("query token status = %d, want 401", queryResponse.Code)
	}
	if upstreamRequests.Load() != 0 {
		t.Errorf("upstream requests = %d, want 0", upstreamRequests.Load())
	}
}

func TestWorkConfigurationRequiresTenantRouting(t *testing.T) {
	signer, _ := NewWorkSigner([]byte("01234567890123456789012345678901"))
	target, _ := url.Parse("http://127.0.0.1:8800")
	_, err := New(Config{
		Authenticate:   Middleware(func(next http.Handler) http.Handler { return next }),
		Proxy:          http.HandlerFunc(func(http.ResponseWriter, *http.Request) {}),
		WorkProxy:      NewWorkProxy(target, slog.Default(), nil),
		WorkSigner:     signer,
		Readiness:      checkerFunc(func(context.Context) error { return nil }),
		Logger:         slog.Default(),
		MaxRequestBody: 1 << 20,
	})
	if err == nil {
		t.Fatal("New() error = nil, want tenant routing error")
	}
}

func TestWorkAbsentPreservesLegacyNanobotFallback(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/work/legacy" || r.Header.Get("Authorization") != "Bearer nanobot-token" {
			t.Errorf("legacy Work fallback request = %s %q", r.URL.Path, r.Header.Get("Authorization"))
		}
		w.WriteHeader(http.StatusNoContent)
	}))
	defer upstream.Close()
	handler := newTestHandler(t, upstream.URL, principalMiddleware(identity.Principal{}))
	request := httptest.NewRequest(http.MethodGet, "/api/work/legacy", nil)
	request.Header.Set("Authorization", "Bearer nanobot-token")
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, request)
	if response.Code != http.StatusNoContent {
		t.Fatalf("legacy Work fallback status = %d", response.Code)
	}
}
