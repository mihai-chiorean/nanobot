-- Apply after 003_terminal_deletion_receipt.sql. The identity checksum is
-- SHA-256 over the canonical lines "NNN:<file-sha256>\n" for migrations
-- 001 through 003. ziggy-control verifies this exact identity at readiness.
CREATE TABLE IF NOT EXISTS ziggy_schema_identity (
    component TEXT PRIMARY KEY CHECK (component = 'tenant_lifecycle'),
    schema_version INTEGER NOT NULL CHECK (schema_version > 0),
    migration_set_sha256 TEXT NOT NULL CHECK (migration_set_sha256 ~ '^[0-9a-f]{64}$'),
    migration_files JSONB NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO ziggy_schema_identity (
    component,
    schema_version,
    migration_set_sha256,
    migration_files
) VALUES (
    'tenant_lifecycle',
    3,
    '77d60a6ce09067523ecc9dd7e253452948b31f03cdc52f96c84bcf88fdd0bb6a',
    '{
      "001_tenant_lifecycle_foundation.sql": "dd3ba1bd8b128f6381f9e30be44aa7969eaee306a2a4922421667efcde07669e",
      "002_runtime_allocation_isolation.sql": "759dc043e312bb268d4052bfeaa24a16d8ffaa353e7cca142507e2d27d0e1c12",
      "003_terminal_deletion_receipt.sql": "f7e933def9673d31cc513f738f6b50035ab84b52e352544e477d1272f922d930"
    }'::jsonb
)
ON CONFLICT (component) DO UPDATE
SET schema_version = EXCLUDED.schema_version,
    migration_set_sha256 = EXCLUDED.migration_set_sha256,
    migration_files = EXCLUDED.migration_files,
    recorded_at = now();

REVOKE ALL ON ziggy_schema_identity FROM PUBLIC;
