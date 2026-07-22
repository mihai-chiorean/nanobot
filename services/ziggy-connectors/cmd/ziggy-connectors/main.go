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
		return err
	}
	logger := slog.New(slog.NewJSONHandler(os.Stderr, &slog.HandlerOptions{Level: slog.LevelInfo}))

	stateSigner := crypto.NewStateSigner(cfg.StateSigningKey)
	tokenCipher, err := crypto.NewAESGCM(cfg.TokenEncryptionKey)
	if err != nil {
		return err
	}
	var accounts store.AccountRepository = store.NewMemoryRepository()
	var transactions store.OAuthTransactionRepository = store.NewMemoryRepository()
	if cfg.DatabaseURL != "" {
		db, err := sql.Open("pgx", cfg.DatabaseURL)
		if err != nil {
			return err
		}
		defer db.Close()
		postgres, err := store.NewPostgresRepository(db)
		if err != nil {
			return err
		}
		accounts = postgres
		transactions = postgres
	}
	google := provider.NewGoogle(provider.GoogleConfig{
		ClientID:     cfg.GoogleClientID,
		ClientSecret: cfg.GoogleClientSecret,
		AuthURL:      cfg.GoogleAuthURL,
		TokenURL:     cfg.GoogleTokenURL,
		UserInfoURL:  cfg.GoogleUserInfoURL,
		ProfileURL:   cfg.GoogleProfileURL,
		HTTPClient:   http.DefaultClient,
	})
	api, err := httpapi.New(httpapi.Config{
		Environment:       cfg.Environment,
		Version:           cfg.Version,
		GoogleRedirectURI: cfg.GoogleRedirectURI,
		GoogleScopes:      cfg.GoogleScopes,
		StateSigner:       stateSigner,
		TokenCipher:       tokenCipher,
		Accounts:          accounts,
		OAuthTransactions: transactions,
		Google:            google,
		PrincipalVerifier: principal.NewVerifier(cfg.TrustKey),
		Logger:            logger,
	})
	if err != nil {
		return err
	}
	server := &http.Server{Addr: cfg.ListenAddr, Handler: api, ReadHeaderTimeout: 10 * time.Second}
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	return httpapi.Serve(ctx, server, cfg.ShutdownTimeout)
}

func errorClass(err error) string {
	if errors.Is(err, context.Canceled) {
		return "canceled"
	}
	return "startup_or_shutdown_failure"
}
