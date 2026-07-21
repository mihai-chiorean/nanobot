package auth

import (
	"context"
	"crypto/rand"
	"crypto/rsa"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/clerk/clerk-sdk-go/v2"
	"github.com/go-jose/go-jose/v3"
	"github.com/go-jose/go-jose/v3/jwt"
	"github.com/mihai-chiorean/nanobot/services/ziggy-control/internal/identity"
)

type roundTripFunc func(*http.Request) (*http.Response, error)

func (roundTrip roundTripFunc) RoundTrip(request *http.Request) (*http.Response, error) {
	return roundTrip(request)
}

func TestNewVerifiesJWTAndAuthorizedParty(t *testing.T) {
	privateKey, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	keyID := "ziggy-auth-test"
	publicJWK := jose.JSONWebKey{
		Key:       &privateKey.PublicKey,
		KeyID:     keyID,
		Algorithm: string(jose.RS256),
		Use:       "sig",
	}
	jwksBody, err := json.Marshal(map[string]any{"keys": []jose.JSONWebKey{publicJWK}})
	if err != nil {
		t.Fatal(err)
	}
	client := &http.Client{Transport: roundTripFunc(func(request *http.Request) (*http.Response, error) {
		if !strings.HasSuffix(request.URL.Path, "/jwks") {
			t.Fatalf("unexpected Clerk API request: %s", request.URL)
		}
		return &http.Response{
			StatusCode: http.StatusOK,
			Header:     http.Header{"Content-Type": []string{"application/json"}},
			Body:       io.NopCloser(strings.NewReader(string(jwksBody))),
			Request:    request,
		}, nil
	})}

	middleware, err := New(Config{
		SecretKey:         "sk_test_unit",
		AuthorizedParties: []string{"https://chat.example.com"},
		HTTPClient:        client,
	})
	if err != nil {
		t.Fatalf("New() error = %v", err)
	}
	next := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		principal, ok := identity.FromContext(r.Context())
		if !ok {
			t.Fatal("principal missing")
		}
		if principal.Subject != "user_123" || principal.Email != "owner@example.com" {
			t.Errorf("principal = %+v", principal)
		}
		w.WriteHeader(http.StatusNoContent)
	})
	handler := middleware(next)

	validToken := signToken(t, privateKey, keyID, "https://chat.example.com")
	request := httptest.NewRequest(http.MethodGet, "/auth/bootstrap", nil)
	request.Header.Set("Authorization", "Bearer "+validToken)
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, request)
	if response.Code != http.StatusNoContent {
		t.Errorf("valid token status = %d, body = %s", response.Code, response.Body.String())
	}

	wrongPartyToken := signToken(t, privateKey, keyID, "https://other.example.com")
	request = httptest.NewRequest(http.MethodGet, "/auth/bootstrap", nil)
	request.Header.Set("Authorization", "Bearer "+wrongPartyToken)
	response = httptest.NewRecorder()
	handler.ServeHTTP(response, request)
	if response.Code != http.StatusUnauthorized {
		t.Errorf("wrong party status = %d, want %d", response.Code, http.StatusUnauthorized)
	}
}

func signToken(t *testing.T, privateKey *rsa.PrivateKey, keyID, authorizedParty string) string {
	t.Helper()
	signer, err := jose.NewSigner(
		jose.SigningKey{Algorithm: jose.RS256, Key: privateKey},
		(&jose.SignerOptions{}).WithType("JWT").WithHeader("kid", keyID),
	)
	if err != nil {
		t.Fatal(err)
	}
	now := time.Now()
	token, err := jwt.Signed(signer).Claims(map[string]any{
		"iss":   "https://clerk.unit-test",
		"sub":   "user_123",
		"iat":   now.Unix(),
		"nbf":   now.Add(-time.Minute).Unix(),
		"exp":   now.Add(time.Minute).Unix(),
		"azp":   authorizedParty,
		"email": "Owner@Example.com",
	}).CompactSerialize()
	if err != nil {
		t.Fatal(err)
	}
	return token
}

func TestWithPrincipalUsesSignedEmailClaim(t *testing.T) {
	resolverCalled := false
	resolver := func(context.Context, string) (string, error) {
		resolverCalled = true
		return "", nil
	}

	next := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		principal, ok := identity.FromContext(r.Context())
		if !ok {
			t.Fatal("principal missing from context")
		}
		if principal.Subject != "user_123" || principal.Email != "owner@example.com" {
			t.Errorf("principal = %+v", principal)
		}
		w.WriteHeader(http.StatusNoContent)
	})
	handler := withPrincipal(next, resolver)

	claims := &clerk.SessionClaims{
		RegisteredClaims: clerk.RegisteredClaims{Subject: "user_123"},
		Custom:           &customClaims{Email: "Owner@Example.com"},
	}
	request := httptest.NewRequest(http.MethodGet, "/", nil)
	request = request.WithContext(clerk.ContextWithSessionClaims(request.Context(), claims))
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, request)

	if response.Code != http.StatusNoContent {
		t.Errorf("status = %d, want %d", response.Code, http.StatusNoContent)
	}
	if resolverCalled {
		t.Error("email resolver called despite signed email claim")
	}
}

func TestWithPrincipalFallsBackToUserLookup(t *testing.T) {
	resolver := func(_ context.Context, subject string) (string, error) {
		if subject != "user_123" {
			t.Fatalf("subject = %q", subject)
		}
		return "Owner@Example.com", nil
	}
	next := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		principal, _ := identity.FromContext(r.Context())
		if principal.Email != "owner@example.com" {
			t.Errorf("email = %q", principal.Email)
		}
		w.WriteHeader(http.StatusNoContent)
	})
	handler := withPrincipal(next, resolver)

	request := httptest.NewRequest(http.MethodGet, "/", nil)
	request = request.WithContext(clerk.ContextWithSessionClaims(request.Context(), &clerk.SessionClaims{
		RegisteredClaims: clerk.RegisteredClaims{Subject: "user_123"},
	}))
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, request)

	if response.Code != http.StatusNoContent {
		t.Errorf("status = %d, want %d", response.Code, http.StatusNoContent)
	}
}

func TestWithPrincipalRejectsMissingClaims(t *testing.T) {
	handler := withPrincipal(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {
		t.Fatal("next handler should not run")
	}), func(context.Context, string) (string, error) { return "", nil })

	response := httptest.NewRecorder()
	handler.ServeHTTP(response, httptest.NewRequest(http.MethodGet, "/", nil))
	if response.Code != http.StatusUnauthorized {
		t.Errorf("status = %d, want %d", response.Code, http.StatusUnauthorized)
	}
}

func TestPrimaryEmailRequiresVerifiedPrimaryAddress(t *testing.T) {
	primaryID := "email_1"
	account := &clerk.User{
		PrimaryEmailAddressID: &primaryID,
		EmailAddresses: []*clerk.EmailAddress{{
			ID:           primaryID,
			EmailAddress: "owner@example.com",
			Verification: &clerk.Verification{Status: "verified"},
		}},
	}

	email, err := primaryEmail(account)
	if err != nil {
		t.Fatalf("primaryEmail() error = %v", err)
	}
	if email != "owner@example.com" {
		t.Errorf("email = %q", email)
	}

	account.EmailAddresses[0].Verification.Status = "unverified"
	if _, err := primaryEmail(account); err == nil {
		t.Fatal("primaryEmail() accepted an unverified address")
	}
}
