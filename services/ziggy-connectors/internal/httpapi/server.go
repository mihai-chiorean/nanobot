package httpapi

import (
	"context"
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"strings"
	"sync"
	"time"

	"github.com/mihai-chiorean/nanobot/services/ziggy-connectors/internal/crypto"
	"github.com/mihai-chiorean/nanobot/services/ziggy-connectors/internal/principal"
	"github.com/mihai-chiorean/nanobot/services/ziggy-connectors/internal/provider"
	"github.com/mihai-chiorean/nanobot/services/ziggy-connectors/internal/runtimeauth"
	"github.com/mihai-chiorean/nanobot/services/ziggy-connectors/internal/store"
	"github.com/modelcontextprotocol/go-sdk/mcp"
	"golang.org/x/sync/singleflight"
)

type Config struct {
	Environment            string
	Version                string
	GoogleRedirectURI      string
	GoogleScopes           []string
	StateTTL               time.Duration
	StateSigner            *crypto.StateSigner
	TokenCipher            crypto.Cipher
	Accounts               store.AccountRepository
	OAuthTransactions      store.OAuthTransactionRepository
	RuntimeOAuthClients    store.RuntimeOAuthClientRepository
	Google                 provider.Gmail
	PrincipalVerifier      *principal.Verifier
	ClientCredentialPepper []byte
	MCPAccessTokens        *runtimeauth.TokenManager
	OAuthIssuerURL         string
	MCPResourceURL         string
	Logger                 *slog.Logger
	Now                    func() time.Time
}

type API struct {
	config         Config
	logger         *slog.Logger
	mcpSchemaCache *mcp.SchemaCache
	accessTokens   sync.Map
	accessRefresh  singleflight.Group
	gmailLimiters  sync.Map
}

type principalContextKey struct{}
type runtimeClaimsContextKey struct{}

const googleGmailReadonlyScope = "https://www.googleapis.com/auth/gmail.readonly"
const maximumMCPRequestBytes = 1 << 20

var runtimeMCPScopes = []string{"gmail.status", "gmail.search", "gmail.read"}

func New(config Config) (http.Handler, error) {
	if strings.TrimSpace(config.GoogleRedirectURI) == "" || config.StateTTL <= 0 || len(config.GoogleScopes) == 0 {
		return nil, errors.New("OAuth redirect URI, scopes, and state TTL are required")
	}
	if config.StateSigner == nil || config.TokenCipher == nil || config.Accounts == nil || config.OAuthTransactions == nil || config.RuntimeOAuthClients == nil || config.Google == nil || config.PrincipalVerifier == nil || len(config.ClientCredentialPepper) < 32 || config.MCPAccessTokens == nil || strings.TrimSpace(config.OAuthIssuerURL) == "" || strings.TrimSpace(config.MCPResourceURL) == "" {
		return nil, errors.New("all connector dependencies are required")
	}
	if config.Logger == nil {
		config.Logger = slog.New(slog.NewTextHandler(nilDiscard{}, nil))
	}
	if config.Now == nil {
		config.Now = time.Now
	}
	api := &API{config: config, logger: config.Logger, mcpSchemaCache: mcp.NewSchemaCache()}
	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", api.health)
	mux.HandleFunc("/readyz", api.ready)
	mux.HandleFunc("/.well-known/oauth-protected-resource", api.protectedResourceMetadata)
	mux.HandleFunc("/.well-known/oauth-protected-resource/mcp", api.protectedResourceMetadata)
	mux.HandleFunc("/.well-known/oauth-authorization-server", api.authorizationServerMetadata)
	mux.Handle("/oauth/google/start", api.auth(http.HandlerFunc(api.oauthStart)))
	mux.HandleFunc("/oauth/google/callback", api.oauthCallback)
	mux.Handle("/accounts", api.auth(http.HandlerFunc(api.accounts)))
	mux.Handle("/oauth/token", http.MaxBytesHandler(http.HandlerFunc(api.oauthToken), 16<<10))
	mux.Handle("/mcp", http.MaxBytesHandler(api.mcpAuth(api.newMCPHandler()), maximumMCPRequestBytes))
	return mux, nil
}

