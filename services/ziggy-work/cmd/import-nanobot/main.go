package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"os"

	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/mihai-chiorean/nanobot/services/ziggy-work/internal/config"
	"github.com/mihai-chiorean/nanobot/services/ziggy-work/internal/importer"
	"github.com/mihai-chiorean/nanobot/services/ziggy-work/internal/model"
	"github.com/mihai-chiorean/nanobot/services/ziggy-work/internal/repository"
)

type options struct {
	input               string
	userID              string
	workspaceID         string
	artifactRoot        string
	artifactDestination string
	artifactMap         string
}

func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

func run() error {
	opts := options{}
	flag.StringVar(&opts.input, "input", "", "bounded Nanobot JSON export")
	flag.StringVar(&opts.userID, "user-id", "", "explicit tenant user ID")
	flag.StringVar(&opts.workspaceID, "workspace-id", "", "explicit tenant workspace ID")
	flag.StringVar(&opts.artifactRoot, "artifact-root", "", "read-only source root for relative artifact paths")
	flag.StringVar(&opts.artifactDestination, "artifact-destination", "", "service artifact destination")
	flag.StringVar(&opts.artifactMap, "artifact-map", "", "JSON object mapping artifact IDs to relative source paths")
	flag.Parse()
	if opts.input == "" || opts.userID == "" || opts.workspaceID == "" {
		return errors.New("--input, --user-id, and --workspace-id are required")
	}

	cfg, err := config.Load()
	if err != nil {
		return err
	}
	if cfg.DatabaseURL == "" {
		return errors.New("database URL is required")
	}
	ctx := context.Background()
	poolConfig, err := pgxpool.ParseConfig(cfg.DatabaseURL)
	if err != nil {
		return fmt.Errorf("parse database URL: %w", err)
	}
	pool, err := pgxpool.NewWithConfig(ctx, poolConfig)
	if err != nil {
		return fmt.Errorf("open database pool: %w", err)
	}
	defer pool.Close()

	file, err := os.Open(opts.input)
	if err != nil {
		return fmt.Errorf("open export: %w", err)
	}
	defer file.Close()

	mappings := map[string]string{}
	if opts.artifactMap != "" {
		data, readErr := os.ReadFile(opts.artifactMap)
		if readErr != nil {
			return fmt.Errorf("read artifact map: %w", readErr)
		}
		if err := json.Unmarshal(data, &mappings); err != nil {
			return fmt.Errorf("parse artifact map: %w", err)
		}
	}

	result, err := importer.Import(ctx, repository.NewPostgres(pool), model.Tenant{
		UserID:      opts.userID,
		WorkspaceID: opts.workspaceID,
	}, file, importer.Options{
		ArtifactRoot:        opts.artifactRoot,
		ArtifactDestination: opts.artifactDestination,
		ArtifactMap:         mappings,
	})
	if err != nil {
		return fmt.Errorf("import Nanobot export: %w", err)
	}
	fmt.Printf("imported tasks=%d events=%d steps=%d artifacts=%d skipped=%d\n",
		result.Tasks, result.Events, result.Steps, result.Artifacts, result.Skipped)
	return nil
}
