package main

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"sync/atomic"
	"syscall"
	"time"

	_ "github.com/jackc/pgx/v5/stdlib"
	"github.com/mihai-chiorean/nanobot/services/ziggy-control/internal/auth"
	"github.com/mihai-chiorean/nanobot/services/ziggy-control/internal/config"
	"github.com/mihai-chiorean/nanobot/services/ziggy-control/internal/httpapi"
	"github.com/mihai-chiorean/nanobot/services/ziggy-control/internal/lifecycle"
	"github.com/mihai-chiorean/nanobot/services/ziggy-control/internal/routing"
	"github.com/mihai-chiorean/nanobot/services/ziggy-control/internal/telemetry"
	"github.com/mihai-chiorean/nanobot/services/ziggy-control/internal/tenant"
)

var version = "dev"

const tenantCountRefreshInterval = 15 * time.Second

type atomicTenantCount struct {
	value atomic.Int64
}

type tenantCountReader interface {
	AdmissionTenantCount(context.Context) (int, error)
}

func newAtomicTenantCount(count int) *atomicTenantCount {
	source := &atomicTenantCount{}
	source.Store(count)
	return source
}

func (source *atomicTenantCount) CurrentTenantCount() int64 {
	count := source.value.Load()
	if count < 1 {
		return 1
	}
	return count
}

