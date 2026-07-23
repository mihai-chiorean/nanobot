package main

import (
	"bytes"
	"context"
	"errors"
	"os"
	"path/filepath"
	"testing"

	"github.com/mihai-chiorean/nanobot/services/ziggy-connectors/internal/config"
	"github.com/mihai-chiorean/nanobot/services/ziggy-connectors/internal/store"
)

func TestParseOptionsRequiresRuntimeFenceAndExactScopes(t *testing.T) {
	_, err := parseOptions([]string{"--database-url-file", "/db", "--client-credential-pepper-file", "/pepper", "--user-id", "u", "--workspace-id", "w", "--runtime-id", "r", "--runtime-generation", "1", "--secret-file", "/secret", "--scopes", "gmail.read"})
	if err == nil {
		t.Fatal("accepted reduced scope set")
	}
	options, err := parseOptions([]string{"--database-url-file", "/db", "--client-credential-pepper-file", "/pepper", "--user-id", "u", "--workspace-id", "w", "--runtime-id", "r", "--runtime-generation", "1", "--secret-file", "/secret"})
	if err != nil || options.scopes != defaultScopes {
		t.Fatalf("parseOptions() = %#v, %v", options, err)
	}
}

func TestWriteSecretFileRestrictsPermissions(t *testing.T) {
	path := filepath.Join(t.TempDir(), "runtime", "oauth-client-secret")
	if err := writeSecretFile(path, "secret-value"); err != nil {
		t.Fatal(err)
	}
	info, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	if info.Mode().Perm() != 0o600 {
		t.Fatalf("secret mode = %o", info.Mode().Perm())
	}
	contents, err := os.ReadFile(path)
	if err != nil || string(contents) != "secret-value\n" {
		t.Fatalf("secret file = %q, %v", contents, err)
	}
}

func TestProvisionKeepsExistingSecretWhenDatabaseUpsertFails(t *testing.T) {
	path := filepath.Join(t.TempDir(), "oauth-client-secret")
	if err := os.WriteFile(path, []byte("previous-secret\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	tenant := store.Tenant{UserID: "user", WorkspaceID: "workspace"}
	repository := &failingClientRepository{existing: store.RuntimeOAuthClient{ClientID: "existing-client", Tenant: tenant, RuntimeID: "runtime", RuntimeGeneration: 2, SecretHash: []byte("previous-hash"), Scopes: []string{"gmail.status", "gmail.search", "gmail.read"}}, upsertErr: errors.New("database unavailable")}
	options := options{userID: tenant.UserID, workspaceID: tenant.WorkspaceID, runtimeID: "runtime", runtimeGeneration: 2, scopes: defaultScopes, secretFile: path}
	if err := provision(context.Background(), options, config.ProvisioningConfig{ClientCredentialPepper: []byte("01234567890123456789012345678901")}, repository, &bytes.Buffer{}); err == nil {
		t.Fatal("provision succeeded despite database failure")
	}
	contents, err := os.ReadFile(path)
	if err != nil || string(contents) != "previous-secret\n" {
		t.Fatalf("credential after database failure = %q, %v", contents, err)
	}
	staged, err := filepath.Glob(filepath.Join(filepath.Dir(path), ".oauth-client-secret.new-*"))
	if err != nil || len(staged) != 0 {
		t.Fatalf("staged files after database failure = %v, %v", staged, err)
	}
}

type failingClientRepository struct {
	existing  store.RuntimeOAuthClient
	upsertErr error
}

func (r *failingClientRepository) UpsertRuntimeOAuthClient(context.Context, store.RuntimeOAuthClient) error {
	return r.upsertErr
}

func (r *failingClientRepository) GetRuntimeOAuthClient(context.Context, string) (store.RuntimeOAuthClient, error) {
	return store.RuntimeOAuthClient{}, store.ErrNotFound
}

func (r *failingClientRepository) GetRuntimeOAuthClientForRuntime(context.Context, store.Tenant, string, int64) (store.RuntimeOAuthClient, error) {
	return r.existing, nil
}
