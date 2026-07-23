package httpapi

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"
	"time"

	"github.com/mihai-chiorean/nanobot/services/ziggy-control/internal/identity"
)

type connectorTenantRouter struct {
	route             TenantRoute
	runtimeCredential string
}

func (router connectorTenantRouter) ResolvePrincipal(context.Context, identity.Principal) (TenantRoute, error) {
	return router.route, nil
}

func (router connectorTenantRouter) ResolveCredential(string) (TenantRoute, bool) {
	return TenantRoute{}, false
}

func (router connectorTenantRouter) ResolveRuntimeCredential(credential string) (TenantRoute, bool) {
	return router.route, credential == router.runtimeCredential && credential != ""
}

func (router connectorTenantRouter) RememberCredentials(TenantRoute, []string, time.Duration) error {
	return nil
}

func TestRuntimeConnectorMCPUsesRuntimeCapabilityAndDoesNotForwardIt(t *testing.T) {
	key := []byte("01234567890123456789012345678901")
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/mcp" {
			t.Errorf("connector path = %q", r.URL.Path)
		}
		if r.Header.Get("Authorization") != "" {
			t.Error("runtime capability reached connector service")
		}
		payload, err := base64.RawURLEncoding.DecodeString(r.Header.Get("X-Ziggy-Principal"))
		if err != nil || !containsAll(string(payload), "usr_tester", "ws_tester") {
			t.Errorf("connector principal = %q, error = %v", payload, err)
		}
		w.WriteHeader(http.StatusNoContent)
	}))
	defer upstream.Close()
	target, _ := url.Parse(upstream.URL)
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	signer, _ := NewConnectorSigner(key)
	route := TenantRoute{UserID: "usr_tester", WorkspaceID: "ws_tester", Proxy: http.HandlerFunc(func(http.ResponseWriter, *http.Request) {})}
	handler, err := New(Config{
		Authenticate:    principalMiddleware(identity.Principal{}),
		Proxy:           route.Proxy,
		TenantRouter:    connectorTenantRouter{route: route, runtimeCredential: "runtime-secret"},
		ConnectorProxy:  NewConnectorReverseProxy(target, logger, nil),
		ConnectorSigner: signer,
		Readiness:       checkerFunc(func(context.Context) error { return nil }),
		Logger:          logger,
		OwnerEmail:      "owner@example.com",
		MaxRequestBody:  1 << 20,
	})
	if err != nil {
		t.Fatal(err)
	}

	request := httptest.NewRequest(http.MethodPost, "/runtime/connectors/mcp", strings.NewReader("{}"))
	request.Header.Set("Authorization", "Bearer runtime-secret")
	request.RemoteAddr = "127.0.0.1:42000"
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, request)
	if response.Code != http.StatusNoContent {
		t.Fatalf("status = %d, body = %s", response.Code, response.Body.String())
	}

	rejected := httptest.NewRecorder()
	badRequest := httptest.NewRequest(http.MethodPost, "/runtime/connectors/mcp", strings.NewReader("{}"))
	badRequest.Header.Set("Authorization", "Bearer wrong-secret")
	badRequest.RemoteAddr = "127.0.0.1:42001"
	handler.ServeHTTP(rejected, badRequest)
	if rejected.Code != http.StatusUnauthorized {
		t.Fatalf("rejected status = %d", rejected.Code)
	}
}

func TestRuntimeConnectorMCPIsNotReachableThroughPublicProxyHeaders(t *testing.T) {
	route := TenantRoute{UserID: "usr_tester", WorkspaceID: "ws_tester"}
	api := &API{
		tenantRouter:    connectorTenantRouter{route: route, runtimeCredential: "runtime-secret"},
		connectorProxy:  http.HandlerFunc(func(http.ResponseWriter, *http.Request) { t.Fatal("connector proxy called") }),
		connectorSigner: &ConnectorSigner{key: []byte("01234567890123456789012345678901"), now: time.Now},
	}
	request := httptest.NewRequest(http.MethodPost, "/runtime/connectors/mcp", strings.NewReader("{}"))
	request.RemoteAddr = "127.0.0.1:42000"
	request.Header.Set("Authorization", "Bearer runtime-secret")
	request.Header.Set("CF-Connecting-IP", "203.0.113.10")
	response := httptest.NewRecorder()
	api.runtimeConnectors(response, request)
	if response.Code != http.StatusNotFound {
		t.Fatalf("status = %d", response.Code)
	}
}

func (router connectorTenantRouter) Default() TenantRoute { return router.route }

