package httpapi

import (
	"context"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/mihai-chiorean/nanobot/services/ziggy-work/internal/executor"
	"github.com/mihai-chiorean/nanobot/services/ziggy-work/internal/model"
	"github.com/mihai-chiorean/nanobot/services/ziggy-work/internal/principal"
	"github.com/mihai-chiorean/nanobot/services/ziggy-work/internal/queue"
	"github.com/mihai-chiorean/nanobot/services/ziggy-work/internal/repository"
	"github.com/mihai-chiorean/nanobot/services/ziggy-work/internal/telemetry"
)

type Config struct {
	Repository          repository.Repository
	Queue               queue.Queue
	Executor            executor.Executor
	Verifier            *principal.Verifier
	Telemetry           telemetry.Telemetry
	Logger              *slog.Logger
	RequestTimeout      time.Duration
	BodyLimit           int64
	EventLimit          int64
	ArtifactRoot        string
	MaxStreams          int
	MaxStreamsPerTenant int
	Version             string
}
type Server struct {
	cfg               Config
	streamSlots       chan struct{}
	tenantStreamSlots sync.Map
}

func New(cfg Config) (*Server, error) {
	if cfg.Repository == nil || cfg.Queue == nil || cfg.Executor == nil || cfg.Verifier == nil {
		return nil, errors.New("http api dependencies are required")
	}
	if cfg.Logger == nil {
		cfg.Logger = slog.Default()
	}
	if cfg.Telemetry == nil {
		cfg.Telemetry = telemetry.Noop()
	}
	if cfg.MaxStreams <= 0 {
		cfg.MaxStreams = 100
	}
	if cfg.MaxStreamsPerTenant <= 0 {
		cfg.MaxStreamsPerTenant = 4
	}
	if cfg.MaxStreamsPerTenant > cfg.MaxStreams {
		cfg.MaxStreamsPerTenant = cfg.MaxStreams
	}
	return &Server{cfg: cfg, streamSlots: make(chan struct{}, cfg.MaxStreams)}, nil
}

func (s *Server) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	ctx, span := s.cfg.Telemetry.Start(r.Context(), "ziggy.work.http")
	defer span.End()
	r = r.WithContext(ctx)
	start := time.Now()
	route := routeName(r.URL.Path)
	status := http.StatusOK
	defer func() { s.cfg.Telemetry.HTTP(r.Context(), r.Method, route, status, time.Since(start)) }()
	switch {
	case r.URL.Path == "/healthz":
		status = s.health(w, r)
		return
	case r.URL.Path == "/readyz":
		status = s.ready(w, r)
		return
	case r.URL.Path == "/api/work" || r.URL.Path == "/api/work/" || strings.HasPrefix(r.URL.Path, "/api/work/"):
		status = s.work(w, r)
		return
	default:
		status = http.StatusNotFound
		writeError(w, status, "not found")
		return
	}
}

func routeName(path string) string {
	switch {
	case path == "/api/work":
		return "/api/work"
	case strings.HasSuffix(path, "/events/stream"):
		return "/api/work/{task_id}/events/stream"
	case strings.HasSuffix(path, "/events"):
		return "/api/work/{task_id}/events"
	case strings.Contains(path, "/artifacts/"):
		return "/api/work/artifacts/{artifact_id}"
	case strings.HasSuffix(path, "/cancel"):
		return "/api/work/{task_id}/cancel"
	case strings.HasSuffix(path, "/messages"), strings.HasSuffix(path, "/message"):
		return "/api/work/{task_id}/messages"
	}
	return "/api/work/{task_id}"
}
func (s *Server) health(w http.ResponseWriter, _ *http.Request) int {
	writeJSON(w, 200, map[string]any{"status": "ok", "version": s.cfg.Version})
	return 200
}
func (s *Server) ready(w http.ResponseWriter, r *http.Request) int {
	ctx, cancel := context.WithTimeout(r.Context(), 2*time.Second)
	defer cancel()
	if err := s.cfg.Repository.Health(ctx); err != nil {
		s.cfg.Telemetry.Dependency(r.Context(), "postgres", false)
		writeError(w, 503, "not ready")
		return 503
	}
	s.cfg.Telemetry.Dependency(r.Context(), "postgres", true)
	if err := s.cfg.Executor.Health(ctx); err != nil {
		s.cfg.Telemetry.Dependency(r.Context(), "nanobot", false)
		writeError(w, 503, "not ready")
		return 503
	}
	s.cfg.Telemetry.Dependency(r.Context(), "nanobot", true)
	writeJSON(w, 200, map[string]any{"status": "ready"})
	return 200
}

