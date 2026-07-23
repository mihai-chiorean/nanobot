package main

import (
	"bytes"
	"context"
	"os"
	"path/filepath"
	"strings"
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

func TestReadSecretFileRequiresRestrictedHighEntropyFile(t *testing.T) {
	path := filepath.Join(t.TempDir(), "oauth-client-secret")
	secret := "01234567890123456789012345678901"
	if err := os.WriteFile(path, []byte(secret+"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if got, err := readSecretFile(path); err != nil || got != secret {
		t.Fatalf("readSecretFile() = %q, %v", got, err)
	}
	if err := os.Chmod(path, 0o644); err != nil {
		t.Fatal(err)
	}
	if _, err := readSecretFile(path); err == nil {
		t.Fatal("accepted broadly readable secret")
	}
}

func TestProvisionIsIdempotentAndRefusesUnsafeRotation(t *testing.T) {
	path := filepath.Join(t.TempDir(), "oauth-client-secret")
	secret := "01234567890123456789012345678901"
	if err := os.WriteFile(path, []byte(secret+"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	tenant := store.Tenant{UserID: "user", WorkspaceID: "workspace"}
	pepper := []byte("01234567890123456789012345678901")
	repository := &failingClientRepository{existing: store.RuntimeOAuthClient{ClientID: "existing-client", Tenant: tenant, RuntimeID: "runtime", RuntimeGeneration: 2, SecretHash: hashSecret(pepper, secret), Scopes: []string{"gmail.status", "gmail.search", "gmail.read"}}}
	options := options{userID: tenant.UserID, workspaceID: tenant.WorkspaceID, runtimeID: "runtime", runtimeGeneration: 2, scopes: defaultScopes, secretFile: path}
	if err := provision(context.Background(), options, config.ProvisioningConfig{ClientCredentialPepper: pepper}, repository, &bytes.Buffer{}); err != nil {
		t.Fatalf("idempotent provision failed: %v", err)
	}
	if repository.registrations != 1 {
		t.Fatalf("idempotent provision performed %d registrations", repository.registrations)
	}
	if err := os.WriteFile(path, []byte("different-secret-0123456789012345\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := provision(context.Background(), options, config.ProvisioningConfig{ClientCredentialPepper: pepper}, repository, &bytes.Buffer{}); err == nil {
		t.Fatal("unsafe secret rotation succeeded")
	}
}

func TestProvisionCreatesDeterministicClientFromExistingSecret(t *testing.T) {
	path := filepath.Join(t.TempDir(), "oauth-client-secret")
	secret := "01234567890123456789012345678901"
	if err := os.WriteFile(path, []byte(secret+"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	repository := &failingClientRepository{}
	options := options{userID: "user", workspaceID: "workspace", runtimeID: "runtime", runtimeGeneration: 1, scopes: defaultScopes, secretFile: path}
	var output bytes.Buffer
	if err := provision(context.Background(), options, config.ProvisioningConfig{ClientCredentialPepper: []byte("01234567890123456789012345678901")}, repository, &output); err != nil {
		t.Fatal(err)
	}
	if repository.registrations != 1 || !strings.Contains(output.String(), "client_id=zrc_") {
		t.Fatalf("registrations = %d, output = %q", repository.registrations, output.String())
	}
}

type failingClientRepository struct {
	existing      store.RuntimeOAuthClient
	registerErr   error
	registrations int
}

func (r *failingClientRepository) RegisterRuntimeOAuthClient(_ context.Context, client store.RuntimeOAuthClient) (store.RuntimeOAuthClient, bool, error) {
	r.registrations++
	if r.registerErr != nil {
		return store.RuntimeOAuthClient{}, false, r.registerErr
	}
	if r.existing.ClientID != "" {
		return r.existing, false, nil
	}
	r.existing = client
	return client, true, nil
}

func (r *failingClientRepository) GetRuntimeOAuthClient(context.Context, string) (store.RuntimeOAuthClient, error) {
	return store.RuntimeOAuthClient{}, store.ErrNotFound
}

func (r *failingClientRepository) GetRuntimeOAuthClientForRuntime(context.Context, store.Tenant, string, int64) (store.RuntimeOAuthClient, error) {
	return r.existing, nil
}

func (r *failingClientRepository) GetActiveRuntimeOAuthClient(context.Context, store.Tenant, string) (store.RuntimeOAuthClient, error) {
	if r.existing.ClientID == "" {
		return store.RuntimeOAuthClient{}, store.ErrNotFound
	}
	return r.existing, nil
}
