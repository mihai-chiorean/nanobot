package importer

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/mihai-chiorean/nanobot/services/ziggy-work/internal/model"
	"github.com/mihai-chiorean/nanobot/services/ziggy-work/internal/repository"
)

func TestImportRejectsTrailingAndDuplicateRecordsBeforeWrites(t *testing.T) {
	r := `{"tasks":[{"task_id":"work_00000000000000000000000000000001","status":"succeeded"}],"events":[{"task_id":"work_00000000000000000000000000000001","seq":1},{"task_id":"work_00000000000000000000000000000001","seq":1}]} `
	if _, err := Parse(strings.NewReader(r)); err == nil {
		t.Fatal("duplicate sequence accepted")
	}
	if _, err := Parse(strings.NewReader(`{"tasks":[]} {}`)); err == nil {
		t.Fatal("trailing JSON accepted")
	}
}

func TestParseBindsLegacyRecordsToRuntimeCursor(t *testing.T) {
	document := `{"tasks":[{"task_id":"work_00000000000000000000000000000001","runtime_task_id":"work_00000000000000000000000000000002","status":"succeeded"}],"events":[{"task_id":"work_00000000000000000000000000000001","seq":7,"type":"status.changed","payload":{"status":"succeeded"}}]}`
	parsed, err := Parse(strings.NewReader(document))
	if err != nil {
		t.Fatal(err)
	}
	if parsed.Tasks[0].RuntimeTaskID != "work_00000000000000000000000000000002" {
		t.Fatalf("runtime task id=%q", parsed.Tasks[0].RuntimeTaskID)
	}
	if parsed.Events[0].RuntimeTaskID != parsed.Tasks[0].RuntimeTaskID || parsed.Events[0].RuntimeSeq != 7 {
		t.Fatalf("runtime event=%+v", parsed.Events[0])
	}
}

func TestImportVerifiesArtifactHashAndTraversal(t *testing.T) {
	root := t.TempDir()
	if err := os.WriteFile(filepath.Join(root, "ok.bin"), []byte("bytes"), 0600); err != nil {
		t.Fatal(err)
	}
	base := map[string]any{"tasks": []any{map[string]any{"task_id": "work_00000000000000000000000000000001", "status": "succeeded"}}, "artifacts": []any{map[string]any{"artifact_id": "a1", "task_id": "work_00000000000000000000000000000001", "path": "ok.bin", "sha256": "bad"}}}
	b, _ := json.Marshal(base)
	repo := repository.NewMemory()
	_, err := Import(context.Background(), repo, model.Tenant{UserID: "u", WorkspaceID: "w"}, strings.NewReader(string(b)), Options{ArtifactRoot: root, ArtifactDestination: filepath.Join(t.TempDir(), "dest")})
	if err == nil {
		t.Fatal("hash mismatch accepted")
	}
	if _, err = repo.GetTask(context.Background(), model.Tenant{UserID: "u", WorkspaceID: "w"}, "work_00000000000000000000000000000001"); err == nil {
		t.Fatal("partial task import")
	}
	base["artifacts"].([]any)[0].(map[string]any)["path"] = "../ok.bin"
	b, _ = json.Marshal(base)
	if _, err = Import(context.Background(), repo, model.Tenant{UserID: "u", WorkspaceID: "w"}, strings.NewReader(string(b)), Options{ArtifactRoot: root, ArtifactDestination: filepath.Join(t.TempDir(), "dest")}); err == nil {
		t.Fatal("traversal accepted")
	}
}

func TestImportAddsIdempotentTerminalEventForActiveTask(t *testing.T) {
	tenant := model.Tenant{UserID: "u", WorkspaceID: "w"}
	repo := repository.NewMemory()
	document := `{"tasks":[{"task_id":"work_00000000000000000000000000000001","status":"running","last_seq":10}],"events":[{"task_id":"work_00000000000000000000000000000001","seq":1,"type":"status.changed","payload":{"status":"running"}}]}`

	for range 2 {
		if _, err := Import(context.Background(), repo, tenant, strings.NewReader(document), Options{}); err != nil {
			t.Fatal(err)
		}
	}
	task, err := repo.GetTask(context.Background(), tenant, "work_00000000000000000000000000000001")
	if err != nil {
		t.Fatal(err)
	}
	events, err := repo.ListEvents(context.Background(), tenant, task.TaskID, 0, 10)
	if err != nil {
		t.Fatal(err)
	}
	if task.Status != model.Interrupted || task.LastSeq != 11 || len(events) != 2 {
		t.Fatalf("task=%+v events=%+v", task, events)
	}
	last := events[len(events)-1]
	if last.Type != "status.changed" || last.Payload["status"] != model.Interrupted {
		t.Fatalf("terminal event=%+v", last)
	}
	if last.RuntimeTaskID != "" || last.RuntimeSeq != 0 {
		t.Fatalf("migration event must not advance runtime cursor: %+v", last)
	}
}
