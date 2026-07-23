package main

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"database/sql"
	"encoding/base64"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"strconv"
	"strings"
	"time"

	_ "github.com/jackc/pgx/v5/stdlib"
	"github.com/mihai-chiorean/nanobot/services/ziggy-connectors/internal/config"
	"github.com/mihai-chiorean/nanobot/services/ziggy-connectors/internal/store"
)

const defaultScopes = "gmail.status gmail.search gmail.read"
const provisionTimeout = 30 * time.Second

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
	ctx, cancel := context.WithTimeout(context.Background(), provisionTimeout)
	defer cancel()
	return provision(ctx, options, cfg, repository, output)
}

func provision(ctx context.Context, options options, cfg config.ProvisioningConfig, repository store.RuntimeOAuthClientRepository, output io.Writer) error {
	tenant := store.Tenant{UserID: options.userID, WorkspaceID: options.workspaceID}
	secret, err := readSecretFile(options.secretFile)
	if err != nil {
		return err
	}
	secretHash := hashSecret(cfg.ClientCredentialPepper, secret)
	clientID := options.clientID
	if clientID == "" {
		clientID = derivedClientID(tenant, options.runtimeID, options.runtimeGeneration)
	}
	scopes := strings.Fields(options.scopes)
	client := store.RuntimeOAuthClient{
		ClientID: clientID, Tenant: tenant, RuntimeID: options.runtimeID, RuntimeGeneration: options.runtimeGeneration,
		SecretHash: secretHash, Scopes: scopes, CreatedAt: time.Now().UTC(),
	}
	active, _, err := repository.RegisterRuntimeOAuthClient(ctx, client)
	if err != nil {
		return err
	}
	if active.RuntimeGeneration != options.runtimeGeneration {
		return errors.New("runtime generation replacement requires a separately staged credential rollout")
	}
	if options.clientID != "" && options.clientID != active.ClientID {
		return errors.New("client-id does not match the active runtime client")
	}
	if !hmac.Equal(active.SecretHash, secretHash) {
		return errors.New("secret file does not match the active runtime client; refusing unsafe rotation")
	}
	if !sameScopes(active.Scopes, scopes) {
		return errors.New("scopes do not match the active runtime client")
	}
	printProvisioning(output, active.ClientID, options.secretFile, active.Scopes)
	return nil
}

func printProvisioning(output io.Writer, clientID, secretFile string, scopes []string) {
	fmt.Fprintf(output, "client_id=%s\nsecret_file=%s\nscopes=%s\n", clientID, secretFile, strings.Join(scopes, " "))
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

func derivedClientID(tenant store.Tenant, runtimeID string, generation int64) string {
	sum := sha256.Sum256([]byte(tenant.UserID + "\x00" + tenant.WorkspaceID + "\x00" + runtimeID + "\x00" + strconv.FormatInt(generation, 10)))
	return "zrc_" + base64.RawURLEncoding.EncodeToString(sum[:18])
}

func hashSecret(pepper []byte, secret string) []byte {
	mac := hmac.New(sha256.New, pepper)
	_, _ = mac.Write([]byte(secret))
	return mac.Sum(nil)
}

func readSecretFile(path string) (string, error) {
	info, err := os.Lstat(path)
	if err != nil {
		return "", fmt.Errorf("read secret file: %w", err)
	}
	if !info.Mode().IsRegular() {
		return "", errors.New("secret-file must be a regular file")
	}
	if info.Mode().Perm()&0o077 != 0 {
		return "", errors.New("secret-file must not be accessible by group or others")
	}
	contents, err := os.ReadFile(path)
	if err != nil {
		return "", fmt.Errorf("read secret file: %w", err)
	}
	secret := strings.TrimSpace(string(contents))
	if len(secret) < 32 {
		return "", errors.New("secret-file must contain at least 32 characters")
	}
	return secret, nil
}
