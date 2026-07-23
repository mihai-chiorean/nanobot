package main

import (
	"context"
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"database/sql"
	"encoding/base64"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"time"

	_ "github.com/jackc/pgx/v5/stdlib"
	"github.com/mihai-chiorean/nanobot/services/ziggy-connectors/internal/config"
	"github.com/mihai-chiorean/nanobot/services/ziggy-connectors/internal/store"
)

const defaultScopes = "gmail.status gmail.search gmail.read"

type options struct {
	databaseURLFile            string
	clientCredentialPepperFile string
	userID                     string
	workspaceID                string
	runtimeID                  string
	runtimeGeneration          int64
	clientID                   string
	scopes                     string
	secretFile                 string
}

func main() {
	if err := run(os.Args[1:], os.Stdout); err != nil {
		fmt.Fprintln(os.Stderr, "provision runtime OAuth client:", err)
		os.Exit(1)
	}
}

func run(args []string, output *os.File) error {
	options, err := parseOptions(args)
	if err != nil {
		return err
	}
	cfg, err := loadProvisioningConfig(options)
	if err != nil {
		return err
	}
	db, err := sql.Open("pgx", cfg.DatabaseURL)
	if err != nil {
		return err
	}
	defer db.Close()
	repository, err := store.NewPostgresRepository(db)
	if err != nil {
		return err
	}
	return provision(context.Background(), options, cfg, repository, output)
}

func provision(ctx context.Context, options options, cfg config.ProvisioningConfig, repository store.RuntimeOAuthClientRepository, output io.Writer) error {
	tenant := store.Tenant{UserID: options.userID, WorkspaceID: options.workspaceID}
	existing, lookupErr := repository.GetRuntimeOAuthClientForRuntime(ctx, tenant, options.runtimeID, options.runtimeGeneration)
	hasExisting := lookupErr == nil
	if lookupErr != nil && !errors.Is(lookupErr, store.ErrNotFound) {
		return lookupErr
	}
	clientID := options.clientID
	if clientID == "" && hasExisting {
		clientID = existing.ClientID
	}
	if clientID == "" {
		generatedID, err := randomValue(18)
		if err != nil {
			return err
		}
		clientID = "zrc_" + generatedID
	}
	secret, err := randomValue(32)
	if err != nil {
		return err
	}
	stagedSecret, err := stageSecretFile(options.secretFile, secret)
	if err != nil {
		return err
	}
	scopes := strings.Fields(options.scopes)
	client := store.RuntimeOAuthClient{
		ClientID: clientID, Tenant: tenant, RuntimeID: options.runtimeID, RuntimeGeneration: options.runtimeGeneration,
		SecretHash: hashSecret(cfg.ClientCredentialPepper, secret), Scopes: scopes, CreatedAt: time.Now().UTC(),
	}
	if err := repository.UpsertRuntimeOAuthClient(ctx, client); err != nil {
		_ = stagedSecret.Discard()
		return err
	}
	if err := stagedSecret.Commit(); err != nil {
		if hasExisting {
			if restoreErr := repository.UpsertRuntimeOAuthClient(ctx, existing); restoreErr != nil {
				return fmt.Errorf("activate staged client secret %q: %w; restore previous client: %v", stagedSecret.stagedPath, err, restoreErr)
			}
		}
		return fmt.Errorf("activate staged client secret %q: %w", stagedSecret.stagedPath, err)
	}
	fmt.Fprintf(output, "client_id=%s\nsecret_file=%s\nscopes=%s\ntoken_url=http://127.0.0.1:8790/oauth/token\nmcp_url=http://127.0.0.1:8790/mcp\n", clientID, options.secretFile, strings.Join(scopes, " "))
	return nil
}