func (s *Server) work(w http.ResponseWriter, r *http.Request) int {
	p, ok := s.principal(w, r)
	if !ok {
		return 401
	}
	tenant := model.Tenant{UserID: p.UserID, WorkspaceID: p.WorkspaceID}
	parts := strings.Split(strings.Trim(r.URL.Path, "/"), "/")
	if len(parts) == 2 {
		return s.workRoot(w, r, tenant)
	}
	if len(parts) == 3 {
		if r.Method != http.MethodGet {
			writeError(w, http.StatusMethodNotAllowed, "method not allowed")
			return http.StatusMethodNotAllowed
		}
		task, ok := s.get(w, r, tenant, parts[2])
		if !ok {
			return http.StatusNotFound
		}
		writeJSON(w, http.StatusOK, map[string]any{"task": s.snapshot(task)})
		return http.StatusOK
	}
	if len(parts) < 4 {
		writeError(w, 404, "not found")
		return 404
	}
	if parts[2] == "artifacts" {
		if len(parts) != 4 {
			writeError(w, http.StatusNotFound, "not found")
			return http.StatusNotFound
		}
		return s.artifact(w, r, tenant, parts[3])
	}
	id := parts[2]
	if !model.WorkIDPattern.MatchString(id) {
		writeError(w, 400, "invalid task id")
		return 400
	}
	switch parts[3] {
	case "events":
		if len(parts) == 5 && parts[4] == "stream" {
			if r.Method != http.MethodGet {
				writeError(w, http.StatusMethodNotAllowed, "method not allowed")
				return http.StatusMethodNotAllowed
			}
			return s.stream(w, r, tenant, id)
		}
		if len(parts) != 4 {
			writeError(w, http.StatusNotFound, "not found")
			return http.StatusNotFound
		}
		if r.Method != http.MethodGet {
			writeError(w, http.StatusMethodNotAllowed, "method not allowed")
			return http.StatusMethodNotAllowed
		}
		return s.events(w, r, tenant, id)
	case "cancel":
		if len(parts) != 4 {
			writeError(w, http.StatusNotFound, "not found")
			return http.StatusNotFound
		}
		if r.Method != http.MethodPost {
			writeError(w, http.StatusMethodNotAllowed, "method not allowed")
			return http.StatusMethodNotAllowed
		}
		return s.cancel(w, r, tenant, id)
	case "message", "messages":
		if len(parts) != 4 {
			writeError(w, http.StatusNotFound, "not found")
			return http.StatusNotFound
		}
		if r.Method != http.MethodPost {
			writeError(w, http.StatusMethodNotAllowed, "method not allowed")
			return http.StatusMethodNotAllowed
		}
		return s.message(w, r, tenant, id)
	default:
		writeError(w, 404, "not found")
		return 404
	}
}
func (s *Server) principal(w http.ResponseWriter, r *http.Request) (principal.Principal, bool) {
	p, err := s.cfg.Verifier.VerifyRequest(r)
	if err != nil {
		writeError(w, 401, "unauthorized")
		return principal.Principal{}, false
	}
	return p, true
}
func (s *Server) workRoot(w http.ResponseWriter, r *http.Request, t model.Tenant) int {
	switch r.Method {
	case http.MethodGet:
		limit, _ := strconv.Atoi(r.URL.Query().Get("limit"))
		tasks, err := s.cfg.Repository.ListTasks(r.Context(), t, model.ListFilter{Status: model.Status(r.URL.Query().Get("status")), Limit: limit})
		if err != nil {
			writeError(w, 500, "repository unavailable")
			return 500
		}
		for i := range tasks {
			tasks[i] = s.snapshot(tasks[i])
		}
		writeJSON(w, 200, map[string]any{"tasks": tasks})
		return 200
	case http.MethodPost:
		var in struct {
			ChatID  string            `json:"chat_id"`
			Content string            `json:"content"`
			Title   string            `json:"title"`
			Mode    string            `json:"mode"`
			Model   string            `json:"model"`
			Media   []json.RawMessage `json:"media"`
		}
		if !decodeBody(w, r, &in, s.cfg.BodyLimit) || strings.TrimSpace(in.Content) == "" {
			writeError(w, 400, "invalid work body")
			return 400
		}
		if len(in.Media) > 0 {
			writeError(w, http.StatusNotImplemented, "media requires legacy websocket fallback")
			return http.StatusNotImplemented
		}
		request, ok := requestIdentity(r, in)
		if !ok {
			writeError(w, http.StatusBadRequest, "valid Idempotency-Key header is required")
			return http.StatusBadRequest
		}
		task, err := s.cfg.Repository.CreateTask(r.Context(), t, model.CreateInput{ChatID: in.ChatID, Content: in.Content, Title: in.Title, Mode: in.Mode, Model: in.Model}, request, s.cfg.Queue)
		if errors.Is(err, model.ErrIdempotencyConflict) {
			writeError(w, http.StatusConflict, "idempotency key conflict")
			return http.StatusConflict
		}
		if err != nil {
			writeError(w, 503, "work unavailable")
			return 503
		}
		writeJSON(w, 201, map[string]any{"task": s.snapshot(task)})
		return 201
	default:
		writeError(w, 405, "method not allowed")
		return 405
	}
}
func (s *Server) get(w http.ResponseWriter, r *http.Request, t model.Tenant, id string) (model.Task, bool) {
	task, err := s.cfg.Repository.GetTask(r.Context(), t, id)
	if errors.Is(err, model.ErrNotFound) {
		writeError(w, 404, "not found")
		return model.Task{}, false
	}
	if err != nil {
		writeError(w, 500, "repository unavailable")
		return model.Task{}, false
	}
	return task, true
}
func (s *Server) events(w http.ResponseWriter, r *http.Request, t model.Tenant, id string) int {
	if _, ok := s.get(w, r, t, id); !ok {
		return 404
	}
	after := parseAfter(r)
	limit := parseLimit(r, 100)
	events, err := s.cfg.Repository.ListEvents(r.Context(), t, id, after, limit)
	if err != nil {
		writeError(w, 500, "repository unavailable")
		return 500
	}
	next := after
	if len(events) > 0 {
		next = events[len(events)-1].Seq
	}
	for i := range events {
		events[i] = s.publicEvent(events[i])
	}
	writeJSON(w, 200, map[string]any{"events": events, "has_more": len(events) == limit, "next_after_seq": next})
	return 200
}
func (s *Server) stream(w http.ResponseWriter, r *http.Request, t model.Tenant, id string) int {
	task, ok := s.get(w, r, t, id)
	if !ok {
		return 404
	}
	select {
	case s.streamSlots <- struct{}{}:
		defer func() { <-s.streamSlots }()
	default:
		writeError(w, http.StatusTooManyRequests, "too many streams")
		return http.StatusTooManyRequests
	}
	tenantSlots, _ := s.tenantStreamSlots.LoadOrStore(
		t.UserID+"\x00"+t.WorkspaceID,
		make(chan struct{}, s.cfg.MaxStreamsPerTenant),
	)
	select {
	case tenantSlots.(chan struct{}) <- struct{}{}:
		defer func() { <-tenantSlots.(chan struct{}) }()
	default:
		writeError(w, http.StatusTooManyRequests, "too many tenant streams")
		return http.StatusTooManyRequests
	}
	flusher, ok := w.(http.Flusher)
	if !ok {
		writeError(w, 500, "stream unavailable")
		return 500
	}
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("Connection", "keep-alive")
	after := parseAfter(r)
	if v := r.Header.Get("Last-Event-ID"); v != "" {
		if n, e := strconv.ParseInt(v, 10, 64); e == nil && n > after {
			after = n
		}
	}
	notify, cancel := s.cfg.Repository.Subscribe(t, id)
	defer cancel()
	poll := time.NewTicker(5 * time.Second)
	defer poll.Stop()
	heartbeat := time.NewTicker(15 * time.Second)
	defer heartbeat.Stop()
	for {
		events, err := s.cfg.Repository.ListEvents(r.Context(), t, id, after, 100)
		if err != nil {
			return 500
		}
		terminalSeen := false
		for _, e := range events {
			if e.Type == "status.changed" {
				if raw, ok := e.Payload["status"].(string); ok && model.Status(raw).Terminal() {
					terminalSeen = true
				}
			}
			e = s.publicEvent(e)
			body, _ := json.Marshal(map[string]any{"event": "work.event", "task_id": e.TaskID, "seq": e.Seq, "type": e.Type, "payload": e.Payload, "actor": e.Actor, "step_id": e.StepID, "created_at": e.CreatedAt})
			fmt.Fprintf(w, "id: %d\nevent: work.event\ndata: %s\n\n", e.Seq, body)
			after = e.Seq
			flusher.Flush()
		}
		if terminalSeen || task.Status.Terminal() && len(events) < 100 {
			return http.StatusOK
		}
		if len(events) < 100 {
			current, err := s.cfg.Repository.GetTask(r.Context(), t, id)
			if err != nil {
				return http.StatusInternalServerError
			}
			task = current
			if task.Status.Terminal() {
				return http.StatusOK
			}
		}
		select {
		case <-r.Context().Done():
			return 200
		case <-notify:
		case <-poll.C:
		case <-heartbeat.C:
			_, _ = io.WriteString(w, ": keep-alive\n\n")
			flusher.Flush()
		}
	}
}
func (s *Server) cancel(w http.ResponseWriter, r *http.Request, t model.Tenant, id string) int {
	request, valid := requestIdentity(r, struct {
		TaskID string `json:"task_id"`
	}{TaskID: id})
	if !valid {
		writeError(w, http.StatusBadRequest, "valid Idempotency-Key header is required")
		return http.StatusBadRequest
	}
	updated, err := s.cfg.Repository.CancelTask(r.Context(), t, id, request)
	if errors.Is(err, model.ErrIdempotencyConflict) {
		writeError(w, http.StatusConflict, "idempotency key conflict")
		return http.StatusConflict
	}
	if errors.Is(err, model.ErrNotFound) {
		writeError(w, http.StatusNotFound, "not found")
		return http.StatusNotFound
	}
	if errors.Is(err, model.ErrTerminal) {
		writeError(w, 409, "task is complete")
		return 409
	}
	if err != nil {
		writeError(w, 503, "repository unavailable")
		return 503
	}
	if updated.RiverJobID != "" {
		_ = s.cfg.Queue.Cancel(r.Context(), updated.RiverJobID)
	}
	_ = s.cfg.Executor.Cancel(r.Context(), updated)
	s.cfg.Telemetry.Cancel(r.Context(), "http")
	writeJSON(w, 200, map[string]any{"task": s.snapshot(updated)})
	return 200
}
func (s *Server) message(w http.ResponseWriter, r *http.Request, t model.Tenant, id string) int {
	var in struct {
		Content string `json:"content"`
	}
	if !decodeBody(w, r, &in, s.cfg.BodyLimit) || strings.TrimSpace(in.Content) == "" {
		writeError(w, 400, "content is required")
		return 400
	}
	request, valid := requestIdentity(r, struct {
		TaskID  string `json:"task_id"`
		Content string `json:"content"`
	}{TaskID: id, Content: in.Content})
	if !valid {
		writeError(w, http.StatusBadRequest, "valid Idempotency-Key header is required")
		return http.StatusBadRequest
	}
	start := time.Now()
	updated, _, err := s.cfg.Repository.EnqueueFollowUp(r.Context(), t, id, in.Content, request, s.cfg.Queue)
	s.cfg.Telemetry.Queue(r.Context(), "follow_up", time.Since(start), err)
	if err != nil {
		if errors.Is(err, model.ErrNotFound) {
			writeError(w, http.StatusNotFound, "not found")
			return http.StatusNotFound
		}
		if errors.Is(err, model.ErrIdempotencyConflict) {
			writeError(w, http.StatusConflict, "idempotency key conflict")
			return http.StatusConflict
		}
		if errors.Is(err, model.ErrTerminal) {
			writeError(w, http.StatusConflict, "task does not accept messages")
			return http.StatusConflict
		}
		writeError(w, 503, "work queue unavailable")
		return 503
	}
	_ = updated
	writeJSON(w, 202, map[string]any{"accepted": true, "task_id": id})
	return 202
}
func (s *Server) artifact(w http.ResponseWriter, r *http.Request, t model.Tenant, id string) int {
	if r.Method != http.MethodGet || id == "" || len(id) > 128 || strings.ContainsAny(id, "/\\") {
		writeError(w, 404, "not found")
		return 404
	}
	a, err := s.cfg.Repository.GetArtifact(r.Context(), t, id)
	if err != nil || !a.Available || s.cfg.ArtifactRoot == "" {
		writeError(w, 404, "not found")
		return 404
	}
	root, err := filepath.Abs(s.cfg.ArtifactRoot)
	if err != nil {
		return artifact404(w)
	}
	clean := filepath.Clean(a.PathRel)
	if clean == "." || clean == ".." || strings.HasPrefix(clean, ".."+string(filepath.Separator)) {
		return artifact404(w)
	}
	path := filepath.Join(root, clean)
	real, err := filepath.EvalSymlinks(path)
	if err != nil {
		return artifact404(w)
	}
	rel, err := filepath.Rel(root, real)
	if err != nil || rel == ".." || strings.HasPrefix(rel, ".."+string(filepath.Separator)) {
		return artifact404(w)
	}
	info, err := os.Stat(real)
	if err != nil || !info.Mode().IsRegular() {
		return artifact404(w)
	}
	http.ServeFile(w, r, real)
	return 200
}
func artifact404(w http.ResponseWriter) int { writeError(w, 404, "not found"); return 404 }
func (s *Server) snapshot(t model.Task) model.Task {
	for i := range t.Artifacts {
		if t.Artifacts[i].Available {
			t.Artifacts[i].URL = "/api/work/artifacts/" + t.Artifacts[i].ArtifactID
		}
	}
	return t
}