func (api *API) mcpAuth(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		authorization := strings.TrimSpace(r.Header.Get("Authorization"))
		parts := strings.Fields(authorization)
		if len(parts) != 2 || !strings.EqualFold(parts[0], "Bearer") {
			api.mcpUnauthorized(w)
			return
		}
		claims, err := api.config.MCPAccessTokens.Verify(parts[1], api.config.Now())
		if err != nil {
			api.mcpUnauthorized(w)
			return
		}
		ctx := context.WithValue(r.Context(), runtimeClaimsContextKey{}, claims)
		next.ServeHTTP(w, r.WithContext(ctx))
	})
}

func runtimeClaimsFromContext(ctx context.Context) (runtimeauth.Claims, bool) {
	claims, ok := ctx.Value(runtimeClaimsContextKey{}).(runtimeauth.Claims)
	return claims, ok
}

func (api *API) mcpUnauthorized(w http.ResponseWriter) {
	w.Header().Set("WWW-Authenticate", fmt.Sprintf(`Bearer resource_metadata=%q, scope=%q`, api.protectedResourceMetadataURL(), strings.Join(runtimeMCPScopes, " ")))
	writeJSON(w, http.StatusUnauthorized, map[string]string{"error": "runtime bearer token required"})
}

func (api *API) protectedResourceMetadata(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		w.Header().Set("Allow", http.MethodGet)
		writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "method not allowed"})
		return
	}
	w.Header().Set("Access-Control-Allow-Origin", "*")
	writeJSON(w, http.StatusOK, map[string]any{
		"resource":                 api.config.MCPResourceURL,
		"authorization_servers":    []string{api.config.OAuthIssuerURL},
		"scopes_supported":         runtimeMCPScopes,
		"bearer_methods_supported": []string{"header"},
		"resource_name":            "Ziggy Gmail MCP",
	})
}

func (api *API) authorizationServerMetadata(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		w.Header().Set("Allow", http.MethodGet)
		writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "method not allowed"})
		return
	}
	w.Header().Set("Access-Control-Allow-Origin", "*")
	writeJSON(w, http.StatusOK, map[string]any{
		"issuer":                                api.config.OAuthIssuerURL,
		"token_endpoint":                        api.config.OAuthIssuerURL + "/oauth/token",
		"grant_types_supported":                 []string{"client_credentials"},
		"token_endpoint_auth_methods_supported": []string{"client_secret_basic"},
		"scopes_supported":                      runtimeMCPScopes,
		"response_types_supported":              []string{},
	})
}

func (api *API) protectedResourceMetadataURL() string {
	return api.config.OAuthIssuerURL + "/.well-known/oauth-protected-resource/mcp"
}

func (api *API) oauthToken(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		w.Header().Set("Allow", http.MethodPost)
		writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "method not allowed"})
		return
	}
	clientID, clientSecret, ok := r.BasicAuth()
	if !ok || strings.TrimSpace(clientID) == "" || clientSecret == "" {
		oauthInvalidClient(w)
		return
	}
	if err := r.ParseForm(); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid_request"})
		return
	}
	if r.PostForm.Get("grant_type") != "client_credentials" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "unsupported_grant_type"})
		return
	}
	if resource := strings.TrimSpace(r.Form.Get("resource")); resource != api.config.MCPResourceURL {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid_target"})
		return
	}
	for _, field := range []string{"user_id", "workspace_id", "runtime_id", "runtime_generation"} {
		if strings.TrimSpace(r.Form.Get(field)) != "" {
			writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid_request"})
			return
		}
	}
	client, err := api.config.RuntimeOAuthClients.GetRuntimeOAuthClient(r.Context(), clientID)
	if err != nil || !hmac.Equal(client.SecretHash, clientSecretHash(api.config.ClientCredentialPepper, clientSecret)) {
		oauthInvalidClient(w)
		return
	}
	scopes, err := requestedScopes(r.PostForm.Get("scope"), client.Scopes)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid_scope"})
		return
	}
	jti, err := randomID()
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "server_error"})
		return
	}
	now := api.config.Now().UTC()
	accessToken, err := api.config.MCPAccessTokens.Issue(runtimeauth.Claims{
		Issuer: api.config.OAuthIssuerURL, Audience: api.config.MCPResourceURL, IssuedAt: now.Unix(), ExpiresAt: now.Add(runtimeauth.TTL).Unix(), JWTID: jti,
		ClientID: client.ClientID, UserID: client.Tenant.UserID, WorkspaceID: client.Tenant.WorkspaceID, RuntimeID: client.RuntimeID, RuntimeGeneration: client.RuntimeGeneration, Scopes: scopes,
	})
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "server_error"})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"access_token": accessToken, "token_type": "Bearer", "expires_in": int(runtimeauth.TTL / time.Second), "scope": strings.Join(scopes, " ")})
}

