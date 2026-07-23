package httpapi

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/mihai-chiorean/nanobot/services/ziggy-connectors/internal/crypto"
	"github.com/mihai-chiorean/nanobot/services/ziggy-connectors/internal/principal"
	"github.com/mihai-chiorean/nanobot/services/ziggy-connectors/internal/provider"
	"github.com/mihai-chiorean/nanobot/services/ziggy-connectors/internal/store"
	"github.com/modelcontextprotocol/go-sdk/mcp"
)

func TestGmailMCPDiscoversToolsAndKeepsAccountsTenantScoped(t *testing.T) {
	key := []byte("01234567890123456789012345678901")
	cipher, err := crypto.NewAESGCM(key)
	if err != nil {
		t.Fatal(err)
	}
	repository := store.NewMemoryRepository()
	tenant := store.Tenant{UserID: "user-a", WorkspaceID: "workspace-a"}
	accountID := "acct_google"
	encryptedRefresh, err := cipher.Encrypt(
		[]byte("refresh-token"),
		encryptionContext("refresh-token", tenant.UserID, tenant.WorkspaceID, accountID),
	)
	if err != nil {
		t.Fatal(err)
	}
	if err := repository.SaveAccount(context.Background(), tenant, store.Account{
		ID:                    accountID,
		Tenant:                tenant,
		Provider:              "google",
		ProviderSubject:       "google-subject",
		Email:                 "owner@example.test",
		Scopes:                []string{googleGmailReadonlyScope},
		EncryptedRefreshToken: encryptedRefresh,
		Status:                "active",
		CreatedAt:             time.Now(),
		UpdatedAt:             time.Now(),
	}); err != nil {
		t.Fatal(err)
	}
	handler, err := New(Config{
		Version:           "test",
		GoogleRedirectURI: "https://gateway.test/callback",
		GoogleScopes:      []string{"openid", "email", googleGmailReadonlyScope},
		StateTTL:          time.Minute,
		StateSigner:       crypto.NewStateSigner(key),
		TokenCipher:       cipher,
		Accounts:          repository,
		OAuthTransactions: repository,
		Google: provider.Fake{
			Token: provider.Token{AccessToken: "access-token"},
			Messages: []provider.MessageSummary{{
				ID: "message-1", ThreadID: "thread-1", Subject: "Daily update",
			}},
			Message: provider.Message{
				MessageSummary: provider.MessageSummary{ID: "message-1", Subject: "Daily update"},
				Body:           "Useful body",
			},
		},
		PrincipalVerifier: principal.NewVerifier(key),
	})
	if err != nil {
		t.Fatal(err)
	}
	server := httptest.NewServer(handler)
	defer server.Close()

	session := connectMCP(t, server.URL+"/mcp", principal.Principal{
		UserID: "user-a", WorkspaceID: "workspace-a", ExpiresAt: time.Now().Add(time.Hour).Unix(),
	}, key)
	defer session.Close()

	tools, err := session.ListTools(context.Background(), nil)
	if err != nil {
		t.Fatal(err)
	}
	if len(tools.Tools) != 3 {
		t.Fatalf("tool count = %d", len(tools.Tools))
	}
	for _, name := range []string{"gmail_connection_status", "gmail_search", "gmail_get_message"} {
		if !hasTool(tools, name) {
			t.Errorf("tool %q is missing", name)
		}
	}

	status, err := session.CallTool(context.Background(), &mcp.CallToolParams{
		Name: "gmail_connection_status", Arguments: map[string]any{},
	})
	if err != nil || status.IsError || !structuredContains(status, `"connected":true`, "owner@example.test") {
		t.Fatalf("status result = %#v, error = %v", status, err)
	}
	search, err := session.CallTool(context.Background(), &mcp.CallToolParams{
		Name: "gmail_search", Arguments: map[string]any{"query": "newer_than:1d", "max_results": 5},
	})
	if err != nil || search.IsError || !structuredContains(search, "message-1", "Daily update", "untrusted external data") {
		t.Fatalf("search result = %#v, error = %v", search, err)
	}
	message, err := session.CallTool(context.Background(), &mcp.CallToolParams{
		Name: "gmail_get_message", Arguments: map[string]any{"message_id": "message-1"},
	})
	if err != nil || message.IsError || !structuredContains(message, "Useful body", "untrusted external data") {
		t.Fatalf("message result = %#v, error = %v", message, err)
	}

	foreign := connectMCP(t, server.URL+"/mcp", principal.Principal{
		UserID: "user-b", WorkspaceID: "workspace-b", ExpiresAt: time.Now().Add(time.Hour).Unix(),
	}, key)
	defer foreign.Close()
	foreignSearch, err := foreign.CallTool(context.Background(), &mcp.CallToolParams{
		Name: "gmail_search", Arguments: map[string]any{"query": "in:inbox"},
	})
	if err != nil {
		t.Fatal(err)
	}
	if !foreignSearch.IsError || structuredContains(foreignSearch, "owner@example.test", "message-1") {
		t.Fatalf("foreign result exposed tenant data: %#v", foreignSearch)
	}
}

type principalTransport struct {
	base      http.RoundTripper
	principal principal.Principal
	key       []byte
}

func (transport principalTransport) RoundTrip(request *http.Request) (*http.Response, error) {
	clone := request.Clone(request.Context())
	withPrincipal(clone, transport.principal, transport.key)
	return transport.base.RoundTrip(clone)
}

func connectMCP(t *testing.T, endpoint string, p principal.Principal, key []byte) *mcp.ClientSession {
	t.Helper()
	client := mcp.NewClient(&mcp.Implementation{Name: "ziggy-connectors-test", Version: "test"}, nil)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	session, err := client.Connect(ctx, &mcp.StreamableClientTransport{
		Endpoint: endpoint,
		HTTPClient: &http.Client{Transport: principalTransport{
			base: http.DefaultTransport, principal: p, key: key,
		}},
		DisableStandaloneSSE: true,
	}, nil)
	if err != nil {
		t.Fatal(err)
	}
	return session
}

func hasTool(result *mcp.ListToolsResult, name string) bool {
	for _, tool := range result.Tools {
		if tool.Name == name {
			return true
		}
	}
	return false
}

func structuredContains(result *mcp.CallToolResult, values ...string) bool {
	encoded, _ := json.Marshal(result.StructuredContent)
	for _, value := range values {
		if !strings.Contains(string(encoded), value) {
			return false
		}
	}
	return true
}
