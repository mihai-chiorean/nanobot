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

func TestWorkEventStreamAdmissionDoesNotDependOnAcceptHeader(t *testing.T) {
	for _, test := range []struct {
		method string
		path   string
		want   bool
	}{
		{method: http.MethodGet, path: "/api/work/work_0123456789abcdef0123456789abcdef/events/stream", want: true},
		{method: http.MethodPost, path: "/api/work/work_0123456789abcdef0123456789abcdef/events/stream", want: false},
		{method: http.MethodGet, path: "/api/work/work_0123456789abcdef0123456789abcdef/events", want: false},
	} {
		req := httptest.NewRequest(test.method, test.path, nil)
		if got := isWorkEventStream(req); got != test.want {
			t.Fatalf("%s %s: got %v want %v", test.method, test.path, got, test.want)
		}
	}
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

func TestLegacyGuestRoutesAreBlocked(t *testing.T) {
	var requests atomic.Int32
	upstream := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {
		requests.Add(1)
	}))
	defer upstream.Close()
	handler := newTestHandler(t, upstream.URL, principalMiddleware(identity.Principal{}))

	for _, method := range []string{http.MethodGet, http.MethodPost, http.MethodPut, http.MethodPatch, http.MethodDelete} {
		for _, requestPath := range []string{
			"/webui/guest/bootstrap",
			"/api/guest",
			"/api/guest/join",
			"/api/guest/invite/child",
		} {
			response := httptest.NewRecorder()
			handler.ServeHTTP(response, httptest.NewRequest(method, requestPath, nil))
			if response.Code != http.StatusNotFound {
				t.Errorf("%s %s status = %d, want 404", method, requestPath, response.Code)
			}
		}
	}
	if requests.Load() != 0 {
		t.Errorf("upstream requests = %d, want 0", requests.Load())
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
		"/webui/guest/bootstrap",
		"/webui/guest/bootstrap/",
		"/api/guest",
		"/api/guest/join",
		"/api/guest/invite/child",
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
		for _, header := range []string{"Cookie", "Proxy-Authorization", "Forwarded", "X-Clerk-User", "X-Ziggy-Subject", "X-Ziggy-Email"} {
			if got := r.Header.Get(header); got != "" {
				t.Errorf("%s was forwarded: %q", header, got)
			}
		}
		if got := r.Header.Get("X-Forwarded-Host"); got == "evil.example" {
			t.Errorf("client X-Forwarded-Host was forwarded: %q", got)
		}
		if got := r.Header.Get("X-Forwarded-Proto"); got == "https" {
			t.Errorf("client X-Forwarded-Proto was forwarded: %q", got)
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
	request.Header.Set("X-Forwarded-Host", "evil.example")
	request.Header.Set("X-Forwarded-Proto", "https")
	request.Header.Set("Cookie", "session=secret")
	request.Header.Set("Proxy-Authorization", "Basic c2VjcmV0")
	request.Header.Set("Forwarded", "for=203.0.113.99")
	request.Header.Set("X-Clerk-User", "user-canary")
	request.Header.Set("X-Ziggy-Subject", "subject-canary")
	request.Header.Set("X-Ziggy-Email", "email-canary")
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

func TestInboundAdmissionIsBoundedByTrafficClassAndReleasesOnCancellation(t *testing.T) {
	for _, test := range []struct {
		name  string
		setup func(*http.Request)
		limit func(*inboundAdmission) *admissionGate
	}{
		{name: "http", limit: func(admission *inboundAdmission) *admissionGate { return &admission.http }},
		{name: "sse", setup: func(request *http.Request) { request.Header.Set("Accept", "text/event-stream") }, limit: func(admission *inboundAdmission) *admissionGate { return &admission.sse }},
		{name: "websocket", setup: func(request *http.Request) { request.Header.Set("Upgrade", "websocket") }, limit: func(admission *inboundAdmission) *admissionGate { return &admission.webSocket }},
	} {
		t.Run(test.name, func(t *testing.T) {
			admission := newInboundAdmission(1, 1, 1)
			var calls atomic.Int32
			handler := (&API{admission: admission}).admit(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if calls.Add(1) == 1 {
					<-r.Context().Done()
				}
				w.WriteHeader(http.StatusNoContent)
			}))

			ctx, cancel := context.WithCancel(context.Background())
			first := httptest.NewRequest(http.MethodGet, "/api", nil).WithContext(ctx)
			if test.setup != nil {
				test.setup(first)
			}
			done := make(chan struct{})
			go func() {
				handler.ServeHTTP(httptest.NewRecorder(), first)
				close(done)
			}()
			deadline := time.After(time.Second)
			for test.limit(admission).active.Load() != 1 {
				select {
				case <-deadline:
					t.Fatal("first request was not admitted")
				default:
					time.Sleep(time.Millisecond)
				}
			}

			second := httptest.NewRecorder()
			secondRequest := httptest.NewRequest(http.MethodGet, "/api", nil)
			if test.setup != nil {
				test.setup(secondRequest)
			}
			handler.ServeHTTP(second, secondRequest)
			if second.Code != http.StatusServiceUnavailable || second.Header().Get("Retry-After") != "1" {
				t.Fatalf("second request = %d, Retry-After %q", second.Code, second.Header().Get("Retry-After"))
			}

			cancel()
			select {
			case <-done:
			case <-time.After(time.Second):
				t.Fatal("canceled request did not release")
			}
			third := httptest.NewRecorder()
			thirdRequest := httptest.NewRequest(http.MethodGet, "/api", nil)
			if test.setup != nil {
				test.setup(thirdRequest)
			}
			handler.ServeHTTP(third, thirdRequest)
			if third.Code != http.StatusNoContent {
				t.Errorf("third request = %d, want 204", third.Code)
			}
		})
	}
}

