package httpapi

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/mihai-chiorean/nanobot/services/ziggy-control/internal/identity"
)

type checkerFunc func(context.Context) error

type writerFunc func([]byte) (int, error)

func (write writerFunc) Write(body []byte) (int, error) {
	return write(body)
}

func (check checkerFunc) Check(ctx context.Context) error {
	return check(ctx)
}

func TestBootstrapAuthenticatesOwnerAndPreservesAuthorization(t *testing.T) {
	var requests atomic.Int32
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requests.Add(1)
		if r.URL.Path != "/auth/bootstrap" {
			t.Errorf("path = %q", r.URL.Path)
		}
		if got := r.Header.Get("Authorization"); got != "Bearer clerk-token" {
			t.Errorf("Authorization = %q", got)
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, `{"token":"nanobot-token","ws_path":"/"}`)
	}))
	defer upstream.Close()

	handler := newTestHandler(t, upstream.URL, principalMiddleware(identity.Principal{
		Subject: "user_123",
		Email:   "owner@example.com",
	}))
	request := httptest.NewRequest(http.MethodGet, "/auth/bootstrap", nil)
	request.Header.Set("Authorization", "Bearer clerk-token")
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, request)

	if response.Code != http.StatusOK {
		t.Fatalf("status = %d, body = %s", response.Code, response.Body.String())
	}
	if requests.Load() != 1 {
		t.Errorf("upstream requests = %d, want 1", requests.Load())
	}
	if !strings.Contains(response.Body.String(), `"nanobot-token"`) {
		t.Errorf("response body = %q", response.Body.String())
	}
}

func TestBootstrapRejectsUnauthorizedAndNonOwner(t *testing.T) {
	var requests atomic.Int32
	upstream := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {
		requests.Add(1)
	}))
	defer upstream.Close()

	tests := []struct {
		name       string
		middleware Middleware
		status     int
	}{
		{
			name: "unauthenticated",
			middleware: func(http.Handler) http.Handler {
				return http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
					writeError(w, http.StatusUnauthorized, "authentication required")
				})
			},
			status: http.StatusUnauthorized,
		},
		{
			name:       "wrong email",
			middleware: principalMiddleware(identity.Principal{Subject: "user_456", Email: "other@example.com"}),
			status:     http.StatusForbidden,
		},
		{
			name:       "wrong pinned subject",
			middleware: principalMiddleware(identity.Principal{Subject: "user_456", Email: "owner@example.com"}),
			status:     http.StatusForbidden,
		},
	}

	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			handler := newTestHandler(t, upstream.URL, test.middleware)
			response := httptest.NewRecorder()
			handler.ServeHTTP(response, httptest.NewRequest(http.MethodGet, "/auth/bootstrap", nil))
			if response.Code != test.status {
				t.Errorf("status = %d, want %d", response.Code, test.status)
			}
		})
	}
	if requests.Load() != 0 {
		t.Errorf("upstream requests = %d, want 0", requests.Load())
	}
}

func TestPrivateRoutesAreNotProxied(t *testing.T) {
	var requests atomic.Int32
	upstream := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {
		requests.Add(1)
	}))
	defer upstream.Close()
	handler := newTestHandler(t, upstream.URL, principalMiddleware(identity.Principal{}))

	for _, requestPath := range []string{
		"/webui/bootstrap",
		"/webui/bootstrap/",
		"/auth/token",
		"/auth/token/private",
		"/auth/bootstrap/",
		"/healthz/",
		"/readyz/",
	} {
		response := httptest.NewRecorder()
		handler.ServeHTTP(response, httptest.NewRequest(http.MethodGet, requestPath, nil))
		if response.Code != http.StatusNotFound {
			t.Errorf("%s status = %d, want 404", requestPath, response.Code)
		}
	}
	if requests.Load() != 0 {
		t.Errorf("upstream requests = %d, want 0", requests.Load())
	}
}