func TestConnectorProxySignsServerResolvedTenantAndStripsPrefix(t *testing.T) {
	key := []byte("01234567890123456789012345678901")
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/accounts" {
			t.Errorf("connector path = %q", r.URL.Path)
		}
		payload := r.Header.Get("X-Ziggy-Principal")
		supplied, err := base64.RawURLEncoding.DecodeString(r.Header.Get("X-Ziggy-Principal-Signature"))
		if err != nil {
			t.Errorf("decode signature: %v", err)
		}
		mac := hmac.New(sha256.New, key)
		_, _ = mac.Write([]byte(payload))
		if !hmac.Equal(supplied, mac.Sum(nil)) {
			t.Error("connector principal signature does not verify")
		}
		decoded, err := base64.RawURLEncoding.DecodeString(payload)
		if err != nil || !containsAll(string(decoded), "usr_tester", "ws_tester") {
			t.Errorf("connector principal = %q, error = %v", decoded, err)
		}
		w.WriteHeader(http.StatusNoContent)
	}))
	defer upstream.Close()
	target, err := url.Parse(upstream.URL)
	if err != nil {
		t.Fatal(err)
	}
	signer, err := NewConnectorSigner(key)
	if err != nil {
		t.Fatal(err)
	}
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	route := TenantRoute{UserID: "usr_tester", WorkspaceID: "ws_tester", Proxy: http.HandlerFunc(func(http.ResponseWriter, *http.Request) {})}
	handler, err := New(Config{
		Authenticate:    principalMiddleware(identity.Principal{Subject: "clerk_tester", Email: "tester@example.com"}),
		Proxy:           route.Proxy,
		TenantRouter:    connectorTenantRouter{route: route},
		ConnectorProxy:  NewConnectorReverseProxy(target, logger, nil),
		ConnectorSigner: signer,
		Readiness:       checkerFunc(func(context.Context) error { return nil }),
		Logger:          logger,
		OwnerEmail:      "owner@example.com",
		OwnerSubject:    "clerk_owner",
		MaxRequestBody:  1 << 20,
		Version:         "test",
	})
	if err != nil {
		t.Fatal(err)
	}
	request := httptest.NewRequest(http.MethodGet, "/connectors/accounts", nil)
	request.Header.Set("X-Ziggy-Principal", "spoofed")
	request.Header.Set("X-Ziggy-Principal-Signature", "spoofed")
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, request)
	if response.Code != http.StatusNoContent {
		t.Fatalf("status = %d, body = %s", response.Code, response.Body.String())
	}
}

func TestRuntimeConnectorAdmissionIsTenantScoped(t *testing.T) {
	route := TenantRoute{UserID: "usr_tester", WorkspaceID: "ws_tester"}
	api := &API{tenantRouter: connectorTenantRouter{route: route, runtimeCredential: "runtime-secret"}}
	request := httptest.NewRequest(http.MethodPost, "/runtime/connectors/mcp", strings.NewReader("{}"))
	request.Header.Set("Authorization", "Bearer runtime-secret")
	if tenant := api.admissionTenant(request); tenant != route.UserID {
		t.Fatalf("admission tenant = %q", tenant)
	}
}

func TestConnectorCallbackNeverReceivesPrincipalHeaders(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/oauth/google/callback" {
			t.Errorf("callback path = %q", r.URL.Path)
		}
		if r.Header.Get("X-Ziggy-Principal") != "" || r.Header.Get("X-Ziggy-Principal-Signature") != "" {
			t.Error("callback received a principal header")
		}
		w.WriteHeader(http.StatusNoContent)
	}))
	defer upstream.Close()
	target, _ := url.Parse(upstream.URL)
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	signer, _ := NewConnectorSigner([]byte("01234567890123456789012345678901"))
	route := TenantRoute{UserID: "usr_owner", WorkspaceID: "ws_owner", Proxy: http.HandlerFunc(func(http.ResponseWriter, *http.Request) {})}
	handler, err := New(Config{
		Authenticate:    principalMiddleware(identity.Principal{}),
		Proxy:           route.Proxy,
		TenantRouter:    connectorTenantRouter{route: route},
		ConnectorProxy:  NewConnectorReverseProxy(target, logger, nil),
		ConnectorSigner: signer,
		Readiness:       checkerFunc(func(context.Context) error { return nil }),
		Logger:          logger,
		OwnerEmail:      "owner@example.com",
		MaxRequestBody:  1 << 20,
	})
	if err != nil {
		t.Fatal(err)
	}
	request := httptest.NewRequest(http.MethodGet, "/connectors/oauth/google/callback?code=a&state=b", nil)
	request.Header.Set("X-Ziggy-Principal", "spoofed")
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, request)
	if response.Code != http.StatusNoContent {
		t.Fatalf("status = %d", response.Code)
	}
}

func containsAll(value string, needles ...string) bool {
	for _, needle := range needles {
		if !strings.Contains(value, needle) {
			return false
		}
	}
	return true
}