func (source *atomicTenantCount) Store(count int) {
	if count < 0 {
		count = 0
	}
	source.value.Store(int64(count))
}

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
	if cfg.TenantMode == "legacy" && cfg.OwnerSubject == "" {
		logger.Warn("single-tenant fallback subject is not pinned")
	}
	if cfg.DeploymentEnv != "production" && len(cfg.AuthorizedParties) == 0 {
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
	telemetryShutdownComplete := false
	defer func() {
		if telemetryShutdownComplete {
			return
		}
		shutdownCtx, cancel := context.WithTimeout(context.Background(), cfg.ShutdownTimeout)
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

	var tenantRouter httpapi.TenantRouter
	tenantCount := 1
	var tenantCountSource *atomicTenantCount
	var durableTenantStore tenantCountReader
	if cfg.TenantMode == "manifest" {
		registry, err := tenant.Load(cfg.TenantManifest, cfg.TenantBindings)
		if err != nil {
			return err
		}
		defaultUpstream, err := config.ParsePrivateUpstream(registry.Default().UpstreamURL)
		if err != nil {
			return fmt.Errorf("default tenant upstream: %w", err)
		}
		if defaultUpstream.String() != cfg.UpstreamURL.String() {
			return fmt.Errorf("default tenant upstream must match ZIGGY_UPSTREAM_URL")
		}
		tenantRouter, err = routing.New(registry, logger, observability)
		if err != nil {
			return err
		}
		tenantCount = len(registry.Allocations())
		logger.Info("tenant routing enabled", "tenant_count", len(registry.Allocations()))
	} else if cfg.TenantMode == "postgres" {
		db, err := sql.Open("pgx", cfg.TenantDatabaseURL)
		if err != nil {
			return fmt.Errorf("open tenant database: %w", err)
		}
		defer db.Close()
		db.SetMaxOpenConns(16)
		db.SetMaxIdleConns(4)
		store, err := lifecycle.NewStore(db)
		if err != nil {
			return err
		}
		startupCtx, cancel := context.WithTimeout(context.Background(), cfg.ReadinessTimeout)
		err = store.Ready(startupCtx)
		if err != nil {
			cancel()
			return fmt.Errorf("tenant lifecycle database unavailable: %w", err)
		}
		tenantCount, err = store.AdmissionTenantCount(startupCtx)
		cancel()
		if err != nil {
			return fmt.Errorf("tenant admission count unavailable: %w", err)
		}
		durableRouter, err := routing.NewDurable(store, logger, observability)
		if err != nil {
			return err
		}
		store.SetRevocationObserver(durableRouter.EvictTenant)
		tenantRouter = durableRouter
		tenantCountSource = newAtomicTenantCount(tenantCount)
		durableTenantStore = store
		logger.Info("durable tenant routing enabled", "tenant_count", tenantCount)
	}
	var connectorProxy http.Handler
	var connectorSigner *httpapi.ConnectorSigner
	if cfg.ConnectorsURL != nil {
		if tenantRouter == nil {
			return fmt.Errorf("connector proxy requires tenant routing")
		}
		connectorSigner, err = httpapi.NewConnectorSigner(cfg.ConnectorTrustKey)
		if err != nil {
			return err
		}
		connectorProxy = httpapi.NewConnectorReverseProxy(cfg.ConnectorsURL, logger, observability)
		logger.Info("connector routing enabled")
	}
	var workProxy http.Handler
	var workSigner *httpapi.WorkSigner
	if cfg.WorkURL != nil {
		if tenantRouter == nil {
			return fmt.Errorf("work proxy requires tenant routing")
		}
		workSigner, err = httpapi.NewWorkSigner(cfg.WorkTrustKey)
		if err != nil {
			return err
		}
		workProxy = httpapi.NewWorkProxy(cfg.WorkURL, logger, observability)
		logger.Info("work routing enabled")
	}

	readiness := httpapi.NewHTTPReadinessChecker(cfg.UpstreamURL, cfg.UpstreamReadyPath, cfg.ReadinessTimeout, cfg.ReadinessCacheTTL, observability)
	if cfg.UpstreamPreflight {
		preflightCtx, cancel := context.WithTimeout(context.Background(), cfg.ReadinessTimeout)
		err := readiness.Preflight(preflightCtx)
		cancel()
		if err != nil {
			return fmt.Errorf("upstream compatibility preflight failed: %w", err)
		}
	}

	handler, err := httpapi.New(httpapi.Config{
		Authenticate:      httpapi.Middleware(authenticate),
		Proxy:             httpapi.NewReverseProxy(cfg.UpstreamURL, logger, observability),
		TenantRouter:      tenantRouter,
		ConnectorProxy:    connectorProxy,
		ConnectorSigner:   connectorSigner,
		WorkProxy:         workProxy,
		WorkSigner:        workSigner,
		Readiness:         readiness,
		Logger:            logger,
		Telemetry:         observability,
		OwnerEmail:        cfg.OwnerEmail,
		OwnerSubject:      cfg.OwnerSubject,
		BlockedPaths:      cfg.BlockedPaths,
		MaxRequestBody:    cfg.MaxRequestBody,
		HTTPInFlight:      cfg.HTTPInFlight,
		SSEInFlight:       cfg.SSEInFlight,
		WebSocketInFlight: cfg.WebSocketInFlight,
		TenantCount:       tenantCount,
		TenantCountSource: tenantCountSource,
		Version:           version,
	})
	if err != nil {
		return err
	}

	server := &http.Server{
		Addr:              cfg.ListenAddr,
		Handler:           handler,
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       30 * time.Second,
		IdleTimeout:       90 * time.Second,
		MaxHeaderBytes:    1 << 20,
		// ReadTimeout only covers reading the request headers and body. SSE and
		// WebSocket response lifetimes remain unbounded because WriteTimeout is zero.
	}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	var tenantCountRefreshDone <-chan struct{}
	if durableTenantStore != nil && tenantCountSource != nil {
		tenantCountRefreshDone = startTenantCountRefresh(
			ctx,
			durableTenantStore,
			tenantCountSource,
			logger,
			tenantCountRefreshInterval,
		)
	}
	defer func() {
		stop()
		if tenantCountRefreshDone != nil {
			<-tenantCountRefreshDone
		}
	}()
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
	shutdownErr := server.Shutdown(shutdownCtx)
	var closeErr error
	if shutdownErr != nil {
		logger.Warn("graceful shutdown deadline reached", "error", shutdownErr)
		if closeErr = server.Close(); closeErr != nil {
			logger.Warn("server close failed", "error_class", "shutdown")
		}
	}
	if err := observability.Shutdown(shutdownCtx); err != nil {
		logger.Warn("OpenTelemetry shutdown incomplete", "error_class", "export")
	}
	telemetryShutdownComplete = true
	if err := <-serveErr; !errors.Is(err, http.ErrServerClosed) {
		return err
	}
	if closeErr != nil {
		return closeErr
	}
	if shutdownErr != nil {
		return shutdownErr
	}
	return nil
}

func startTenantCountRefresh(
	ctx context.Context,
	store tenantCountReader,
	source *atomicTenantCount,
	logger *slog.Logger,
	interval time.Duration,
) <-chan struct{} {
	done := make(chan struct{})
	go func() {
		defer close(done)
		ticker := time.NewTicker(interval)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				count, err := store.AdmissionTenantCount(ctx)
				if err != nil {
					logger.Warn("tenant admission count refresh failed", "error_class", "database")
					continue
				}
				source.Store(count)
			}
		}
	}()
	return done
}