func TestOrdinaryRequestsAreProxiedWithNewRequestID(t *testing.T) {
	var upstreamRequestID string
	var forwardedFor string
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		upstreamRequestID = r.Header.Get("X-Request-ID")
		forwardedFor = r.Header.Get("X-Forwarded-For")
		if got := r.Header.Get("Authorization"); got != "Bearer nanobot-token" {
			t.Errorf("Authorization = %q", got)
		}
		if got := r.Header.Get("X-Nanobot-Auth"); got != "" {
			t.Errorf("X-Nanobot-Auth was forwarded: %q", got)
		}
		w.WriteHeader(http.StatusNoContent)
	}))
	defer upstream.Close()
	handler := newTestHandler(t, upstream.URL, principalMiddleware(identity.Principal{}))

	request := httptest.NewRequest(http.MethodGet, "/api/conversations?limit=5", nil)
	request.Header.Set("Authorization", "Bearer nanobot-token")
	request.Header.Set("X-Request-ID", "untrusted")
	request.Header.Set("X-Nanobot-Auth", "spoofed")
	request.Header.Set("X-Forwarded-For", "203.0.113.99")
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, request)

	if response.Code != http.StatusNoContent {
		t.Errorf("status = %d, want 204", response.Code)
	}
	responseRequestID := response.Header().Get("X-Request-ID")
	if responseRequestID == "" || responseRequestID == "untrusted" {
		t.Errorf("response request ID = %q", responseRequestID)
	}
	if upstreamRequestID != responseRequestID {
		t.Errorf("upstream request ID = %q, response = %q", upstreamRequestID, responseRequestID)
	}
	if strings.Contains(forwardedFor, "203.0.113.99") {
		t.Errorf("spoofed X-Forwarded-For was forwarded: %q", forwardedFor)
	}
}

func TestOversizedRequestBodyIsRejected(t *testing.T) {
	var requests atomic.Int32
	upstream := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {
		requests.Add(1)
	}))
	defer upstream.Close()
	handler := newTestHandlerWithLogger(
		t,
		upstream.URL,
		principalMiddleware(identity.Principal{}),
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		8,
	)

	request := httptest.NewRequest(http.MethodPost, "/api/documents", strings.NewReader("too many bytes"))
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, request)

	if response.Code != http.StatusRequestEntityTooLarge {
		t.Errorf("status = %d, want 413", response.Code)
	}
	if requests.Load() != 0 {
		t.Errorf("upstream requests = %d, want 0", requests.Load())
	}
}

func TestAccessLogIncludesRequestOutcome(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusTeapot)
		_, _ = io.WriteString(w, "short and stout")
	}))
	defer upstream.Close()
	var logs bytes.Buffer
	logger := slog.New(slog.NewJSONHandler(&logs, nil))
	handler := newTestHandlerWithLogger(
		t,
		upstream.URL,
		principalMiddleware(identity.Principal{}),
		logger,
		64<<20,
	)

	response := httptest.NewRecorder()
	handler.ServeHTTP(response, httptest.NewRequest(http.MethodGet, "/api/conversations", nil))

	var event map[string]any
	if err := json.Unmarshal(logs.Bytes(), &event); err != nil {
		t.Fatalf("decode access log: %v; log = %s", err, logs.String())
	}
	if event["request_id"] == "" || event["status"] != float64(http.StatusTeapot) {
		t.Errorf("access log identity/outcome = %+v", event)
	}
	if event["bytes"] != float64(len("short and stout")) || event["route"] != "proxy" {
		t.Errorf("access log response fields = %+v", event)
	}
}

func TestServerSentEventsFlushBeforeUpstreamCompletes(t *testing.T) {
	release := make(chan struct{})
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		_, _ = io.WriteString(w, "data: first\n\n")
		w.(http.Flusher).Flush()
		<-release
		_, _ = io.WriteString(w, "data: second\n\n")
	}))
	defer upstream.Close()
	defer close(release)

	handler := newTestHandler(t, upstream.URL, principalMiddleware(identity.Principal{}))
	frontDoor := httptest.NewServer(handler)
	defer frontDoor.Close()

	client := &http.Client{Timeout: 2 * time.Second}
	response, err := client.Get(frontDoor.URL + "/api/events")
	if err != nil {
		t.Fatalf("GET event stream: %v", err)
	}
	defer response.Body.Close()

	line, err := bufio.NewReader(response.Body).ReadString('\n')
	if err != nil {
		t.Fatalf("read first event: %v", err)
	}
	if line != "data: first\n" {
		t.Errorf("first line = %q", line)
	}
}

