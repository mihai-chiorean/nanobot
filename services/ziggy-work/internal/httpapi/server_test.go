package httpapi

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/mihai-chiorean/nanobot/services/ziggy-work/internal/model"
	"github.com/mihai-chiorean/nanobot/services/ziggy-work/internal/principal"
	"github.com/mihai-chiorean/nanobot/services/ziggy-work/internal/queue"
	"github.com/mihai-chiorean/nanobot/services/ziggy-work/internal/repository"
	"github.com/mihai-chiorean/nanobot/services/ziggy-work/internal/telemetry"
)

type fakeExecutor struct{}

func (fakeExecutor) Execute(context.Context, model.Task, string) error         { return nil }
func (fakeExecutor) Cancel(context.Context, model.Task) error                  { return nil }
func (fakeExecutor) Message(context.Context, model.Task, string, string) error { return nil }
func (fakeExecutor) Health(context.Context) error                              { return nil }

func signedRequest(method, path, user, workspace string) *http.Request {
	key := []byte("01234567890123456789012345678901")
	payload := base64.RawURLEncoding.EncodeToString([]byte(`{"user_id":"` + user + `","workspace_id":"` + workspace + `","expires_at":4102444800}`))
	mac := hmac.New(sha256.New, key)
	_, _ = mac.Write([]byte(payload))
	req := httptest.NewRequest(method, path, nil)
	req.Header.Set(principal.HeaderPayload, payload)
	req.Header.Set(principal.HeaderSignature, base64.RawURLEncoding.EncodeToString(mac.Sum(nil)))
	return req
}

func TestArtifactTenantAndPathIsolation(t *testing.T) {
	repo := repository.NewMemory()
	tenantA := model.Tenant{UserID: "a", WorkspaceID: "wa"}
	taskID := "work_00000000000000000000000000000001"
	_, err := repo.Import(context.Background(), tenantA, repository.LegacyImport{Tasks: []model.Task{{TaskID: taskID, Status: model.Succeeded}}, Artifacts: []model.Artifact{{ArtifactID: "artifact_1", TaskID: taskID, Available: true, PathRel: "task/artifact_1"}}})
	if err != nil {
		t.Fatal(err)
	}
	root := t.TempDir()
	path := filepath.Join(root, "task", "artifact_1")
	if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte("safe"), 0600); err != nil {
		t.Fatal(err)
	}
	api, err := New(Config{Repository: repo, Queue: queue.NewMemory(2, 1, nil), Executor: fakeExecutor{}, Verifier: principal.NewVerifier([]byte("01234567890123456789012345678901")), Telemetry: telemetry.Noop(), ArtifactRoot: root})
	if err != nil {
		t.Fatal(err)
	}
	for _, path := range []string{"/api/work/artifacts/artifact_1", "/api/work/artifacts/../artifact_1"} {
		rec := httptest.NewRecorder()
		api.ServeHTTP(rec, signedRequest("GET", path, "b", "wb"))
		if rec.Code != 404 {
			t.Fatalf("foreign/traversal path %s status=%d", path, rec.Code)
		}
	}
	rec := httptest.NewRecorder()
	api.ServeHTTP(rec, signedRequest("GET", "/api/work/"+taskID, "a", "wa"))
	if rec.Code != 200 {
		t.Fatalf("detail status=%d", rec.Code)
	}
}

func TestMutationsRequireTenantScopedIdempotencyAndReplayResults(t *testing.T) {
	repo := repository.NewMemory()
	api, err := New(Config{
		Repository: repo,
		Queue:      queue.NewMemory(16, 1, nil),
		Executor:   fakeExecutor{},
		Verifier:   principal.NewVerifier([]byte("01234567890123456789012345678901")),
		Telemetry:  telemetry.Noop(),
	})
	if err != nil {
		t.Fatal(err)
	}

	request := func(method, path, body, key, user string) *http.Request {
		req := signedRequest(method, path, user, "workspace")
		req.Body = io.NopCloser(strings.NewReader(body))
		req.ContentLength = int64(len(body))
		req.Header.Set("Content-Type", "application/json")
		if key != "" {
			req.Header.Set("Idempotency-Key", key)
		}
		return req
	}
	call := func(req *http.Request) *httptest.ResponseRecorder {
		recorder := httptest.NewRecorder()
		api.ServeHTTP(recorder, req)
		return recorder
	}

	body := `{"chat_id":"chat-1","content":"prepare report"}`
	if got := call(request(http.MethodPost, "/api/work", body, "", "user-a")); got.Code != http.StatusBadRequest {
		t.Fatalf("missing key status=%d", got.Code)
	}
	first := call(request(http.MethodPost, "/api/work", body, "create-request-0001", "user-a"))
	second := call(request(http.MethodPost, "/api/work", body, "create-request-0001", "user-a"))
	if first.Code != http.StatusCreated || second.Code != http.StatusCreated {
		t.Fatalf("create statuses=%d,%d", first.Code, second.Code)
	}
	var firstBody, secondBody struct {
		Task model.Task `json:"task"`
	}
	if err := json.Unmarshal(first.Body.Bytes(), &firstBody); err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(second.Body.Bytes(), &secondBody); err != nil {
		t.Fatal(err)
	}
	if firstBody.Task.TaskID == "" || firstBody.Task.TaskID != secondBody.Task.TaskID {
		t.Fatalf("replay task ids=%q,%q", firstBody.Task.TaskID, secondBody.Task.TaskID)
	}
	conflict := call(request(http.MethodPost, "/api/work", `{"chat_id":"chat-1","content":"different"}`, "create-request-0001", "user-a"))
	if conflict.Code != http.StatusConflict {
		t.Fatalf("conflict status=%d", conflict.Code)
	}
	otherTenant := call(request(http.MethodPost, "/api/work", body, "create-request-0001", "user-b"))
	if otherTenant.Code != http.StatusCreated {
		t.Fatalf("tenant-scoped replay status=%d", otherTenant.Code)
	}

	taskPath := "/api/work/" + firstBody.Task.TaskID
	for range 2 {
		response := call(request(http.MethodPost, taskPath+"/messages", `{"content":"continue"}`, "message-request-0001", "user-a"))
		if response.Code != http.StatusAccepted {
			t.Fatalf("message replay status=%d", response.Code)
		}
	}
	for range 2 {
		response := call(request(http.MethodPost, taskPath+"/cancel", "", "cancel-request-0001", "user-a"))
		if response.Code != http.StatusOK {
			t.Fatalf("cancel replay status=%d", response.Code)
		}
	}
	events, err := repo.ListEvents(context.Background(), model.Tenant{UserID: "user-a", WorkspaceID: "workspace"}, firstBody.Task.TaskID, 0, 20)
	if err != nil {
		t.Fatal(err)
	}
	if len(events) != 4 {
		t.Fatalf("replayed mutations produced %d events", len(events))
	}
}
