package httpapi

import (
	"bufio"
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"mime"
	"net"
	"net/http"
	"net/url"
	"path"
	"strings"
	"sync/atomic"
	"time"

	"github.com/mihai-chiorean/nanobot/services/ziggy-control/internal/identity"
	"github.com/mihai-chiorean/nanobot/services/ziggy-control/internal/telemetry"
)

type Middleware func(http.Handler) http.Handler

type Config struct {
	Authenticate      Middleware
	Proxy             http.Handler
	Readiness         ReadinessChecker
	Logger            *slog.Logger
	Telemetry         *telemetry.Recorder
	OwnerEmail        string
	OwnerSubject      string
	BlockedPaths      map[string]struct{}
	MaxRequestBody    int64
	HTTPInFlight      int64
	SSEInFlight       int64
	WebSocketInFlight int64
	Version           string
}

type API struct {
	proxy          http.Handler
	readiness      ReadinessChecker
	logger         *slog.Logger
	telemetry      *telemetry.Recorder
	ownerEmail     string
	ownerSubject   string
	blockedPaths   map[string]struct{}
	maxRequestBody int64
	admission      *inboundAdmission
	version        string
}

type requestIDKey struct{}

const maxEnrollmentBody = 4 << 10

const (
	defaultHTTPInFlight      = int64(64)
	defaultSSEInFlight       = int64(8)
	defaultWebSocketInFlight = int64(8)
)

type admissionClass uint8

const (
	admissionHTTP admissionClass = iota
	admissionSSE
	admissionWebSocket
)

type admissionGate struct {
	limit  int64
	active atomic.Int64
}

func (gate *admissionGate) tryAcquire() (func(), bool) {
	for {
		current := gate.active.Load()
		if current >= gate.limit {
			return nil, false
		}
		if gate.active.CompareAndSwap(current, current+1) {
			var released atomic.Bool
			return func() {
				if released.CompareAndSwap(false, true) {
					gate.active.Add(-1)
				}
			}, true
		}
	}
}

type inboundAdmission struct {
	http      admissionGate
	sse       admissionGate
	webSocket admissionGate
}

func newInboundAdmission(httpLimit, sseLimit, webSocketLimit int64) *inboundAdmission {
	if httpLimit <= 0 {
		httpLimit = defaultHTTPInFlight
	}
	if sseLimit <= 0 {
		sseLimit = defaultSSEInFlight
	}
	if webSocketLimit <= 0 {
		webSocketLimit = defaultWebSocketInFlight
	}
	return &inboundAdmission{
		http:      admissionGate{limit: httpLimit},
		sse:       admissionGate{limit: sseLimit},
		webSocket: admissionGate{limit: webSocketLimit},
	}
}

func New(config Config) (http.Handler, error) {
	if config.Authenticate == nil {
		return nil, errors.New("authentication middleware is required")
	}
	if config.Proxy == nil {
		return nil, errors.New("proxy handler is required")
	}
	if config.Readiness == nil {
		return nil, errors.New("readiness checker is required")
	}
	if config.Logger == nil {
		return nil, errors.New("logger is required")
	}
	if strings.TrimSpace(config.OwnerEmail) == "" {
		return nil, errors.New("owner email is required")
	}
	if config.MaxRequestBody <= 0 {
		return nil, errors.New("maximum request body must be positive")
	}

	api := &API{
		proxy:          config.Proxy,
		readiness:      config.Readiness,
		logger:         config.Logger,
		telemetry:      config.Telemetry,
		ownerEmail:     strings.ToLower(strings.TrimSpace(config.OwnerEmail)),
		ownerSubject:   strings.TrimSpace(config.OwnerSubject),
		blockedPaths:   cloneBlockedPaths(config.BlockedPaths),
		maxRequestBody: config.MaxRequestBody,
		admission:      newInboundAdmission(config.HTTPInFlight, config.SSEInFlight, config.WebSocketInFlight),
		version:        config.Version,
	}
	if api.telemetry == nil {
		api.telemetry = telemetry.Noop()
	}
	for _, reserved := range []string{"/auth/bootstrap", "/webui/guest/bootstrap", "/healthz", "/readyz"} {
		api.blockedPaths[reserved] = struct{}{}
	}

	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", api.health)
	mux.HandleFunc("/readyz", api.ready)
	mux.Handle("/auth/bootstrap", config.Authenticate(http.HandlerFunc(api.bootstrap)))
	mux.HandleFunc("/webui/guest/bootstrap", api.enroll)
	mux.HandleFunc("/", api.forward)
	return api.assignRequestID(api.observe(api.admit(mux))), nil
}

