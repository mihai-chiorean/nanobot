package httpapi

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"errors"
	"log/slog"
	"net/http"
	"net/http/httputil"
	"net/url"
	"strings"
	"time"

	"github.com/mihai-chiorean/nanobot/services/ziggy-control/internal/telemetry"
)

const workPrincipalTTL = time.Minute

type WorkSigner struct {
	key []byte
	now func() time.Time
}

type workPrincipal struct {
	UserID      string `json:"user_id"`
	WorkspaceID string `json:"workspace_id"`
	ExpiresAt   int64  `json:"expires_at"`
}

type signedWorkPrincipal struct {
	payload   string
	signature string
}

type workPrincipalKey struct{}

type WorkProxy struct {
	proxy *httputil.ReverseProxy
}

func NewWorkSigner(key []byte) (*WorkSigner, error) {
	if len(key) < 32 {
		return nil, errors.New("work trust key must contain at least 32 bytes")
	}
	return &WorkSigner{key: append([]byte(nil), key...), now: time.Now}, nil
}

func (signer *WorkSigner) sign(userID, workspaceID string) (signedWorkPrincipal, error) {
	if strings.TrimSpace(userID) == "" || strings.TrimSpace(workspaceID) == "" {
		return signedWorkPrincipal{}, errors.New("work principal is incomplete")
	}
	payload, err := json.Marshal(workPrincipal{
		UserID:      userID,
		WorkspaceID: workspaceID,
		ExpiresAt:   signer.now().Add(workPrincipalTTL).Unix(),
	})
	if err != nil {
		return signedWorkPrincipal{}, err
	}
	encoded := base64.RawURLEncoding.EncodeToString(payload)
	mac := hmac.New(sha256.New, signer.key)
	_, _ = mac.Write([]byte(encoded))
	return signedWorkPrincipal{
		payload:   encoded,
		signature: base64.RawURLEncoding.EncodeToString(mac.Sum(nil)),
	}, nil
}

func withWorkPrincipal(ctx context.Context, principal signedWorkPrincipal) context.Context {
	return context.WithValue(ctx, workPrincipalKey{}, principal)
}

func workPrincipalFromContext(ctx context.Context) (signedWorkPrincipal, bool) {
	principal, ok := ctx.Value(workPrincipalKey{}).(signedWorkPrincipal)
	return principal, ok
}

func NewWorkProxy(target *url.URL, logger *slog.Logger, observability *telemetry.Recorder) *WorkProxy {
	return &WorkProxy{proxy: newReverseProxy(target, logger, observability, proxyKindWork)}
}

func NewWorkReverseProxy(target *url.URL, logger *slog.Logger, observability *telemetry.Recorder) *httputil.ReverseProxy {
	return NewWorkProxy(target, logger, observability).proxy
}

func (proxy *WorkProxy) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	proxy.proxy.ServeHTTP(w, r)
}
