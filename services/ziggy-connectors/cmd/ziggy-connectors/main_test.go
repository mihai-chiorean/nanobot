package main

import (
	"log/slog"
	"testing"
	"time"

	"github.com/mihai-chiorean/nanobot/services/ziggy-connectors/internal/config"
	"github.com/mihai-chiorean/nanobot/services/ziggy-connectors/internal/crypto"
	"github.com/mihai-chiorean/nanobot/services/ziggy-connectors/internal/runtimeauth"
	"github.com/mihai-chiorean/nanobot/services/ziggy-connectors/internal/store"
)

func TestNewAPIWiresLoadedConfiguration(t *testing.T) {
	key := []byte("01234567890123456789012345678901")
	cipher, err := crypto.NewAESGCM(key)
	if err != nil {
		t.Fatal(err)
	}
	repository := store.NewMemoryRepository()
	cfg := config.Config{
		Environment:            "production",
		Version:                "test",
		GoogleClientID:         "client.apps.googleusercontent.com",
		GoogleClientSecret:     "secret",
		GoogleRedirectURI:      "https://chat.example.test/connectors/oauth/google/callback",
		GoogleAuthURL:          "https://accounts.google.com/o/oauth2/v2/auth",
		GoogleTokenURL:         "https://oauth2.googleapis.com/token",
		GoogleUserInfoURL:      "https://openidconnect.googleapis.com/v1/userinfo",
		GoogleProfileURL:       "https://gmail.googleapis.com/gmail/v1/users/me/profile",
		GoogleScopes:           []string{"openid", "email", "https://www.googleapis.com/auth/gmail.readonly"},
		StateTTL:               10 * time.Minute,
		TrustKey:               key,
		ClientCredentialPepper: key,
		MCPAccessSigningKey:    key,
		OAuthIssuerURL:         "https://127.0.0.1:8790",
		MCPResourceURL:         "https://127.0.0.1:8790/mcp",
	}

	tokens, err := runtimeauth.New(key, cfg.OAuthIssuerURL, cfg.MCPResourceURL)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := newAPI(cfg, repository, repository, repository, slog.Default(), cipher, crypto.NewStateSigner(key), tokens); err != nil {
		t.Fatalf("newAPI() error = %v", err)
	}
}

func TestErrorClassPreservesStartupStage(t *testing.T) {
	err := classify("startup_api", contextCanceledError{})
	if got := errorClass(err); got != "startup_api" {
		t.Fatalf("errorClass() = %q", got)
	}
}

type contextCanceledError struct{}

func (contextCanceledError) Error() string { return "test" }