func (api *API) admit(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/healthz" || r.URL.Path == "/readyz" {
			next.ServeHTTP(w, r)
			return
		}

		gate := &api.admission.http
		switch {
		case isWebSocketUpgrade(r):
			gate = &api.admission.webSocket
		case acceptsSSE(r):
			gate = &api.admission.sse
		}
		release, acquired := gate.tryAcquire()
		if !acquired {
			w.Header().Set("Retry-After", "1")
			writeError(w, http.StatusServiceUnavailable, "inbound capacity unavailable")
			return
		}
		defer release()
		next.ServeHTTP(w, r)
	})
}

func acceptsSSE(r *http.Request) bool {
	return strings.Contains(strings.ToLower(r.Header.Get("Accept")), "text/event-stream")
}

func (api *API) health(w http.ResponseWriter, r *http.Request) {
	if !allowReadMethod(w, r) {
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{
		"status":  "ok",
		"service": "ziggy-control",
		"version": api.version,
	})
}

func (api *API) ready(w http.ResponseWriter, r *http.Request) {
	if !allowReadMethod(w, r) {
		return
	}
	if err := api.readiness.Check(r.Context()); err != nil {
		api.logger.WarnContext(r.Context(), "readiness check failed",
			"route", "readiness",
			"error_class", requestErrorClass(err),
		)
		writeError(w, http.StatusServiceUnavailable, "upstream unavailable")
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"status": "ready"})
}

func (api *API) bootstrap(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		w.Header().Set("Allow", http.MethodGet)
		writeError(w, http.StatusMethodNotAllowed, "method not allowed")
		return
	}
	principal, ok := identity.FromContext(r.Context())
	if !ok || !api.isOwner(principal) {
		api.logger.WarnContext(r.Context(), "bootstrap authorization denied", "route", "bootstrap")
		writeError(w, http.StatusForbidden, "account not authorized")
		return
	}
	api.logger.InfoContext(r.Context(), "bootstrap authorized", "route", "bootstrap")
	api.proxy.ServeHTTP(w, r)
}

func (api *API) enroll(w http.ResponseWriter, r *http.Request) {
	writer := noStoreResponseWriter{ResponseWriter: w}
	credential, status, err := enrollmentCredential(&writer, r)
	if err != nil {
		writeError(&writer, status, err.Error())
		return
	}
	if r.Method == http.MethodGet {
		writer.Header().Set("Deprecation", "true")
	}

	request := r.Clone(r.Context())
	requestURL := *r.URL
	requestURL.RawQuery = url.Values{"code": []string{credential}}.Encode()
	request.URL = &requestURL
	request.Method = http.MethodGet
	request.Body = http.NoBody
	request.ContentLength = 0
	request.GetBody = nil
	request.Header.Del("Authorization")
	request.Header.Del("Content-Type")
	request.Header.Del("Content-Length")
	api.proxy.ServeHTTP(&writer, request)
}

func (api *API) forward(w http.ResponseWriter, r *http.Request) {
	if api.isBlocked(r.URL.Path) {
		http.NotFound(w, r)
		return
	}
	if isWebSocketUpgrade(r) {
		request, status, err := webSocketRequest(r)
		if err != nil {
			writeError(w, status, err.Error())
			return
		}
		r = request
	} else if r.Body != nil {
		if r.ContentLength > api.maxRequestBody {
			writeError(w, http.StatusRequestEntityTooLarge, "request body too large")
			return
		}
		r.Body = http.MaxBytesReader(w, r.Body, api.maxRequestBody)
	}
	api.proxy.ServeHTTP(w, r)
}