func oauthInvalidClient(w http.ResponseWriter) {
	w.Header().Set("WWW-Authenticate", `Basic realm="ziggy-connectors"`)
	writeJSON(w, http.StatusUnauthorized, map[string]string{"error": "invalid_client"})
}

func clientSecretHash(pepper []byte, secret string) []byte {
	mac := hmac.New(sha256.New, pepper)
	_, _ = mac.Write([]byte(secret))
	return mac.Sum(nil)
}

func requestedScopes(raw string, allowed []string) ([]string, error) {
	requested := strings.Fields(raw)
	if len(requested) == 0 {
		requested = append([]string(nil), allowed...)
	}
	if len(requested) == 0 {
		return nil, errors.New("no client scopes")
	}
	seen := make(map[string]struct{}, len(requested))
	result := make([]string, 0, len(requested))
	for _, scope := range requested {
		if _, duplicate := seen[scope]; duplicate || !containsScope(allowed, scope) {
			return nil, errors.New("invalid client scope")
		}
		seen[scope] = struct{}{}
		result = append(result, scope)
	}
	return result, nil
}

func containsScope(scopes []string, wanted string) bool {
	for _, scope := range scopes {
		if scope == wanted {
			return true
		}
	}
	return false
}

func (api *API) auth(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		p, err := api.config.PrincipalVerifier.VerifyRequest(r)
		if err != nil {
			writeJSON(w, http.StatusUnauthorized, map[string]string{"error": "private gateway identity required"})
			return
		}
		ctx := context.WithValue(r.Context(), principalContextKey{}, p)
		next.ServeHTTP(w, r.WithContext(ctx))
	})
}

func principalFromContext(ctx context.Context) (principal.Principal, bool) {
	p, ok := ctx.Value(principalContextKey{}).(principal.Principal)
	return p, ok
}

func (api *API) health(w http.ResponseWriter, r *http.Request) {
	if !readMethod(w, r) {
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok", "service": "ziggy-connectors", "version": api.config.Version})
}

func (api *API) ready(w http.ResponseWriter, r *http.Request) {
	if !readMethod(w, r) {
		return
	}
	if readiness, ok := api.config.Accounts.(store.Readiness); ok {
		if err := readiness.Ready(r.Context()); err != nil {
			writeJSON(w, http.StatusServiceUnavailable, map[string]string{"status": "not_ready"})
			return
		}
	}
	writeJSON(w, http.StatusOK, map[string]string{"status": "ready"})
}

func (api *API) oauthStart(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		w.Header().Set("Allow", http.MethodGet)
		writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "method not allowed"})
		return
	}
	p, ok := principalFromContext(r.Context())
	if !ok {
		writeJSON(w, http.StatusUnauthorized, map[string]string{"error": "private gateway identity required"})
		return
	}
	now := api.config.Now()
	txID, err := randomID()
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "unable to start authorization"})
		return
	}
	nonce, err := randomID()
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "unable to start authorization"})
		return
	}
	verifier, err := randomVerifier()
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "unable to start authorization"})
		return
	}
	expiresAt := now.Add(api.config.StateTTL)
	state, err := api.config.StateSigner.Sign(crypto.StateClaims{TransactionID: txID, UserID: p.UserID, WorkspaceID: p.WorkspaceID, Nonce: nonce, ExpiresAt: expiresAt.Unix()})
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "unable to start authorization"})
		return
	}
	encryptedVerifier, err := api.config.TokenCipher.Encrypt(
		[]byte(verifier), encryptionContext("oauth-pkce", p.UserID, p.WorkspaceID, txID),
	)
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "unable to start authorization"})
		return
	}
	transaction := store.OAuthTransaction{ID: txID, Tenant: store.Tenant{UserID: p.UserID, WorkspaceID: p.WorkspaceID}, StateHash: crypto.Hash(state), Nonce: nonce, EncryptedPKCEVerifier: encryptedVerifier, RedirectURI: api.config.GoogleRedirectURI, ExpiresAt: expiresAt}
	if err := api.config.OAuthTransactions.CreateOAuthTransaction(r.Context(), transaction.Tenant, transaction); err != nil {
		api.logger.ErrorContext(r.Context(), "OAuth transaction creation failed", "error_class", "oauth_transaction_store_failure")
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "unable to start authorization"})
		return
	}
	authorizationURL, err := api.config.Google.AuthorizationURL(api.config.GoogleRedirectURI, state, verifier, api.config.GoogleScopes)
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "unable to start authorization"})
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"authorization_url": authorizationURL})
}

