package httpapi

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/mihai-chiorean/nanobot/services/ziggy-connectors/internal/crypto"
	"github.com/mihai-chiorean/nanobot/services/ziggy-connectors/internal/principal"
	"github.com/mihai-chiorean/nanobot/services/ziggy-connectors/internal/provider"
	"github.com/mihai-chiorean/nanobot/services/ziggy-connectors/internal/store"
)

func TestOAuthFlowIsTenantBoundAndOneUse(t *testing.T) {
	now := time.Now()
	key := []byte("01234567890123456789012345678901")
	cipher, err := crypto.NewAESGCM(key)
	if err != nil {
		t.Fatal(err)
	}
	repo := store.NewMemoryRepository()
	api, err := New(Config{
		Environment:       "test",
		Version:           "test",
		GoogleRedirectURI: "https://gateway.test/oauth/google/callback",
		GoogleScopes:      []string{"openid", "email", googleGmailReadonlyScope},
		StateTTL:          time.Minute,
		StateSigner:       crypto.NewStateSigner(key),
		TokenCipher:       cipher,
		Accounts:          repo,
		OAuthTransactions: repo,
		Google:            provider.Fake{Token: provider.Token{AccessToken: "access", RefreshToken: "refresh", Scopes: []string{"openid", "email", googleGmailReadonlyScope}}, Profile: provider.Profile{Provider: "google", Subject: "google-sub", Email: "a@example.test"}},
		PrincipalVerifier: principal.NewVerifier(key),
		Now:               func() time.Time { return now },
	})
	if err != nil {
		t.Fatal(err)
	}
	tenantA := principal.Principal{UserID: "user-a", WorkspaceID: "workspace-a", ExpiresAt: now.Add(time.Hour).Unix()}
	start := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodGet, "/oauth/google/start", nil)
	withPrincipal(request, tenantA, key)
	api.ServeHTTP(start, request)
	if start.Code != http.StatusOK {
		t.Fatalf("start status = %d, body = %s", start.Code, start.Body)
	}
	var startBody map[string]string
	if err := json.Unmarshal(start.Body.Bytes(), &startBody); err != nil {
		t.Fatal(err)
	}
	if got := queryValue(startBody["authorization_url"], "redirect_uri"); got != "https://gateway.test/oauth/google/callback" {
		t.Fatalf("redirect URI = %q", got)
	}
	if got := queryValue(startBody["authorization_url"], "code_challenge_method"); got != "S256" {
		t.Fatalf("PKCE method = %q", got)
	}
	if queryValue(startBody["authorization_url"], "code_challenge") == "" {
		t.Fatal("missing PKCE challenge")
	}
	state := queryValue(startBody["authorization_url"], "state")
	callback := httptest.NewRequest(http.MethodGet, "/oauth/google/callback?code=code&state="+state, nil)
	callbackResponse := httptest.NewRecorder()
	api.ServeHTTP(callbackResponse, callback)
	if callbackResponse.Code != http.StatusOK {
		t.Fatalf("callback status = %d, body = %s", callbackResponse.Code, callbackResponse.Body)
	}
	replay := httptest.NewRecorder()
	api.ServeHTTP(replay, callback)
	if replay.Code != http.StatusConflict {
		t.Fatalf("replay status = %d, body = %s", replay.Code, replay.Body)
	}
	foreign := httptest.NewRecorder()
	foreignRequest := httptest.NewRequest(http.MethodGet, "/accounts", nil)
	withPrincipal(foreignRequest, principal.Principal{UserID: "user-b", WorkspaceID: "workspace-b", ExpiresAt: now.Add(time.Hour).Unix()}, key)
	api.ServeHTTP(foreign, foreignRequest)
	if foreign.Code != http.StatusOK || strings.Contains(foreign.Body.String(), "a@example.test") {
		t.Fatalf("foreign account response exposed tenant A: %s", foreign.Body)
	}
}

