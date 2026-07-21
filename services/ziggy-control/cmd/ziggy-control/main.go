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
	"github.com/mihai-chiorean/nanobot/services/ziggy-control/internal/telemetry"
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

	observability, err := telemetry.New(context.Background(), telemetry.Config{
		Endpoint:              cfg.OTelEndpoint,
		AuthorizationFile:     cfg.OTelAuthFile,
		ServiceVersion:        version,
		DeploymentEnvironment: cfg.DeploymentEnv,
		TraceSampleRatio:      cfg.OTelTraceSample,
	})
	if err != nil {
		logger.Warn("OpenTelemetry disabled", "error_class", "initialization")
		observability = telemetry.Noop()
	}
	if observability.Enabled() {
		logger.Info("OpenTelemetry enabled", "export", "loopback_otlp_http")
	}
	defer func() {
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 7*time.Second)
		defer cancel()
		if err := observability.Shutdown(shutdownCtx); err != nil {
			logger.Warn("OpenTelemetry shutdown incomplete", "error_class", "export")
		}
	}()

	authenticate, err := auth.New(auth.Config{
		SecretKey:         cfg.ClerkSecretKey,
		AuthorizedParties: cfg.AuthorizedParties,
		Telemetry:         observability,
	})
	if err != nil {
		return err
	}

	handler, err := httpapi.New(httpapi.Config{
		Authenticate:   httpapi.Middleware(authenticate),
		Proxy:          httpapi.NewReverseProxy(cfg.UpstreamURL, logger, observability),
		Readiness:      httpapi.NewHTTPReadinessChecker(cfg.UpstreamURL, cfg.UpstreamReadyPath, cfg.ReadinessTimeout, cfg.ReadinessCacheTTL, observability),
		Logger:         logger,
		Telemetry:      observability,
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
