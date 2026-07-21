package main

import (
	"context"
	"errors"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/mihai-chiorean/nanobot/services/ziggy-control/internal/auth"
	"github.com/mihai-chiorean/nanobot/services/ziggy-control/internal/config"
	"github.com/mihai-chiorean/nanobot/services/ziggy-control/internal/httpapi"
)

var version = "dev"

func main() {
	if err := run(); err != nil {
		slog.Error("ziggy-control stopped", "error", err)
		os.Exit(1)
	}
}

func run() error {
	cfg, err := config.Load()
	if err != nil {
		return err
	}

	logger := slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{Level: cfg.LogLevel}))
	slog.SetDefault(logger)

	authenticate, err := auth.New(auth.Config{
		SecretKey:         cfg.ClerkSecretKey,
		AuthorizedParties: cfg.AuthorizedParties,
	})
	if err != nil {
		return err
	}

	handler, err := httpapi.New(httpapi.Config{
		Authenticate: httpapi.Middleware(authenticate),
		Proxy:        httpapi.NewReverseProxy(cfg.UpstreamURL, logger),
		Readiness:    httpapi.NewHTTPReadinessChecker(cfg.UpstreamURL, cfg.ReadinessTimeout),
		Logger:       logger,
		OwnerEmail:   cfg.OwnerEmail,
		OwnerSubject: cfg.OwnerSubject,
		BlockedPaths: cfg.BlockedPaths,
		Version:      version,
	})
	if err != nil {
		return err
	}

	server := &http.Server{
		Addr:              cfg.ListenAddr,
		Handler:           handler,
		ReadHeaderTimeout: 5 * time.Second,
		IdleTimeout:       90 * time.Second,
		MaxHeaderBytes:    1 << 20,
	}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	serveErr := make(chan error, 1)
	go func() {
		logger.Info("ziggy-control listening",
			"address", cfg.ListenAddr,
			"upstream", cfg.UpstreamURL.Redacted(),
			"version", version,
		)
		serveErr <- server.ListenAndServe()
	}()

	select {
	case err := <-serveErr:
		if errors.Is(err, http.ErrServerClosed) {
			return nil
		}
		return err
	case <-ctx.Done():
	}

	logger.Info("shutting down")
	shutdownCtx, cancel := context.WithTimeout(context.Background(), cfg.ShutdownTimeout)
	defer cancel()
	if err := server.Shutdown(shutdownCtx); err != nil {
		return err
	}
	if err := <-serveErr; !errors.Is(err, http.ErrServerClosed) {
		return err
	}
	return nil
}
