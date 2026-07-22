package httpapi

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"strings"
	"time"

	"github.com/mihai-chiorean/nanobot/services/ziggy-connectors/internal/crypto"
	"github.com/mihai-chiorean/nanobot/services/ziggy-connectors/internal/principal"
	"github.com/mihai-chiorean/nanobot/services/ziggy-connectors/internal/provider"
	"github.com/mihai-chiorean/nanobot/services/ziggy-connectors/internal/store"
)

type Config struct {
	Environment       string
	Version           string
	GoogleRedirectURI string
	GoogleScopes      []string
	StateTTL          time.Duration
	StateSigner       *crypto.StateSigner
	TokenCipher       crypto.Cipher
	Accounts          store.AccountRepository
	OAuthTransactions store.OAuthTransactionRepository
	Google            provider.Gmail
	PrincipalVerifier *principal.Verifier
	Logger            *slog.Logger
	Now               func() time.Time
}

type API struct {
	config Config
	logger *slog.Logger
}

type principalContextKey struct{}

func New(config Config) (http.Handler, error) {
	if strings.TrimSpace(config.GoogleRedirectURI) == "" || config.StateTTL <= 0 || len(config.GoogleScopes) == 0 {
		return nil, errors.New("OAuth redirect URI, scopes, and state TTL are required")
	}
	if config.StateSigner == nil || config.TokenCipher == nil || config.Accounts == nil || config.OAuthTransactions == nil || config.Google == nil || config.PrincipalVerifier == nil {
		return nil, errors.New("all connector dependencies are required")
	}
	if config.Logger == nil {
		config.Logger = slog.New(slog.NewTextHandler(nilDiscard{}, nil))
	}
	if config.Now == nil {
		config.Now = time.Now
	}
	api := &API{config: config, logger: config.Logger}
	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", api.health)
	mux.HandleFunc("/readyz", api.ready)
	mux.Handle("/oauth/google/start", api.auth(http.HandlerFunc(api.oauthStart)))
	mux.HandleFunc("/oauth/google/callback", api.oauthCallback)
	mux.Handle("/accounts", api.auth(http.HandlerFunc(api.accounts)))
	return mux, nil
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
	if len(token.Scopes) == 0 {
		token.Scopes = append([]string(nil), api.config.GoogleScopes...)
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
