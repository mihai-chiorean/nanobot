package main

import (
	"context"
	"database/sql"
	"errors"
	"flag"
	"fmt"
	"os"
	"strings"
	"time"

	_ "github.com/jackc/pgx/v5/stdlib"
	"github.com/mihai-chiorean/nanobot/services/ziggy-control/internal/lifecycle"
	"github.com/mihai-chiorean/nanobot/services/ziggy-control/internal/tenant"
)

func main() {
	if err := run(os.Args[1:], os.Stdout); err != nil {
		fmt.Fprintln(os.Stderr, "tenant manifest import failed")
		os.Exit(1)
	}
}

func run(args []string, output *os.File) error {
	flags := flag.NewFlagSet("import-tenant-manifest", flag.ContinueOnError)
	flags.SetOutput(os.Stderr)
	manifestPath := flags.String("manifest", "", "validated tenant manifest path")
	bindingsPath := flags.String("bindings", "", "tenant subject bindings path")
	databaseURLFile := flags.String("database-url-file", "", "PostgreSQL URL credential file")
	dryRun := flags.Bool("dry-run", false, "validate and roll back the import")
	if err := flags.Parse(args); err != nil {
		return errors.New("invalid command arguments")
	}
	if flags.NArg() != 0 || strings.TrimSpace(*manifestPath) == "" || strings.TrimSpace(*bindingsPath) == "" || strings.TrimSpace(*databaseURLFile) == "" {
		return errors.New("manifest, bindings, and database-url-file are required")
	}
	registry, err := tenant.Load(*manifestPath, *bindingsPath)
	if err != nil {
		return errors.New("tenant manifest validation failed")
	}
	contents, err := os.ReadFile(*databaseURLFile)
	if err != nil || strings.TrimSpace(string(contents)) == "" {
		return errors.New("tenant database credential is unavailable")
	}
	db, err := sql.Open("pgx", strings.TrimSpace(string(contents)))
	if err != nil {
		return errors.New("tenant database connection failed")
	}
	defer db.Close()
	db.SetMaxOpenConns(1)
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	store, err := lifecycle.NewStore(db)
	if err != nil {
		return errors.New("tenant import initialization failed")
	}
	if err := store.Ready(ctx); err != nil {
		return errors.New("tenant lifecycle migrations are unavailable")
	}
	result, err := store.ImportLegacyAllocations(ctx, registry.ImportAllocations(), *dryRun)
	if err != nil {
		return errors.New("tenant import rejected")
	}
	prefix := "imported"
	if *dryRun {
		prefix = "dry-run"
	}
	fmt.Fprintf(output, "%s tenants=%d users_created=%d users_existing=%d workspaces_created=%d workspaces_existing=%d runtimes_created=%d runtimes_existing=%d\n",
		prefix, result.Tenants, result.UsersCreated, result.UsersExisting, result.WorkspacesCreated, result.WorkspacesExisting, result.RuntimesCreated, result.RuntimesExisting)
	return nil
}
