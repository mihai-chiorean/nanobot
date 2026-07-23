package main

import (
	"context"
	"errors"
	"log/slog"
	"net"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/mihai-chiorean/nanobot/services/ziggy-work/internal/config"
	"github.com/mihai-chiorean/nanobot/services/ziggy-work/internal/executor"
	"github.com/mihai-chiorean/nanobot/services/ziggy-work/internal/httpapi"
	"github.com/mihai-chiorean/nanobot/services/ziggy-work/internal/job"
	"github.com/mihai-chiorean/nanobot/services/ziggy-work/internal/model"
	"github.com/mihai-chiorean/nanobot/services/ziggy-work/internal/principal"
	"github.com/mihai-chiorean/nanobot/services/ziggy-work/internal/queue"
	"github.com/mihai-chiorean/nanobot/services/ziggy-work/internal/repository"
	"github.com/mihai-chiorean/nanobot/services/ziggy-work/internal/telemetry"
	"github.com/mihai-chiorean/nanobot/services/ziggy-work/internal/tenant"
)

var version = "dev"

func main() {
	if err := run(); err != nil {
		slog.Error("ziggy-work stopped", "failure_class", "startup_or_runtime")
		os.Exit(1)
	}
}

func run() error {
	cfg, err := config.Load()
	if err != nil {
		return err
	}
	if cfg.Version == "dev" {
		cfg.Version = version
	}
	logger := slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{Level: slog.LevelInfo}))
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	if cfg.DatabaseURL == "" {
		return errors.New("ZIGGY_WORK_DATABASE_URL or ZIGGY_WORK_DATABASE_URL_FILE is required")
	}
	poolConfig, err := pgxpool.ParseConfig(cfg.DatabaseURL)
	if err != nil {
		return err
	}
	poolConfig.MaxConns = int32(cfg.QueueWorkers*4 + 4)
	pool, err := pgxpool.NewWithConfig(ctx, poolConfig)
	if err != nil {
		return err
	}
	defer pool.Close()
	if err := pool.Ping(ctx); err != nil {
		return err
	}
	repo := repository.NewPostgres(pool)
	registry, err := tenant.Load(cfg.TenantManifest)
	if err != nil {
		return err
	}
	tel, err := telemetry.New(ctx, cfg.OTLPEndpoint, cfg.Version, logger)
	if err != nil {
		return err
	}
	defer func() {
		shutdown, cancel := context.WithTimeout(context.Background(), cfg.ShutdownTimeout)
		defer cancel()
		if err := tel.Shutdown(shutdown); err != nil {
			logger.Warn("telemetry shutdown failed", "failure_class", "telemetry")
		}
	}()
	runtime := executor.NewNanobot(repo, registry, cfg.ArtifactRoot, cfg.UpstreamTimeout, logger, tel)
	handler := func(ctx context.Context, item job.Job) error {
		tenantID := model.Tenant{UserID: item.UserID, WorkspaceID: item.WorkspaceID}
		task, err := repo.GetTask(ctx, tenantID, item.TaskID)
		if err != nil {
			return err
		}
		if task.Status.Terminal() {
			return nil
		}
		started := time.Now()
		if item.FollowUp {
			err = runtime.Message(ctx, task, item.Content, item.CommandID)
		} else {
			err = runtime.Execute(ctx, task, item.Content)
		}
		tel.Execution(ctx, "river", time.Since(started), err)
		if err != nil {
			tel.Retry(ctx, "river")
			if item.MaxAttempts > 0 && item.Attempt >= item.MaxAttempts {
				message := "work execution exhausted its retry budget"
				if _, _, statusErr := repo.UpdateStatus(ctx, tenantID, item.TaskID, model.Failed, &message, nil); statusErr != nil && !errors.Is(statusErr, model.ErrTerminal) {
					logger.Error("failed to finalize exhausted work task", "failure_class", "repository")
				}
			}
		}
		return err
	}
	workQueue, err := queue.NewRiver(pool, cfg.QueueName, cfg.QueueWorkers, handler)
	if err != nil {
		return err
	}
	if err := workQueue.Start(ctx); err != nil {
		return err
	}
	defer func() {
		shutdown, cancel := context.WithTimeout(context.Background(), cfg.ShutdownTimeout)
		defer cancel()
		if err := workQueue.Stop(shutdown); err != nil {
			logger.Warn("work queue shutdown failed", "failure_class", "queue")
		}
	}()
	if cfg.ReconcileInterval > 0 {
		go reconciliationLoop(ctx, runtime, cfg.ReconcileInterval, logger)
	}
	api, err := httpapi.New(httpapi.Config{Repository: repo, Queue: workQueue, Executor: runtime, Verifier: principal.NewVerifier(cfg.PrincipalKey), Telemetry: tel, Logger: logger, RequestTimeout: cfg.RequestTimeout, BodyLimit: cfg.BodyLimit, EventLimit: cfg.EventLimit, ArtifactRoot: cfg.ArtifactRoot, MaxStreams: cfg.StreamLimit, MaxStreamsPerTenant: cfg.TenantStreamLimit, Version: cfg.Version})
	if err != nil {
		return err
	}
	requestBase, cancelRequests := context.WithCancel(context.Background())
	defer cancelRequests()
	server := &http.Server{
		Addr:              cfg.ListenAddr,
		Handler:           api,
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       cfg.RequestTimeout,
		IdleTimeout:       60 * time.Second,
		BaseContext:       func(net.Listener) context.Context { return requestBase },
	}
	serverErr := make(chan error, 1)
	go func() {
		err := server.ListenAndServe()
		if errors.Is(err, http.ErrServerClosed) {
			err = nil
		}
		serverErr <- err
	}()
	select {
	case err := <-serverErr:
		return err
	case <-ctx.Done():
		cancelRequests()
		shutdown, cancel := context.WithTimeout(context.Background(), cfg.ShutdownTimeout)
		defer cancel()
		return server.Shutdown(shutdown)
	}
}

func reconciliationLoop(ctx context.Context, runtime *executor.Nanobot, interval time.Duration, logger *slog.Logger) {
	if err := runtime.Reconcile(ctx); err != nil && !errors.Is(err, context.Canceled) {
		logger.Warn("initial runtime reconciliation failed", "failure_class", "reconciliation")
	}
	ticker := time.NewTicker(interval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			if err := runtime.Reconcile(ctx); err != nil {
				logger.Warn("runtime reconciliation failed", "failure_class", "reconciliation")
			}
		}
	}
}