func enrollmentCredential(w http.ResponseWriter, r *http.Request) (string, int, error) {
	if r.Method != http.MethodGet && r.Method != http.MethodPost {
		w.Header().Set("Allow", "GET, POST")
		return "", http.StatusMethodNotAllowed, errors.New("method not allowed")
	}
	headerCredential, headerPresent, headerValid := bearerCredential(r.Header.Get("Authorization"))
	if headerPresent && !headerValid {
		return "", http.StatusUnauthorized, errors.New("valid enrollment credential required")
	}

	var supplied []string
	if r.Method == http.MethodGet {
		supplied = append(supplied, r.URL.Query()["code"]...)
		supplied = append(supplied, r.URL.Query()["join_code"]...)
	} else {
		mediaType := "application/x-www-form-urlencoded"
		if contentType := strings.TrimSpace(r.Header.Get("Content-Type")); contentType != "" {
			parsedType, _, err := mime.ParseMediaType(contentType)
			if err != nil || parsedType != mediaType {
				return "", http.StatusUnsupportedMediaType, errors.New("form-encoded enrollment body required")
			}
		}
		body, err := io.ReadAll(http.MaxBytesReader(w, r.Body, maxEnrollmentBody))
		if err != nil {
			var tooLarge *http.MaxBytesError
			if errors.As(err, &tooLarge) {
				return "", http.StatusRequestEntityTooLarge, errors.New("enrollment body too large")
			}
			return "", http.StatusBadRequest, errors.New("invalid enrollment body")
		}
		form, err := url.ParseQuery(string(body))
		if err != nil {
			return "", http.StatusBadRequest, errors.New("invalid enrollment body")
		}
		supplied = append(supplied, form["code"]...)
		supplied = append(supplied, form["join_code"]...)
	}
	if headerCredential != "" {
		supplied = append(supplied, headerCredential)
	}

	credential := ""
	for _, candidate := range supplied {
		candidate = strings.TrimSpace(candidate)
		if candidate == "" {
			continue
		}
		if len(candidate) > 512 {
			return "", http.StatusBadRequest, errors.New("invalid enrollment credential")
		}
		if credential != "" && candidate != credential {
			return "", http.StatusBadRequest, errors.New("conflicting enrollment credentials")
		}
		credential = candidate
	}
	if credential == "" {
		return "", http.StatusUnauthorized, errors.New("valid enrollment credential required")
	}
	return credential, 0, nil
}

func webSocketRequest(r *http.Request) (*http.Request, int, error) {
	headerToken, present, valid := bearerCredential(r.Header.Get("Authorization"))
	if !present {
		return r, 0, nil
	}
	if !valid {
		return nil, http.StatusUnauthorized, errors.New("valid WebSocket credential required")
	}
	query := r.URL.Query()
	if queryToken := strings.TrimSpace(query.Get("token")); queryToken != "" && queryToken != headerToken {
		return nil, http.StatusBadRequest, errors.New("conflicting WebSocket credentials")
	}
	query.Set("token", headerToken)
	request := r.Clone(r.Context())
	requestURL := *r.URL
	requestURL.RawQuery = query.Encode()
	request.URL = &requestURL
	request.Header.Del("Authorization")
	return request, 0, nil
}

func bearerCredential(value string) (credential string, present, valid bool) {
	value = strings.TrimSpace(value)
	if value == "" {
		return "", false, false
	}
	scheme, credential, found := strings.Cut(value, " ")
	credential = strings.TrimSpace(credential)
	if !found || !strings.EqualFold(scheme, "Bearer") || credential == "" {
		return "", true, false
	}
	return credential, true, true
}

func isWebSocketUpgrade(r *http.Request) bool {
	return strings.EqualFold(strings.TrimSpace(r.Header.Get("Upgrade")), "websocket")
}

func (api *API) isOwner(principal identity.Principal) bool {
	if !strings.EqualFold(strings.TrimSpace(principal.Email), api.ownerEmail) {
		return false
	}
	return api.ownerSubject == "" || principal.Subject == api.ownerSubject
}

func (api *API) isBlocked(requestPath string) bool {
	cleaned := path.Clean("/" + strings.TrimPrefix(requestPath, "/"))
	for blocked := range api.blockedPaths {
		if cleaned == blocked || strings.HasPrefix(cleaned, strings.TrimSuffix(blocked, "/")+"/") {
			return true
		}
	}
	return false
}

func cloneBlockedPaths(source map[string]struct{}) map[string]struct{} {
	result := make(map[string]struct{}, len(source)+3)
	for blocked := range source {
		result[blocked] = struct{}{}
	}
	return result
}

