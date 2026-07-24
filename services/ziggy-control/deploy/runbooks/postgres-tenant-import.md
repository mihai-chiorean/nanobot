# PostgreSQL Tenant Import

This runbook prepares a durable tenant database without changing the running
control service. It is an operator procedure, not a deployment command.

## Preconditions

- Keep the production service in `ZIGGY_TENANCY_MODE=manifest` throughout the
  import and validation steps.
- Retain the immutable `/etc/ziggy/tenants.json` and
  `/var/lib/ziggy-control/tenant-bindings.json` files. They are the rollback
  source of truth until the PostgreSQL cutover is accepted.
- Apply `migrations/001_tenant_lifecycle_foundation.sql` followed by
  `migrations/002_runtime_allocation_isolation.sql` with the database migration
  owner. The application/import role must not own schema migrations.
- Create `/etc/ziggy/secrets/tenant-database-url`, mode `0400`, containing only
  the PostgreSQL DSN. Do not put the DSN in the environment file or a command
  line.

## Import

Build the operator binary from the release revision:

```sh
make tenant-import
```

Run the dry run first. It performs the same serializable validation as import,
then rolls back and prints aggregate counts only:

```sh
sudo bin/import-tenant-manifest \
  --manifest /etc/ziggy/tenants.json \
  --bindings /var/lib/ziggy-control/tenant-bindings.json \
  --database-url-file /etc/ziggy/secrets/tenant-database-url \
  --dry-run
```

Run the same command without `--dry-run` to import. It is idempotent: matching
rows are reported as existing; a mismatched user, subject, workspace, runtime,
endpoint, or secret fails the complete transaction. The command never prints a
tenant identity, endpoint, DSN, or secret.

## Future Cutover

After independent validation, install the optional database credential drop-in:

```sh
sudo install -o root -g root -m 0644 \
  services/ziggy-control/deploy/systemd/ziggy-control.service.d/40-tenant-database-credential.conf \
  /etc/systemd/system/ziggy-control.service.d/40-tenant-database-credential.conf
```

Then, in one reviewed deployment change, set
`ZIGGY_TENANCY_MODE=postgres`, remove manifest/bindings variables from the
environment file, reload systemd, and restart the service. This runbook does
not authorize that deployment.

## Rollback

Leave PostgreSQL data intact. Restore `ZIGGY_TENANCY_MODE=manifest` and the
original manifest/bindings variables, remove the database credential drop-in,
then run `systemctl daemon-reload` and restart `ziggy-control`. Removing the
drop-in matters: its DSN environment variable is intentionally incompatible
with manifest mode. Re-run the importer only after resolving any reported
conflict; never edit imported runtime credentials in place to force a retry.
