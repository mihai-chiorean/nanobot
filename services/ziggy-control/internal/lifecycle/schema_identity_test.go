package lifecycle

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestExpectedSchemaIdentityMatchesMigrationFiles(t *testing.T) {
	var canonical strings.Builder
	for version, filename := range []string{
		"001_tenant_lifecycle_foundation.sql",
		"002_runtime_allocation_isolation.sql",
		"003_terminal_deletion_receipt.sql",
	} {
		contents, err := os.ReadFile(filepath.Join("..", "..", "migrations", filename))
		if err != nil {
			t.Fatal(err)
		}
		fileDigest := sha256.Sum256(contents)
		fmt.Fprintf(&canonical, "%03d:%s\n", version+1, hex.EncodeToString(fileDigest[:]))
	}
	identity := sha256.Sum256([]byte(canonical.String()))
	got := hex.EncodeToString(identity[:])
	if got != expectedSchemaIdentitySHA256 {
		t.Fatalf("schema identity = %s, want %s; add a migration and update the identity instead of editing an applied migration", got, expectedSchemaIdentitySHA256)
	}

	identityMigration, err := os.ReadFile(filepath.Join("..", "..", "migrations", "004_schema_identity.sql"))
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(identityMigration), expectedSchemaIdentitySHA256) {
		t.Fatal("schema identity migration does not record the binary's expected checksum")
	}
}
