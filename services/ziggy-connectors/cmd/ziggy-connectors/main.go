package main

import (
	"context"
	"database/sql"
	"errors"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	_ "github.com/jackc/pgx/v5/stdlib"
	"github.com/mihai-chiorean/nanobot/services/ziggy-connectors/internal/config"
	"github.com/mihai-chiorean/nanobot/services/ziggy-connectors/internal/crypto"
	"github.com/mihai-chiorean/nanobot/services/ziggy-connectors/internal/httpapi"
	"github.com/mihai-chiorean/nanobot/services/ziggy-connectors/internal/principal"
	"github.com/mihai-chiorean/nanobot/services/ziggy-connectors/internal/provider"
	"github.com/mihai-chiorean/nanobot/services/ziggy-connectors/internal/runtimeauth"
	"github.com/mihai-chiorean/nanobot/services/ziggy-connectors/internal/store"
)

func main() {
	if err := run(); err != nil {
		slog.Error("connector service stopped", "error_class", errorClass(err))
		os.Exit(1)
	}
}

func run() error {
	cfg, err := config.Load()
	if err != nil {
		return classify("startup_configuration", err)
	}
	logger := slog.New(slog.NewJSONHandler(os.Stderr, &slog.HandlerOptions{Level: slog.LevelInfo}))

	stateSigner := crypto.NewStateSigner(cfg.StateSigningKey)
	tokenCipher, err := crypto.NewAESGCM(cfg.TokenEncryptionKey)
	if err != nil {
		return classify("startup_crypto", err)
	}
	mcpAccessTokens, err := runtimeauth.New(cfg.MCPAccessSigningKey, cfg.OAuthIssuerURL, cfg.MCPResourceURL)
	if err != nil {
		return classify("startup_crypto", err)
	}
	memory := store.NewMemoryRepository()
	var accounts store.AccountRepository = memory
	var transactions store.OAuthTransactionRepository = memory
	var runtimeOAuthClients store.RuntimeOAuthClientRepository = memory
	if cfg.DatabaseURL != "" {
		db, err := sql.Open("pgx", cfg.DatabaseURL)
		if err != nil {
			return classify("startup_database", err)
		}
		defer db.Close()
		postgres, err := store.NewPostgresRepository(db)
		if err != nil {
			return classify("startup_database", err)
		}
		accounts = postgres
		transactions = postgres
		runtimeOAuthClients = postgres
	}
	api, err := newAPI(cfg, accounts, transactions, runtimeOAuthClients, logger, tokenCipher, stateSigner, mcpAccessTokens)
	if err != nil {
		return classify("startup_api", err)
	}
	server := &http.Server{Addr: cfg.ListenAddr, Handler: api, ReadHeaderTimeout: 10 * time.Second}
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	if err := httpapi.Serve(ctx, server, cfg.ShutdownTimeout); err != nil {
		return classify("runtime_http", err)
	}
	return nil
}

func newAPI(cfg config.Config, accounts store.AccountRepository, transactions store.OAuthTransactionRepository, runtimeOAuthClients store.RuntimeOAuthClientRepository, logger *slog.Logger, tokenCipher crypto.Cipher, stateSigner *crypto.StateSigner, mcpAccessTokens *runtimeauth.TokenManager) (http.Handler, error) {
	google := provider.NewGoogle(provider.GoogleConfig{
		ClientID:     cfg.GoogleClientID,
		ClientSecret: cfg.GoogleClientSecret,
		AuthURL:      cfg.GoogleAuthURL,
		TokenURL:     cfg.GoogleTokenURL,
		UserInfoURL:  cfg.GoogleUserInfoURL,
		ProfileURL:   cfg.GoogleProfileURL,
		GmailAPIURL:  cfg.GoogleGmailAPIURL,
	})
	return httpapi.New(httpapi.Config{
		Environment:            cfg.Environment,
		Version:                cfg.Version,
		GoogleRedirectURI:      cfg.GoogleRedirectURI,
		GoogleScopes:           cfg.GoogleScopes,
		StateTTL:               cfg.StateTTL,
		StateSigner:            stateSigner,
		TokenCipher:            tokenCipher,
		Accounts:               accounts,
		OAuthTransactions:      transactions,
		RuntimeOAuthClients:    runtimeOAuthClients,
		Google:                 google,
		PrincipalVerifier:      principal.NewVerifier(cfg.TrustKey),
		ClientCredentialPepper: cfg.ClientCredentialPepper,
		MCPAccessTokens:        mcpAccessTokens,
		OAuthIssuerURL:         cfg.OAuthIssuerURL,
		MCPResourceURL:         cfg.MCPResourceURL,
		Logger:                 logger,
	})
}

func errorClass(err error) string {
	var classified *classifiedError
	if errors.As(err, &classified) {
		return classified.class
	}
	if errors.Is(err, context.Canceled) {
		return "canceled"
	}
	return "startup_or_shutdown_failure"
}

type classifiedError struct {
	class string
	err   error
}

func (e *classifiedError) Error() string { return e.err.Error() }
func (e *classifiedError) Unwrap() error { return e.err }

func classify(class string, err error) error {
	return &classifiedError{class: class, err: err}
}