func (api *API) oauthCallback(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		w.Header().Set("Allow", http.MethodGet)
		writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "method not allowed"})
		return
	}
	state := strings.TrimSpace(r.URL.Query().Get("state"))
	claims, err := api.config.StateSigner.Verify(state, api.config.Now())
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid authorization state"})
		return
	}
	tenant := store.Tenant{UserID: claims.UserID, WorkspaceID: claims.WorkspaceID}
	transaction, err := api.config.OAuthTransactions.ConsumeOAuthTransaction(r.Context(), tenant, crypto.Hash(state))
	if err != nil {
		status := http.StatusBadRequest
		if errors.Is(err, store.ErrReplay) {
			status = http.StatusConflict
		}
		writeJSON(w, status, map[string]string{"error": "authorization transaction unavailable"})
		return
	}
	if transaction.ID != claims.TransactionID || transaction.Nonce != claims.Nonce || transaction.RedirectURI != api.config.GoogleRedirectURI {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "authorization transaction mismatch"})
		return
	}
	verifierBytes, err := api.config.TokenCipher.Decrypt(
		transaction.EncryptedPKCEVerifier,
		encryptionContext("oauth-pkce", tenant.UserID, tenant.WorkspaceID, transaction.ID),
	)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "authorization transaction unavailable"})
		return
	}
	code := strings.TrimSpace(r.URL.Query().Get("code"))
	if code == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "authorization code missing"})
		return
	}
	token, err := api.config.Google.ExchangeCode(r.Context(), code, string(verifierBytes), transaction.RedirectURI)
	if err != nil {
		api.logger.WarnContext(r.Context(), "Google OAuth exchange failed", "error_class", "google_oauth_exchange_failure")
		writeJSON(w, http.StatusBadGateway, map[string]string{"error": "Google authorization failed"})
		return
	}
	if !hasScope(token.Scopes, googleGmailReadonlyScope) {
		api.logger.WarnContext(r.Context(), "Required Google scope was not granted", "error_class", "google_required_scope_missing")
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "Google Gmail read permission was not granted; reconnect and approve Gmail access"})
		return
	}
	profile, err := api.config.Google.ValidateProfile(r.Context(), token.AccessToken)
	if err != nil || profile.Provider != "google" || profile.Subject == "" || profile.Email == "" {
		api.logger.WarnContext(r.Context(), "Google profile validation failed", "error_class", "google_profile_validation_failure")
		writeJSON(w, http.StatusBadGateway, map[string]string{"error": "Google account validation failed"})
		return
	}
	accountID := stableAccountID(profile.Provider, profile.Subject)
	encryptedRefresh, err := api.config.TokenCipher.Encrypt(
		[]byte(token.RefreshToken),
		encryptionContext("refresh-token", tenant.UserID, tenant.WorkspaceID, accountID),
	)
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "unable to save Google account"})
		return
	}
	now := api.config.Now()
	account := store.Account{ID: accountID, Tenant: tenant, Provider: profile.Provider, ProviderSubject: profile.Subject, Email: profile.Email, Scopes: token.Scopes, EncryptedRefreshToken: encryptedRefresh, TokenKeyVersion: "v1", Status: "active", CreatedAt: now, UpdatedAt: now}
	if err := api.config.Accounts.SaveAccount(r.Context(), tenant, account); err != nil {
		api.logger.ErrorContext(r.Context(), "Google account persistence failed", "error_class", "account_store_failure")
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "unable to save Google account"})
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"status": "connected", "account_id": account.ID})
}

func hasScope(scopes []string, required string) bool {
	for _, scope := range scopes {
		if scope == required {
			return true
		}
	}
	return false
}

