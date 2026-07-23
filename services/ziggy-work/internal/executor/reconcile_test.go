package executor

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strconv"
	"sync/atomic"
	"testing"
	"time"

	"github.com/mihai-chiorean/nanobot/services/ziggy-work/internal/model"
	"github.com/mihai-chiorean/nanobot/services/ziggy-work/internal/repository"
	"github.com/mihai-chiorean/nanobot/services/ziggy-work/internal/telemetry"
	"github.com/mihai-chiorean/nanobot/services/ziggy-work/internal/tenant"
)

func TestReconcilePaginatesTasksEventsAndCopiesArtifacts(t *testing.T) {
	now := time.Now().UTC().Truncate(time.Second)
	taskIDs := []string{"work_00000000000000000000000000000001", "work_00000000000000000000000000000002"}
	artifactBytes := []byte("durable artifact")
	secret := "01234567890123456789012345678901"
	mux := http.NewServeMux()
	mux.HandleFunc("/auth/token", func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer "+secret {
			http.Error(w, "unauthorized", http.StatusUnauthorized)
			return
		}
		writeTestJSON(w, map[string]any{"token": "rest-token", "ws_path": "/ws"})
	})
	mux.HandleFunc("/api/work", func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Query().Get("after_task_id") == "" {
			writeTestJSON(w, map[string]any{"tasks": []any{map[string]any{"task_id": taskIDs[0]}}, "has_more": true, "next_task_id": taskIDs[0]})
			return
		}
		writeTestJSON(w, map[string]any{"tasks": []any{map[string]any{"task_id": taskIDs[1]}}, "has_more": false, "next_task_id": taskIDs[1]})
	})
	for index, taskID := range taskIDs {
		taskID := taskID
		index := index
		mux.HandleFunc("/api/work/"+taskID, func(w http.ResponseWriter, r *http.Request) {
			artifacts := []any{}
			steps := []any{}
			if index == 0 {
				artifacts = []any{map[string]any{"artifact_id": "artifact_00000000000000000000000000000001", "task_id": taskID, "kind": "report", "name": "report.md", "mime": "text/markdown", "size_bytes": len(artifactBytes), "created_at": now, "url": "/api/work/artifacts/artifact_00000000000000000000000000000001"}}
				steps = []any{map[string]any{"step_id": "step_1", "task_id": taskID, "seq_start": 1, "title": "Research", "status": "succeeded"}}
			}
			writeTestJSON(w, map[string]any{"task": map[string]any{"task_id": taskID, "session_key": "work:" + taskID, "chat_id": "chat", "title": fmt.Sprintf("task %d", index), "status": "succeeded", "mode": "background", "created_at": now, "updated_at": now, "steps": steps, "artifacts": artifacts}})
		})
		mux.HandleFunc("/api/work/"+taskID+"/events", func(w http.ResponseWriter, r *http.Request) {
			after, _ := strconv.Atoi(r.URL.Query().Get("after_seq"))
			if index == 0 && after == 0 {
				writeTestJSON(w, map[string]any{"events": []any{map[string]any{"task_id": taskID, "seq": 1, "type": "status.changed", "actor": "nanobot", "created_at": now, "payload": map[string]any{"status": "running"}}}, "has_more": true, "next_after_seq": 1})
				return
			}
			seq := 1
			if index == 0 {
				seq = 2
			}
			writeTestJSON(w, map[string]any{"events": []any{map[string]any{"task_id": taskID, "seq": seq, "type": "status.changed", "actor": "nanobot", "created_at": now, "payload": map[string]any{"status": "succeeded"}}}, "has_more": false, "next_after_seq": seq})
		})
	}
	mux.HandleFunc("/api/work/artifacts/artifact_00000000000000000000000000000001", func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write(artifactBytes)
	})
	server := httptest.NewServer(mux)
	defer server.Close()

	manifest := filepath.Join(t.TempDir(), "tenants.json")
	document := map[string]any{"version": 1, "tenants": []any{map[string]any{"user_id": "user", "workspace_id": "workspace", "upstream_url": server.URL, "upstream_bootstrap_secret": secret, "status": "active"}}}
	b, _ := json.Marshal(document)
	if err := os.WriteFile(manifest, b, 0600); err != nil {
		t.Fatal(err)
	}
	registry, err := tenant.Load(manifest)
	if err != nil {
		t.Fatal(err)
	}
	repo := repository.NewMemory()
	artifactRoot := t.TempDir()
	runtime := NewNanobot(repo, registry, artifactRoot, 5*time.Second, slog.New(slog.NewTextHandler(io.Discard, nil)), telemetry.Noop())
	if err := runtime.Reconcile(context.Background()); err != nil {
		t.Fatal(err)
	}
	if err := runtime.Reconcile(context.Background()); err != nil {
		t.Fatal(err)
	}
	tenantID := model.Tenant{UserID: "user", WorkspaceID: "workspace"}
	tasks, err := repo.ListTasks(context.Background(), tenantID, model.ListFilter{Limit: 10})
	if err != nil || len(tasks) != 2 {
		t.Fatalf("tasks=%d err=%v", len(tasks), err)
	}
	events, err := repo.ListEvents(context.Background(), tenantID, taskIDs[0], 0, 100)
	if err != nil || len(events) != 2 {
		t.Fatalf("events=%d err=%v", len(events), err)
	}
	task, err := repo.GetTask(context.Background(), tenantID, taskIDs[0])
	if err != nil || len(task.Steps) != 1 || len(task.Artifacts) != 1 || !task.Artifacts[0].Available {
		t.Fatalf("task=%+v err=%v", task, err)
	}
	contents, err := os.ReadFile(filepath.Join(artifactRoot, task.Artifacts[0].PathRel))
	if err != nil || string(contents) != string(artifactBytes) {
		t.Fatalf("artifact=%q err=%v", contents, err)
	}
}

func TestNanobotHTTPClientRejectsRedirectsWithoutLeakingCredentials(t *testing.T) {
	var targetHits atomic.Int32
	target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		targetHits.Add(1)
		if r.Header.Get("Authorization") != "" {
			t.Error("redirect target received Authorization header")
		}
		writeTestJSON(w, map[string]any{"token": "stolen"})
	}))
	defer target.Close()

	redirect := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, target.URL+"/capture", http.StatusTemporaryRedirect)
	}))
	defer redirect.Close()

	runtime := NewNanobot(repository.NewMemory(), nil, t.TempDir(), 5*time.Second, slog.New(slog.NewTextHandler(io.Discard, nil)), telemetry.Noop())
	allocation := tenant.Allocation{UpstreamURL: redirect.URL, UpstreamBootstrapSecret: "bootstrap-secret"}
	if _, _, err := runtime.token(context.Background(), allocation); err == nil {
		t.Fatal("token redirect was accepted")
	}
	var document map[string]any
	if err := runtime.getJSON(context.Background(), redirect.URL+"/api/work", "rest-secret", 1024, &document); err == nil {
		t.Fatal("JSON redirect was accepted")
	}
	if _, err := runtime.getArtifact(context.Background(), redirect.URL, "/api/work/artifacts/artifact_1", "rest-secret"); err == nil {
		t.Fatal("artifact redirect was accepted")
	}
	if targetHits.Load() != 0 {
		t.Fatalf("redirect target received %d requests", targetHits.Load())
	}
}

func writeTestJSON(w http.ResponseWriter, value any) {
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(value)
}
