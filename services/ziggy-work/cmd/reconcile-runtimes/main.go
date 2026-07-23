package main

import (
	"context"
	"errors"
	"log/slog"
	"os"

	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/mihai-chiorean/nanobot/services/ziggy-work/internal/config"
	"github.com/mihai-chiorean/nanobot/services/ziggy-work/internal/executor"
	"github.com/mihai-chiorean/nanobot/services/ziggy-work/internal/repository"
	"github.com/mihai-chiorean/nanobot/services/ziggy-work/internal/telemetry"
	"github.com/mihai-chiorean/nanobot/services/ziggy-work/internal/tenant"
)

func main() {
	if err := run(); err != nil {
		slog.Error("runtime reconciliation failed", "failure_class", "reconciliation")
		os.Exit(1)
	}
}

func run() error {
	cfg, err := config.Load()
	if err != nil {
		return err
	}
	if cfg.DatabaseURL == "" || cfg.TenantManifest == "" {
		return errors.New("database URL and tenant manifest are required")
	}

	ctx := context.Background()
	poolConfig, err := pgxpool.ParseConfig(cfg.DatabaseURL)
	if err != nil {
		return err
	}
	pool, err := pgxpool.NewWithConfig(ctx, poolConfig)
	if err != nil {
		return err
	}
	defer pool.Close()

	registry, err := tenant.Load(cfg.TenantManifest)
	if err != nil {
		return err
	}
	tel, err := telemetry.New(ctx, cfg.OTLPEndpoint, cfg.Version, slog.Default())
	if err != nil {
		return err
	}
	defer tel.Shutdown(ctx)
	runtime := executor.NewNanobot(
		repository.NewPostgres(pool),
		registry,
		cfg.ArtifactRoot,
		cfg.UpstreamTimeout,
		slog.Default(),
		tel,
	)
	return runtime.Reconcile(ctx)
}
