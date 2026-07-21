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

	logger := slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{Level: cfg.LogLevel})).With(
		"service", "ziggy-control",
		"version", version,
	)
	slog.SetDefault(logger)
	if cfg.OwnerSubject == "" {
		logger.Warn("owner subject is not pinned")
	}
	if len(cfg.AuthorizedParties) == 0 {
		logger.Warn("Clerk authorized-party validation is disabled")
	}

	authenticate, err := auth.New(auth.Config{
		SecretKey:         cfg.ClerkSecretKey,
		AuthorizedParties: cfg.AuthorizedParties,
	})
	if err != nil {
		return err
	}

	handler, err := httpapi.New(httpapi.Config{
		Authenticate:   httpapi.Middleware(authenticate),
		Proxy:          httpapi.NewReverseProxy(cfg.UpstreamURL, logger),
		Readiness:      httpapi.NewHTTPReadinessChecker(cfg.UpstreamURL, cfg.UpstreamReadyPath, cfg.ReadinessTimeout, cfg.ReadinessCacheTTL),
		Logger:         logger,
		OwnerEmail:     cfg.OwnerEmail,
		OwnerSubject:   cfg.OwnerSubject,
		BlockedPaths:   cfg.BlockedPaths,
		MaxRequestBody: cfg.MaxRequestBody,
		Version:        version,
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
		// WriteTimeout and ReadTimeout remain zero because this endpoint carries
		// long-lived WebSocket/SSE traffic. Request bodies are size-bounded in the handler.
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
		logger.Warn("graceful shutdown deadline reached", "error", err)
		if closeErr := server.Close(); closeErr != nil {
			return closeErr
		}
	}
	if err := <-serveErr; !errors.Is(err, http.ErrServerClosed) {
		return err
	}
	return nil
}
