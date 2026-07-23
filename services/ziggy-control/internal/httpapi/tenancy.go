package httpapi

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/mihai-chiorean/nanobot/services/ziggy-control/internal/identity"
)

const maxBootstrapResponseBytes = 1 << 20

type TenantRoute struct {
	UserID                  string
	WorkspaceID             string
	UpstreamBootstrapSecret string
	Proxy                   http.Handler
}

type TenantRouter interface {
	ResolvePrincipal(context.Context, identity.Principal) (TenantRoute, error)
	ResolveCredential(string) (TenantRoute, bool)
	RememberCredentials(TenantRoute, []string, time.Duration) error
	Default() TenantRoute
}

type runtimeCredentialRouter interface {
	ResolveRuntimeCredential(string) (TenantRoute, bool)
}

type capturedResponse struct {
	header   http.Header
	body     bytes.Buffer
	status   int
	overflow bool
}

func newCapturedResponse() *capturedResponse {
	return &capturedResponse{header: make(http.Header)}
}

func (response *capturedResponse) Header() http.Header {
	return response.header
}

func (response *capturedResponse) WriteHeader(status int) {
	if response.status == 0 {
		response.status = status
	}
}

func (response *capturedResponse) Write(body []byte) (int, error) {
	if response.status == 0 {
		response.status = http.StatusOK
	}
	if response.body.Len()+len(body) > maxBootstrapResponseBytes {
		response.overflow = true
		return 0, errors.New("bootstrap response exceeds limit")
	}
	return response.body.Write(body)
}

func (response *capturedResponse) send(w http.ResponseWriter) {
	for key, values := range response.header {
		for _, value := range values {
			w.Header().Add(key, value)
		}
	}
	status := response.status
	if status == 0 {
		status = http.StatusOK
	}
	w.WriteHeader(status)
	_, _ = w.Write(response.body.Bytes())
}

func captureBootstrap(proxy http.Handler, request *http.Request) (*capturedResponse, []string, time.Duration, error) {
	response := newCapturedResponse()
	proxy.ServeHTTP(response, request)
	if response.overflow {
		return response, nil, 0, errors.New("bootstrap response too large")
	}
	if response.status < http.StatusOK || response.status >= http.StatusMultipleChoices {
		return response, nil, 0, nil
	}

	var payload map[string]json.RawMessage
	if err := json.Unmarshal(response.body.Bytes(), &payload); err != nil {
		return response, nil, 0, errors.New("bootstrap response is not JSON")
	}
	credentials := make([]string, 0, 2)
	seen := make(map[string]struct{}, 2)
	for _, key := range []string{"token", "rest_token", "restToken", "access_token", "session_token", "ws_token", "websocket_token", "webSocketToken"} {
		raw, ok := payload[key]
		if !ok {
			continue
		}
		var credential string
		if err := json.Unmarshal(raw, &credential); err != nil {
			continue
		}
		credential = strings.TrimSpace(credential)
		if credential == "" {
			continue
		}
		if _, exists := seen[credential]; exists {
			continue
		}
		seen[credential] = struct{}{}
		credentials = append(credentials, credential)
	}
	if len(credentials) == 0 {
		return response, nil, 0, errors.New("bootstrap response has no transport credential")
	}

	ttl := 5 * time.Minute
	if raw, ok := payload["expires_in"]; ok {
		var seconds json.Number
		decoder := json.NewDecoder(bytes.NewReader(raw))
		decoder.UseNumber()
		if err := decoder.Decode(&seconds); err == nil {
			if value, err := strconv.ParseInt(seconds.String(), 10, 64); err == nil && value > 0 {
				ttl = time.Duration(value) * time.Second
			}
		}
	}
	return response, credentials, ttl, nil
}

func requestCredential(request *http.Request) string {
	if credential, _, valid := bearerCredential(request.Header.Get("Authorization")); valid {
		return credential
	}
	return strings.TrimSpace(request.URL.Query().Get("token"))
}
