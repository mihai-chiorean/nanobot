package httpapi

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"log/slog"
	"net/http"
	"path"
	"strings"
	"time"

	"github.com/mihai-chiorean/nanobot/services/ziggy-control/internal/identity"
)

type Middleware func(http.Handler) http.Handler

type Config struct {
	Authenticate   Middleware
	Proxy          http.Handler
	Readiness      ReadinessChecker
	Logger         *slog.Logger
	OwnerEmail     string
	OwnerSubject   string
	BlockedPaths   map[string]struct{}
	MaxRequestBody int64
	Version        string
}

type API struct {
	proxy          http.Handler
	readiness      ReadinessChecker
	logger         *slog.Logger
	ownerEmail     string
	ownerSubject   string
	blockedPaths   map[string]struct{}
	maxRequestBody int64
	version        string
}

type requestIDKey struct{}

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
		ownerEmail:     strings.ToLower(strings.TrimSpace(config.OwnerEmail)),
		ownerSubject:   strings.TrimSpace(config.OwnerSubject),
		blockedPaths:   cloneBlockedPaths(config.BlockedPaths),
		maxRequestBody: config.MaxRequestBody,
		version:        config.Version,
	}
	for _, reserved := range []string{"/auth/bootstrap", "/healthz", "/readyz"} {
		api.blockedPaths[reserved] = struct{}{}
	}

	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", api.health)
	mux.HandleFunc("/readyz", api.ready)
	mux.Handle("/auth/bootstrap", config.Authenticate(http.HandlerFunc(api.bootstrap)))
	mux.HandleFunc("/", api.forward)
	return api.assignRequestID(api.observe(mux)), nil
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
			"request_id", RequestID(r.Context()),
			"error", err,
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
		api.logger.WarnContext(r.Context(), "bootstrap authorization denied",
			"request_id", RequestID(r.Context()),
			"subject", principal.Subject,
		)
		writeError(w, http.StatusForbidden, "account not authorized")
		return
	}
	api.logger.InfoContext(r.Context(), "bootstrap authorized",
		"request_id", RequestID(r.Context()),
		"subject", principal.Subject,
	)
	api.proxy.ServeHTTP(w, r)
}

func (api *API) forward(w http.ResponseWriter, r *http.Request) {
	if api.isBlocked(r.URL.Path) {
		http.NotFound(w, r)
		return
	}
	if !isWebSocketUpgrade(r) && r.Body != nil {
		if r.ContentLength > api.maxRequestBody {
			writeError(w, http.StatusRequestEntityTooLarge, "request body too large")
			return
		}
		r.Body = http.MaxBytesReader(w, r.Body, api.maxRequestBody)
	}
	api.proxy.ServeHTTP(w, r)
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
			api.logger.ErrorContext(r.Context(), "request ID generation failed", "error", err)
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
		observed := &observedResponseWriter{ResponseWriter: w}
		next.ServeHTTP(observed, r)
		duration := time.Since(started)
		api.logger.InfoContext(r.Context(), "request completed",
			"request_id", RequestID(r.Context()),
			"method", r.Method,
			"path", r.URL.Path,
			"route", routeName(r.URL.Path),
			"status", observed.statusCode(),
			"bytes", observed.bytes,
			"duration_ms", float64(duration)/float64(time.Millisecond),
			"cf_ray", r.Header.Get("CF-Ray"),
		)
	})
}

type observedResponseWriter struct {
	http.ResponseWriter
	status int
	bytes  int64
}

func (w *observedResponseWriter) Unwrap() http.ResponseWriter {
	return w.ResponseWriter
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
	default:
		return "proxy"
	}
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