func (api *API) assignRequestID(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requestID, err := newRequestID()
		if err != nil {
			api.logger.ErrorContext(r.Context(), "request ID generation failed", "error_class", "entropy_unavailable")
			writeError(w, http.StatusInternalServerError, "internal error")
			return
		}

		request := r.Clone(context.WithValue(r.Context(), requestIDKey{}, requestID))
		request.Header.Set("X-Request-ID", requestID)
		w.Header().Set("X-Request-ID", requestID)
		next.ServeHTTP(w, request)
	})
}

func (api *API) observe(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		started := time.Now()
		ctx, finishTelemetry := api.telemetry.StartRequest(r.Context(), r.Header, routeName(r.URL.Path), r.Method)
		request := r.WithContext(ctx)
		observed := &observedResponseWriter{ResponseWriter: w}
		next.ServeHTTP(observed, request)
		duration := time.Since(started)
		status := observed.statusCode()
		finishTelemetry(status, observed.bytes)
		api.logger.InfoContext(request.Context(), "request completed",
			"method", requestMethod(request.Method),
			"route", routeName(request.URL.Path),
			"status", status,
			"bytes", observed.bytes,
			"duration_ms", float64(duration)/float64(time.Millisecond),
		)
	})
}

func requestMethod(method string) string {
	switch strings.ToUpper(strings.TrimSpace(method)) {
	case http.MethodGet, http.MethodHead, http.MethodPost, http.MethodPut,
		http.MethodPatch, http.MethodDelete, http.MethodOptions, http.MethodConnect,
		http.MethodTrace:
		return strings.ToUpper(strings.TrimSpace(method))
	default:
		return "OTHER"
	}
}

type observedResponseWriter struct {
	http.ResponseWriter
	status int
	bytes  int64
}

func (w *observedResponseWriter) Unwrap() http.ResponseWriter {
	return w.ResponseWriter
}

func (w *observedResponseWriter) Hijack() (net.Conn, *bufio.ReadWriter, error) {
	hijacker, ok := w.ResponseWriter.(http.Hijacker)
	if !ok {
		return nil, nil, errors.New("response writer does not support hijacking")
	}
	connection, buffer, err := hijacker.Hijack()
	if err == nil && w.status == 0 {
		w.status = http.StatusSwitchingProtocols
	}
	return connection, buffer, err
}

func (w *observedResponseWriter) WriteHeader(status int) {
	if w.status != 0 {
		return
	}
	w.status = status
	w.ResponseWriter.WriteHeader(status)
}

func (w *observedResponseWriter) Write(body []byte) (int, error) {
	if w.status == 0 {
		w.status = http.StatusOK
	}
	written, err := w.ResponseWriter.Write(body)
	w.bytes += int64(written)
	return written, err
}

func (w *observedResponseWriter) statusCode() int {
	if w.status == 0 {
		return http.StatusOK
	}
	return w.status
}

func routeName(requestPath string) string {
	switch requestPath {
	case "/healthz":
		return "health"
	case "/readyz":
		return "readiness"
	case "/auth/bootstrap":
		return "bootstrap"
	case "/webui/guest/bootstrap":
		return "enrollment"
	default:
		return "proxy"
	}
}

type noStoreResponseWriter struct {
	http.ResponseWriter
}

func (w noStoreResponseWriter) Unwrap() http.ResponseWriter {
	return w.ResponseWriter
}

func (w noStoreResponseWriter) WriteHeader(status int) {
	w.Header().Set("Cache-Control", "no-store")
	w.ResponseWriter.WriteHeader(status)
}

func (w noStoreResponseWriter) Write(body []byte) (int, error) {
	w.Header().Set("Cache-Control", "no-store")
	return w.ResponseWriter.Write(body)
}

func RequestID(ctx context.Context) string {
	requestID, _ := ctx.Value(requestIDKey{}).(string)
	return requestID
}

func newRequestID() (string, error) {
	buffer := make([]byte, 16)
	if _, err := rand.Read(buffer); err != nil {
		return "", err
	}
	return hex.EncodeToString(buffer), nil
}

func allowReadMethod(w http.ResponseWriter, r *http.Request) bool {
	if r.Method == http.MethodGet || r.Method == http.MethodHead {
		return true
	}
	w.Header().Set("Allow", "GET, HEAD")
	writeError(w, http.StatusMethodNotAllowed, "method not allowed")
	return false
}

func writeError(w http.ResponseWriter, status int, message string) {
	writeJSON(w, status, map[string]string{"error": message})
}

func writeJSON(w http.ResponseWriter, status int, body any) {
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(body)
}