func parseOptions(args []string) (options, error) {
	var result options
	flags := flag.NewFlagSet("provision-runtime-oauth-client", flag.ContinueOnError)
	flags.SetOutput(os.Stderr)
	flags.StringVar(&result.databaseURLFile, "database-url-file", os.Getenv("ZIGGY_CONNECTORS_DATABASE_URL_FILE"), "database URL secret file")
	flags.StringVar(&result.clientCredentialPepperFile, "client-credential-pepper-file", os.Getenv("ZIGGY_CONNECTORS_CLIENT_CREDENTIAL_PEPPER_FILE"), "client credential pepper secret file")
	flags.StringVar(&result.userID, "user-id", "", "stable Ziggy user ID")
	flags.StringVar(&result.workspaceID, "workspace-id", "", "stable Ziggy workspace ID")
	flags.StringVar(&result.runtimeID, "runtime-id", "", "fenced runtime ID")
	flags.Int64Var(&result.runtimeGeneration, "runtime-generation", 0, "positive fenced runtime generation")
	flags.StringVar(&result.clientID, "client-id", "", "optional stable OAuth client ID")
	flags.StringVar(&result.scopes, "scopes", defaultScopes, "space-separated granted scopes")
	flags.StringVar(&result.secretFile, "secret-file", "", "path to create or replace with the client secret")
	if err := flags.Parse(args); err != nil {
		return options{}, err
	}
	result.databaseURLFile = strings.TrimSpace(result.databaseURLFile)
	result.clientCredentialPepperFile = strings.TrimSpace(result.clientCredentialPepperFile)
	result.userID = strings.TrimSpace(result.userID)
	result.workspaceID = strings.TrimSpace(result.workspaceID)
	result.runtimeID = strings.TrimSpace(result.runtimeID)
	result.clientID = strings.TrimSpace(result.clientID)
	result.secretFile = strings.TrimSpace(result.secretFile)
	if result.databaseURLFile == "" || result.clientCredentialPepperFile == "" || result.userID == "" || result.workspaceID == "" || result.runtimeID == "" || result.runtimeGeneration < 1 || result.secretFile == "" {
		return options{}, errors.New("database-url-file, client-credential-pepper-file, user-id, workspace-id, runtime-id, positive runtime-generation, and secret-file are required")
	}
	if scopes := strings.Fields(result.scopes); !sameScopes(scopes, strings.Fields(defaultScopes)) {
		return options{}, errors.New("scopes must be exactly gmail.status gmail.search gmail.read")
	}
	return result, nil
}

func loadProvisioningConfig(options options) (config.ProvisioningConfig, error) {
	env := map[string]string{
		"ZIGGY_CONNECTORS_DATABASE_URL_FILE":             options.databaseURLFile,
		"ZIGGY_CONNECTORS_CLIENT_CREDENTIAL_PEPPER_FILE": options.clientCredentialPepperFile,
	}
	return config.LoadProvisioningFrom(func(key string) (string, bool) { value, ok := env[key]; return value, ok }, os.ReadFile)
}

func sameScopes(actual, expected []string) bool {
	if len(actual) != len(expected) {
		return false
	}
	seen := make(map[string]struct{}, len(actual))
	for _, scope := range actual {
		seen[scope] = struct{}{}
	}
	if len(seen) != len(expected) {
		return false
	}
	for _, scope := range expected {
		if _, ok := seen[scope]; !ok {
			return false
		}
	}
	return true
}

func randomValue(size int) (string, error) {
	bytes := make([]byte, size)
	if _, err := rand.Read(bytes); err != nil {
		return "", err
	}
	return base64.RawURLEncoding.EncodeToString(bytes), nil
}

func hashSecret(pepper []byte, secret string) []byte {
	mac := hmac.New(sha256.New, pepper)
	_, _ = mac.Write([]byte(secret))
	return mac.Sum(nil)
}

type stagedSecretFile struct {
	path       string
	stagedPath string
}

func stageSecretFile(path, secret string) (*stagedSecretFile, error) {
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return nil, err
	}
	if info, err := os.Lstat(path); err == nil && !info.Mode().IsRegular() {
		return nil, errors.New("secret-file must be a regular file")
	} else if err != nil && !errors.Is(err, os.ErrNotExist) {
		return nil, err
	}
	file, err := os.CreateTemp(filepath.Dir(path), "."+filepath.Base(path)+".new-*")
	if err != nil {
		return nil, err
	}
	stagedPath := file.Name()
	cleanup := func(err error) (*stagedSecretFile, error) {
		_ = file.Close()
		_ = os.Remove(stagedPath)
		return nil, err
	}
	if err := file.Chmod(0o600); err != nil {
		return cleanup(err)
	}
	if _, err := file.WriteString(secret + "\n"); err != nil {
		return cleanup(err)
	}
	if err := file.Sync(); err != nil {
		return cleanup(err)
	}
	if err := file.Close(); err != nil {
		return cleanup(err)
	}
	return &stagedSecretFile{path: path, stagedPath: stagedPath}, nil
}

func (s *stagedSecretFile) Commit() error {
	if s == nil || s.stagedPath == "" {
		return errors.New("staged secret file is unavailable")
	}
	if err := os.Rename(s.stagedPath, s.path); err != nil {
		return err
	}
	s.stagedPath = ""
	return nil
}

func (s *stagedSecretFile) Discard() error {
	if s == nil || s.stagedPath == "" {
		return nil
	}
	err := os.Remove(s.stagedPath)
	s.stagedPath = ""
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	return err
}

func writeSecretFile(path, secret string) error {
	staged, err := stageSecretFile(path, secret)
	if err != nil {
		return err
	}
	if err := staged.Commit(); err != nil {
		return err
	}
	return nil
}