func TestInboundAdmissionIsTenantFairWithinGlobalCap(t *testing.T) {
	admission := newInboundAdmissionWithTenantCount(4, 4, 4, 2)

	releases := make([]func(), 0, 4)
	for _, tenant := range []string{"tenant-a", "tenant-a", "tenant-b", "tenant-b"} {
		release, acquired := admission.tryAcquire(admissionHTTP, tenant)
		if !acquired {
			t.Fatalf("tenant %q was rejected before its share was full", tenant)
		}
		releases = append(releases, release)
	}
	if release, acquired := admission.tryAcquire(admissionHTTP, "tenant-a"); acquired {
		release()
		t.Fatal("tenant exceeded its fair share")
	}
	if release, acquired := admission.tryAcquire(admissionHTTP, "tenant-c"); acquired {
		release()
		t.Fatal("global cap was exceeded")
	}
	for _, release := range releases {
		release()
	}
}

func TestInboundAdmissionLimitsAnonymousTrafficSeparately(t *testing.T) {
	admission := newInboundAdmissionWithTenantCount(4, 4, 4, 1)

	first, acquired := admission.tryAcquire(admissionHTTP, anonymousTenant)
	if !acquired {
		t.Fatal("first anonymous request was rejected")
	}
	second, acquired := admission.tryAcquire(admissionHTTP, anonymousTenant)
	if !acquired {
		t.Fatal("second anonymous request was rejected")
	}
	defer first()
	defer second()
	if release, acquired := admission.tryAcquire(admissionHTTP, anonymousTenant); acquired {
		release()
		t.Fatal("anonymous traffic consumed the whole global budget")
	}
}

func TestHealthAndReadinessBypassInboundAdmission(t *testing.T) {
	admission := newInboundAdmission(1, 1, 1)
	handler := (&API{admission: admission}).admit(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusNoContent)
	}))
	admission.http.active.Store(1)
	for _, route := range []string{"/healthz", "/readyz"} {
		response := httptest.NewRecorder()
		handler.ServeHTTP(response, httptest.NewRequest(http.MethodGet, route, nil))
		if response.Code != http.StatusNoContent {
			t.Errorf("%s status = %d, want 204", route, response.Code)
		}
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
	if event["status"] != float64(http.StatusTeapot) {
		t.Errorf("access log outcome = %+v", event)
	}
	if event["bytes"] != float64(len("short and stout")) || event["route"] != "proxy" {
		t.Errorf("access log response fields = %+v", event)
	}
	for _, forbidden := range []string{"request_id", "trace_id", "path", "cf_ray"} {
		if _, ok := event[forbidden]; ok {
			t.Errorf("access log contains forbidden field %q: %+v", forbidden, event)
		}
	}
}

func TestRequestFailureLogsExcludeCredentialsAndIdentifiers(t *testing.T) {
	tests := []struct {
		name    string
		handler func(*testing.T, *slog.Logger) http.Handler
		request func() *http.Request
	}{
		{
			name: "authorization denial",
			handler: func(t *testing.T, logger *slog.Logger) http.Handler {
				upstream := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {
					t.Fatal("denied request reached upstream")
				}))
				t.Cleanup(upstream.Close)
				return newTestHandlerWithLogger(t, upstream.URL, principalMiddleware(identity.Principal{
					Subject: "user-canary",
					Email:   "email-canary@example.com",
				}), logger, 64<<20)
			},
			request: func() *http.Request {
				request := httptest.NewRequest(http.MethodGet, "/auth/bootstrap?token=query-canary", nil)
				request.Header.Set("Authorization", "Bearer token-canary")
				return request
			},
		},
		{
			name: "proxy failure",
			handler: func(t *testing.T, logger *slog.Logger) http.Handler {
				upstream := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {}))
				upstreamURL := upstream.URL
				upstream.Close()
				return newTestHandlerWithLogger(t, upstreamURL, principalMiddleware(identity.Principal{}), logger, 64<<20)
			},
			request: func() *http.Request {
				request := httptest.NewRequest(http.MethodGet, "/api/sessions/session-canary/messages?token=query-canary", nil)
				request.Header.Set("Authorization", "Bearer token-canary")
				return request
			},
		},
	}

	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			var logs bytes.Buffer
			logger := slog.New(slog.NewJSONHandler(&logs, nil))
			handler := test.handler(t, logger)
			response := httptest.NewRecorder()
			handler.ServeHTTP(response, test.request())

			serialized := logs.String()
			for _, forbidden := range []string{
				"user-canary", "email-canary@example.com", "session-canary",
				"token-canary", "query-canary",
				"request_id", "trace_id", "127.0.0.1:",
			} {
				if strings.Contains(serialized, forbidden) {
					t.Errorf("logs contain forbidden value %q: %s", forbidden, serialized)
				}
			}
		})
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
		if got := r.URL.Query().Get("token"); got != "nanobot-token" {
			t.Errorf("upstream WebSocket token = %q", got)
		}
		if got := r.Header.Get("Authorization"); got != "" {
			t.Errorf("upstream Authorization = %q, want empty", got)
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
		"GET / HTTP/1.1\r\nHost: %s\r\nConnection: Upgrade\r\nUpgrade: websocket\r\nAuthorization: Bearer nanobot-token\r\n\r\n",
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
		Proxy:          NewReverseProxy(target, logger, nil),
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