func (s *Server) publicEvent(e model.Event) model.Event {
	if s.cfg.EventLimit <= 0 {
		return e
	}
	b, err := json.Marshal(e.Payload)
	if err == nil && int64(len(b)) > s.cfg.EventLimit {
		e.Payload = map[string]any{"truncated": true}
	}
	return e
}

func requestIdentity(r *http.Request, payload any) (model.RequestIdentity, bool) {
	key := r.Header.Get("Idempotency-Key")
	if len(key) < 16 || len(key) > 128 || strings.TrimSpace(key) != key {
		return model.RequestIdentity{}, false
	}
	for _, ch := range key {
		if !(ch >= 'a' && ch <= 'z' || ch >= 'A' && ch <= 'Z' || ch >= '0' && ch <= '9' || strings.ContainsRune("-_.:", ch)) {
			return model.RequestIdentity{}, false
		}
	}
	body, err := json.Marshal(payload)
	if err != nil {
		return model.RequestIdentity{}, false
	}
	digest := sha256.Sum256(body)
	return model.RequestIdentity{Key: key, PayloadHash: fmt.Sprintf("%x", digest)}, true
}

func parseAfter(r *http.Request) int64 {
	v := r.URL.Query().Get("after_seq")
	if v == "" {
		v = r.URL.Query().Get("after")
	}
	n, _ := strconv.ParseInt(v, 10, 64)
	if n < 0 {
		return 0
	}
	return n
}
func parseLimit(r *http.Request, d int) int {
	n, e := strconv.Atoi(r.URL.Query().Get("limit"))
	if e != nil || n < 1 {
		return d
	}
	if n > 500 {
		return 500
	}
	return n
}
func decodeBody(w http.ResponseWriter, r *http.Request, v any, limit int64) bool {
	if limit <= 0 {
		limit = 1 << 20
	}
	r.Body = http.MaxBytesReader(w, r.Body, limit)
	dec := json.NewDecoder(r.Body)
	if err := dec.Decode(v); err != nil {
		return false
	}
	var extra any
	if dec.Decode(&extra) != io.EOF {
		return false
	}
	return true
}
func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}
func writeError(w http.ResponseWriter, status int, _ string) {
	writeJSON(w, status, map[string]any{"error": "request failed"})
}
