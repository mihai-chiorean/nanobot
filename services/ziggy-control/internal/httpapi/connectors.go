package httpapi

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"errors"
	"net/http"
	"strings"
	"time"

	"github.com/mihai-chiorean/nanobot/services/ziggy-control/internal/identity"
)

const connectorPrincipalTTL = time.Minute

type ConnectorSigner struct {
	key []byte
	now func() time.Time
}

type connectorPrincipal struct {
	UserID      string `json:"user_id"`
	WorkspaceID string `json:"workspace_id"`
	ExpiresAt   int64  `json:"expires_at"`
}

type connectorPrincipalKey struct{}

type signedConnectorPrincipal struct {
	payload   string
	signature string
}

func NewConnectorSigner(key []byte) (*ConnectorSigner, error) {
	if len(key) < 32 {
		return nil, errors.New("connector trust key must contain at least 32 bytes")
	}
	return &ConnectorSigner{key: append([]byte(nil), key...), now: time.Now}, nil
}

func (signer *ConnectorSigner) sign(userID, workspaceID string) (signedConnectorPrincipal, error) {
	if strings.TrimSpace(userID) == "" || strings.TrimSpace(workspaceID) == "" {
		return signedConnectorPrincipal{}, errors.New("connector principal is incomplete")
	}
	payload, err := json.Marshal(connectorPrincipal{
		UserID:      userID,
		WorkspaceID: workspaceID,
		ExpiresAt:   signer.now().Add(connectorPrincipalTTL).Unix(),
	})
	if err != nil {
		return signedConnectorPrincipal{}, err
	}
	encoded := base64.RawURLEncoding.EncodeToString(payload)
	mac := hmac.New(sha256.New, signer.key)
	_, _ = mac.Write([]byte(encoded))
	return signedConnectorPrincipal{
		payload:   encoded,
		signature: base64.RawURLEncoding.EncodeToString(mac.Sum(nil)),
	}, nil
}

func withConnectorPrincipal(ctx context.Context, principal signedConnectorPrincipal) context.Context {
	return context.WithValue(ctx, connectorPrincipalKey{}, principal)
}

func connectorPrincipalFromContext(ctx context.Context) (signedConnectorPrincipal, bool) {
	principal, ok := ctx.Value(connectorPrincipalKey{}).(signedConnectorPrincipal)
	return principal, ok
}

func (api *API) connectorCallback(w http.ResponseWriter, r *http.Request) {
	if api.connectorProxy == nil {
		http.NotFound(w, r)
		return
	}
	api.connectorProxy.ServeHTTP(w, r)
}

func (api *API) connectors(w http.ResponseWriter, r *http.Request) {
	if api.connectorProxy == nil || api.connectorSigner == nil || api.tenantRouter == nil {
		http.NotFound(w, r)
		return
	}
	principal, ok := identity.FromContext(r.Context())
	if !ok {
		writeError(w, http.StatusUnauthorized, "authentication required")
		return
	}
	route, err := api.tenantRouter.ResolvePrincipal(r.Context(), principal)
	if err != nil {
		writeError(w, http.StatusForbidden, "account not authorized")
		return
	}
	signed, err := api.connectorSigner.sign(route.UserID, route.WorkspaceID)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "connector identity unavailable")
		return
	}
	request := r.Clone(withConnectorPrincipal(r.Context(), signed))
	api.connectorProxy.ServeHTTP(w, request)
}
