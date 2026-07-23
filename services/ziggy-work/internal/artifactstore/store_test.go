package artifactstore

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/mihai-chiorean/nanobot/services/ziggy-work/internal/model"
)

func TestPutIsolatesTenantPathsAndRejectsMutation(t *testing.T) {
	root := t.TempDir()
	taskID := "work_00000000000000000000000000000001"
	tenantA := model.Tenant{UserID: "user-a", WorkspaceID: "workspace"}
	tenantB := model.Tenant{UserID: "user-b", WorkspaceID: "workspace"}

	pathA, _, _, _, err := Put(root, tenantA, taskID, "artifact_same", strings.NewReader("alpha"), 1024, 0, "")
	if err != nil {
		t.Fatal(err)
	}
	pathB, _, _, _, err := Put(root, tenantB, taskID, "artifact_same", strings.NewReader("beta"), 1024, 0, "")
	if err != nil {
		t.Fatal(err)
	}
	if pathA == pathB {
		t.Fatal("different tenants received the same artifact path")
	}
	for path, expected := range map[string]string{pathA: "alpha", pathB: "beta"} {
		contents, readErr := os.ReadFile(filepath.Join(root, path))
		if readErr != nil || string(contents) != expected {
			t.Fatalf("artifact %q: contents=%q err=%v", path, contents, readErr)
		}
	}
	if _, _, _, _, err := Put(root, tenantA, taskID, "artifact_same", strings.NewReader("changed"), 1024, 0, ""); err == nil {
		t.Fatal("immutable artifact identifier accepted different bytes")
	}
}