func TestOAuthRejectsPartialGrantWithoutPersistingAccount(t *testing.T) {
	now := time.Now()
	key := []byte("01234567890123456789012345678901")
	cipher, err := crypto.NewAESGCM(key)
	if err != nil {
		t.Fatal(err)
	}
	repo := store.NewMemoryRepository()
	api, err := New(Config{
		GoogleRedirectURI: "https://gateway.test/oauth/google/callback",
		GoogleScopes:      []string{"openid", "email", googleGmailReadonlyScope},
		StateTTL:          time.Minute,
		StateSigner:       crypto.NewStateSigner(key),
		TokenCipher:       cipher,
		Accounts:          repo,
		OAuthTransactions: repo,
		Google: provider.Fake{
			Token:   provider.Token{AccessToken: "access", RefreshToken: "refresh", Scopes: []string{"openid", "email"}},
			Profile: provider.Profile{Provider: "google", Subject: "google-sub", Email: "a@example.test"},
		},
		PrincipalVerifier: principal.NewVerifier(key),
		Now:               func() time.Time { return now },
	})
	if err != nil {
		t.Fatal(err)
	}
	tenant := principal.Principal{UserID: "user-a", WorkspaceID: "workspace-a", ExpiresAt: now.Add(time.Hour).Unix()}
	start := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodGet, "/oauth/google/start", nil)
	withPrincipal(request, tenant, key)
	api.ServeHTTP(start, request)
	if start.Code != http.StatusOK {
		t.Fatalf("start status = %d, body = %s", start.Code, start.Body)
	}
	var startBody map[string]string
	if err := json.Unmarshal(start.Body.Bytes(), &startBody); err != nil {
		t.Fatal(err)
	}
	state := queryValue(startBody["authorization_url"], "state")
	callback := httptest.NewRecorder()
	api.ServeHTTP(callback, httptest.NewRequest(http.MethodGet, "/oauth/google/callback?code=code&state="+state, nil))
	if callback.Code != http.StatusBadRequest || !strings.Contains(callback.Body.String(), "Gmail read permission") {
		t.Fatalf("callback status = %d, body = %s", callback.Code, callback.Body)
	}
	accounts, err := repo.ListAccounts(context.Background(), store.Tenant{UserID: tenant.UserID, WorkspaceID: tenant.WorkspaceID})
	if err != nil {
		t.Fatal(err)
	}
	if len(accounts) != 0 {
		t.Fatalf("stored %d accounts after partial grant", len(accounts))
	}
}

func TestOAuthRejectsTamperedAndExpiredState(t *testing.T) {
	now := time.Now()
	key := []byte("01234567890123456789012345678901")
	cipher, _ := crypto.NewAESGCM(key)
	repo := store.NewMemoryRepository()
	api, _ := New(Config{GoogleRedirectURI: "https://gateway.test/callback", GoogleScopes: []string{"scope"}, StateTTL: time.Minute, StateSigner: crypto.NewStateSigner(key), TokenCipher: cipher, Accounts: repo, OAuthTransactions: repo, Google: provider.Fake{Token: provider.Token{AccessToken: "a", RefreshToken: "r"}, Profile: provider.Profile{Provider: "google", Subject: "s", Email: "e@example.test"}}, PrincipalVerifier: principal.NewVerifier(key), Now: func() time.Time { return now }})
	start := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodGet, "/oauth/google/start", nil)
	withPrincipal(request, principal.Principal{UserID: "u", WorkspaceID: "w", ExpiresAt: now.Add(time.Hour).Unix()}, key)
	api.ServeHTTP(start, request)
	var body map[string]string
	_ = json.Unmarshal(start.Body.Bytes(), &body)
	state := queryValue(body["authorization_url"], "state")
	tampered := httptest.NewRecorder()
	api.ServeHTTP(tampered, httptest.NewRequest(http.MethodGet, "/oauth/google/callback?code=c&state="+state+"x", nil))
	if tampered.Code != http.StatusBadRequest {
		t.Fatalf("tampered status = %d", tampered.Code)
	}
	now = now.Add(2 * time.Minute)
	expired := httptest.NewRecorder()
	api.ServeHTTP(expired, httptest.NewRequest(http.MethodGet, "/oauth/google/callback?code=c&state="+state, nil))
	if expired.Code != http.StatusBadRequest {
		t.Fatalf("expired status = %d", expired.Code)
	}
}

func TestServeStopsOnContextCancellation(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	server := &http.Server{Addr: "127.0.0.1:0", Handler: http.NewServeMux()}
	if err := Serve(ctx, server, time.Second); err != nil {
		t.Fatal(err)
	}
}

func TestProtectedRouteHasNoAuthBypass(t *testing.T) {
	key := []byte("01234567890123456789012345678901")
	cipher, _ := crypto.NewAESGCM(key)
	repo := store.NewMemoryRepository()
	api, err := New(Config{GoogleRedirectURI: "https://gateway.test/callback", GoogleScopes: []string{"scope"}, StateTTL: time.Minute, StateSigner: crypto.NewStateSigner(key), TokenCipher: cipher, Accounts: repo, OAuthTransactions: repo, Google: provider.Fake{}, PrincipalVerifier: principal.NewVerifier(key)})
	if err != nil {
		t.Fatal(err)
	}
	response := httptest.NewRecorder()
	api.ServeHTTP(response, httptest.NewRequest(http.MethodGet, "/accounts", nil))
	if response.Code != http.StatusUnauthorized {
		t.Fatalf("unauthenticated status = %d", response.Code)
	}
}

func withPrincipal(request *http.Request, p principal.Principal, key []byte) {
	payload, _ := json.Marshal(p)
	encoded := base64.RawURLEncoding.EncodeToString(payload)
	mac := hmac.New(sha256.New, key)
	_, _ = mac.Write([]byte(encoded))
	request.Header.Set(principal.HeaderPayload, encoded)
	request.Header.Set(principal.HeaderSignature, base64.RawURLEncoding.EncodeToString(mac.Sum(nil)))
}

func queryValue(raw, name string) string {
	request := httptest.NewRequest(http.MethodGet, raw, nil)
	return request.URL.Query().Get(name)
}