func TestWebSocketUpgradePassesThrough(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if !strings.EqualFold(r.Header.Get("Upgrade"), "websocket") {
			t.Errorf("Upgrade = %q", r.Header.Get("Upgrade"))
		}
		connection, buffer, err := w.(http.Hijacker).Hijack()
		if err != nil {
			t.Errorf("hijack: %v", err)
			return
		}
		defer connection.Close()
		_, _ = fmt.Fprint(buffer, "HTTP/1.1 101 Switching Protocols\r\nConnection: Upgrade\r\nUpgrade: websocket\r\n\r\n")
		_ = buffer.Flush()
	}))
	defer upstream.Close()

	logEvents := make(chan []byte, 1)
	logger := slog.New(slog.NewJSONHandler(writerFunc(func(body []byte) (int, error) {
		logEvents <- bytes.Clone(body)
		return len(body), nil
	}), nil))
	handler := newTestHandlerWithLogger(
		t,
		upstream.URL,
		principalMiddleware(identity.Principal{}),
		logger,
		64<<20,
	)
	frontDoor := httptest.NewServer(handler)
	defer frontDoor.Close()
	frontURL, err := url.Parse(frontDoor.URL)
	if err != nil {
		t.Fatal(err)
	}

	connection, err := net.DialTimeout("tcp", frontURL.Host, time.Second)
	if err != nil {
		t.Fatalf("dial front door: %v", err)
	}
	defer connection.Close()
	_ = connection.SetDeadline(time.Now().Add(2 * time.Second))
	_, _ = fmt.Fprintf(connection,
		"GET /?token=nanobot-token HTTP/1.1\r\nHost: %s\r\nConnection: Upgrade\r\nUpgrade: websocket\r\n\r\n",
		frontURL.Host,
	)

	status, err := bufio.NewReader(connection).ReadString('\n')
	if err != nil {
		t.Fatalf("read upgrade response: %v", err)
	}
	if status != "HTTP/1.1 101 Switching Protocols\r\n" {
		t.Errorf("status line = %q", status)
	}
	_ = connection.Close()

	select {
	case rawEvent := <-logEvents:
		var event map[string]any
		if err := json.Unmarshal(rawEvent, &event); err != nil {
			t.Fatalf("decode WebSocket access log: %v", err)
		}
		if event["status"] != float64(http.StatusSwitchingProtocols) {
			t.Errorf("WebSocket access log status = %v, want 101", event["status"])
		}
	case <-time.After(2 * time.Second):
		t.Fatal("timed out waiting for WebSocket access log")
	}
}

func TestHealthAndReadiness(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {}))
	defer upstream.Close()
	handler := newTestHandler(t, upstream.URL, principalMiddleware(identity.Principal{}))

	for _, route := range []string{"/healthz", "/readyz"} {
		response := httptest.NewRecorder()
		handler.ServeHTTP(response, httptest.NewRequest(http.MethodGet, route, nil))
		if response.Code != http.StatusOK {
			t.Errorf("%s status = %d, body = %s", route, response.Code, response.Body.String())
		}
	}
}

func newTestHandler(t *testing.T, upstreamURL string, authenticate Middleware) http.Handler {
	t.Helper()
	return newTestHandlerWithLogger(
		t,
		upstreamURL,
		authenticate,
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		64<<20,
	)
}

func newTestHandlerWithLogger(
	t *testing.T,
	upstreamURL string,
	authenticate Middleware,
	logger *slog.Logger,
	maxRequestBody int64,
) http.Handler {
	t.Helper()
	target, err := url.Parse(upstreamURL)
	if err != nil {
		t.Fatal(err)
	}
	handler, err := New(Config{
		Authenticate:   authenticate,
		Proxy:          NewReverseProxy(target, logger),
		Readiness:      checkerFunc(func(context.Context) error { return nil }),
		Logger:         logger,
		OwnerEmail:     "owner@example.com",
		OwnerSubject:   "user_123",
		MaxRequestBody: maxRequestBody,
		BlockedPaths: map[string]struct{}{
			"/webui/bootstrap": {},
			"/auth/token":      {},
		},
		Version: "test",
	})
	if err != nil {
		t.Fatalf("New() error = %v", err)
	}
	return handler
}

func principalMiddleware(principal identity.Principal) Middleware {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			next.ServeHTTP(w, r.WithContext(identity.NewContext(r.Context(), principal)))
		})
	}
}