func (api *API) accounts(w http.ResponseWriter, r *http.Request) {
	if !readMethod(w, r) {
		return
	}
	p, ok := principalFromContext(r.Context())
	if !ok {
		writeJSON(w, http.StatusUnauthorized, map[string]string{"error": "private gateway identity required"})
		return
	}
	accounts, err := api.config.Accounts.ListAccounts(r.Context(), store.Tenant{UserID: p.UserID, WorkspaceID: p.WorkspaceID})
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "unable to read account status"})
		return
	}
	result := make([]accountStatus, 0, len(accounts))
	for _, account := range accounts {
		result = append(result, accountStatus{ID: account.ID, Provider: account.Provider, Email: account.Email, Scopes: account.Scopes, Status: account.Status, LastError: account.LastError, CreatedAt: account.CreatedAt, UpdatedAt: account.UpdatedAt})
	}
	writeJSON(w, http.StatusOK, map[string]any{"accounts": result})
}

type accountStatus struct {
	ID        string    `json:"account_id"`
	Provider  string    `json:"provider"`
	Email     string    `json:"email"`
	Scopes    []string  `json:"scopes"`
	Status    string    `json:"status"`
	LastError string    `json:"last_error,omitempty"`
	CreatedAt time.Time `json:"created_at"`
	UpdatedAt time.Time `json:"updated_at"`
}

func Serve(ctx context.Context, server *http.Server, shutdownTimeout time.Duration) error {
	if server == nil || server.Handler == nil || shutdownTimeout <= 0 {
		return errors.New("server, handler, and positive shutdown timeout are required")
	}
	listener, err := (&netListen{}).Listen(server.Addr)
	if err != nil {
		return fmt.Errorf("listen: %w", err)
	}
	serveErr := make(chan error, 1)
	go func() { serveErr <- server.Serve(listener) }()
	select {
	case err := <-serveErr:
		if errors.Is(err, http.ErrServerClosed) {
			return nil
		}
		return err
	case <-ctx.Done():
		shutdownCtx, cancel := context.WithTimeout(context.Background(), shutdownTimeout)
		defer cancel()
		if err := server.Shutdown(shutdownCtx); err != nil {
			return fmt.Errorf("graceful shutdown: %w", err)
		}
		return nil
	}
}

func ServeTLS(ctx context.Context, server *http.Server, shutdownTimeout time.Duration, certFile, keyFile string) error {
	if server == nil || server.Handler == nil || shutdownTimeout <= 0 || strings.TrimSpace(certFile) == "" || strings.TrimSpace(keyFile) == "" {
		return errors.New("server, handler, positive shutdown timeout, TLS certificate file, and TLS key file are required")
	}
	listener, err := (&netListen{}).Listen(server.Addr)
	if err != nil {
		return fmt.Errorf("listen: %w", err)
	}
	defer listener.Close()
	serveErr := make(chan error, 1)
	go func() { serveErr <- server.ServeTLS(listener, certFile, keyFile) }()
	select {
	case err := <-serveErr:
		if errors.Is(err, http.ErrServerClosed) {
			return nil
		}
		return err
	case <-ctx.Done():
		shutdownCtx, cancel := context.WithTimeout(context.Background(), shutdownTimeout)
		defer cancel()
		if err := server.Shutdown(shutdownCtx); err != nil {
			return fmt.Errorf("graceful shutdown: %w", err)
		}
		return nil
	}
}

type netListen struct{}

func (*netListen) Listen(address string) (net.Listener, error) { return net.Listen("tcp", address) }

func randomID() (string, error) {
	bytes := make([]byte, 16)
	if _, err := rand.Read(bytes); err != nil {
		return "", err
	}
	return hex.EncodeToString(bytes), nil
}

func randomVerifier() (string, error) {
	bytes := make([]byte, 32)
	if _, err := rand.Read(bytes); err != nil {
		return "", err
	}
	return hex.EncodeToString(bytes), nil
}

func stableAccountID(providerName, subject string) string {
	hash := crypto.Hash(providerName + "\x00" + subject)
	return hex.EncodeToString(hash[:16])
}

func encryptionContext(kind, userID, workspaceID, objectID string) []byte {
	return []byte(kind + "\x00" + userID + "\x00" + workspaceID + "\x00" + objectID)
}

func readMethod(w http.ResponseWriter, r *http.Request) bool {
	if r.Method == http.MethodGet {
		return true
	}
	w.Header().Set("Allow", http.MethodGet)
	writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "method not allowed"})
	return false
}

func writeJSON(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}

type nilDiscard struct{}

func (nilDiscard) Write(p []byte) (int, error) { return len(p), nil }
