-- Apply after 002_runtime_allocation_isolation.sql. Existing terminal rows
-- predate destruction receipts, so they receive an explicit legacy marker
-- while all identifying and runtime credential material is scrubbed.
ALTER TABLE ziggy_tenant_users
    ADD COLUMN IF NOT EXISTS destruction_receipt TEXT;

UPDATE ziggy_tenant_users
SET destruction_receipt = COALESCE(destruction_receipt, 'legacy:' || md5(user_id)),
    expected_email = 'deleted-' || md5('ziggy:' || user_id) || '@invalid',
    clerk_subject = NULL
WHERE lifecycle_status = 'deleted';

UPDATE ziggy_tenant_runtime_allocations
SET upstream_url = '',
    upstream_bootstrap_secret = '',
    metadata = '{}'::jsonb
WHERE allocation_state = 'deleted';

DO $migration$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'ziggy_tenant_users'::regclass
          AND conname = 'ziggy_tenant_users_destruction_receipt_check'
    ) THEN
        ALTER TABLE ziggy_tenant_users
            ADD CONSTRAINT ziggy_tenant_users_destruction_receipt_check
            CHECK (
                (lifecycle_status = 'deleted' AND destruction_receipt IS NOT NULL AND btrim(destruction_receipt) <> '')
                OR (lifecycle_status <> 'deleted' AND destruction_receipt IS NULL)
            );
    END IF;
END
$migration$;

COMMENT ON COLUMN ziggy_tenant_users.destruction_receipt IS
    'SHA-256 digest of the external destruction receipt; legacy:* marks deletions completed before migration 003';
