# PostgreSQL Tenant Import

This runbook prepares a durable tenant database without changing the running
control service. It is an operator procedure, not a deployment command.

## Preconditions

- Keep the production service in `ZIGGY_TENANCY_MODE=manifest` throughout the
  import and validation steps.
- Retain the immutable `/etc/ziggy/tenants.json` and
  `/var/lib/ziggy-control/tenant-bindings.json` files. They are the rollback
  source of truth until the PostgreSQL cutover is accepted.
- Apply migrations `001` through `004` in filename order with the database
  migration owner. Migration `004` records the checksummed identity required by
  service readiness. Applied migrations `001` through `003` are immutable.
  Migration `004` is replay-safe but never overwrites an existing identity; a
  mismatch must be investigated rather than repaired by replaying the file.
- Create separate `ziggy_control` and `ziggy_tenant_import` login roles. Neither
  role may own the database, schema, tables, migrations, or lifecycle event
  sequence.
- As the migration owner, apply the explicit grants after replacing the three
  psql variables with the deployment's database and role names:

  ```sh
  psql --set=database_name=ziggy \
    --set=control_role=ziggy_control \
    --set=import_role=ziggy_tenant_import \
    --file=deploy/postgres/least-privilege-grants.sql
  ```

  The control role has only the column writes needed for lifecycle operations.
  The importer cannot update existing lifecycle state, runtime allocations, or
  schema identity. Neither role can update/delete audit events or mutate schema
  identity.
- Create `/etc/ziggy/secrets/tenant-database-url`, mode `0400`, containing only
  the `ziggy_control` PostgreSQL DSN. Give the import command a separate
  credential file containing the `ziggy_tenant_import` DSN. Do not put either
  DSN in the environment file or a command line. Use an absolute Unix-socket
  host for local peer authentication or `sslmode=verify-full` for a network
  connection.

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
tenant identity, endpoint, DSN, or secret. Imports are deterministically sorted
and serialized by a PostgreSQL transaction advisory lock. SQLSTATE `40001` and
`40P01` are retried within the command deadline; uniqueness or durable-state
conflicts are not retried.

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
