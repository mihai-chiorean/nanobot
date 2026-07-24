# Rootless Runtime Canary

This is a Phase 1 proof procedure. It does not change `ziggy-control` routing,
deploy a production runtime, or enable Nanobot shell execution by default.

## Preconditions

1. Use a dedicated unprivileged `ziggy-runtime` account with a systemd user
   session enabled through `loginctl enable-linger ziggy-runtime`. Give it valid,
   non-overlapping `/etc/subuid` and `/etc/subgid` ranges containing at least
   262,144 free IDs dedicated to these four possible `auto:size=65536`
   allocations. Reserve another 65,536 IDs for every unrelated rootless
   container run by the account. Do not use the normal deployment account,
   `--userns=keep-id`, host networking, or a shared Podman socket.
2. Confirm cgroup v2, rootless Podman, Quadlet, an enforcing SELinux or AppArmor
   profile, a local non-NFS Podman graphroot, and a working rootless
   `UserNS=auto` allocation. Install the manager binary and its user unit under
   that account. Keep the default graphroot/runroot or update and review every
   `ReadWritePaths` allowlist in both the manager and generated Quadlets.
3. Build the runtime image from
   `services/ziggy-runtime-manager/deploy/image/Containerfile`, scan/sign it,
   record its digest, and place only the digest in the manager's mode-`0600`
   policy. Never use a tag.
4. Deploy a trusted, dual-homed egress gateway separately. Each tenant internal
   network must include exactly its runtime and that gateway. The gateway needs
   an enforcing allowlist for only `/model/v1` and `/mcp`, IPv4/IPv6/UDP bypass
   denial, request size/deadline limits, and generation-capability validation.
   Its controlled uplink may reach only the model gateway and tenant MCP broker.
5. Allocate two disposable workspaces, distinct loopback ports, distinct model
   and MCP capabilities, and different generations. Keep Nanobot exec disabled.
   The manager's 15-minute capability lifetime is canary-only; issue fresh
   capabilities immediately before each start and do not treat policy-file
   replacement as credential rotation.
6. Create the `ziggy-control` group. Make it a supplementary group of
   `ziggy-runtime`, and run the future control process with `ziggy-control` as
   its primary GID so Linux `SO_PEERCRED` authorization agrees with socket
   permissions. Install `deploy/tmpfiles.d/ziggy-runtime-manager.conf` as root
   and run `systemd-tmpfiles --create` before starting the user unit. Do not put
   the socket below `/run/user/<uid>`, whose mode-`0700` parent blocks
   cross-user traversal.
7. Create the manager state and Quadlet directories as `ziggy-runtime`, mode
   `0700`, on durable local storage. Never delete, restore backward, or copy
   individual generation state files; loss of the high-watermark invalidates
   the canary and requires reconciliation before restart.
8. Phase 1 accepts at most four policy workspaces. The installed slice supplies
   `400%` CPU, `4G` memory, and `1024` tasks, matching four per-runtime limits of
   `1 CPU`, `1G`, and `256` tasks. Named workspace volumes have no enforced
   quota. Use a dedicated filesystem with free capacity of at least the unpacked
   pinned image plus four recorded workspace test allowances plus 20% headroom;
   stop the canary if free space falls below that headroom. This disk model is
   not approved for production tenants.

## Canary Sequence

1. Install `ziggy-tenant.slice` and `ziggy-runtime-manager.service` as user
   units, run `systemctl --user daemon-reload`, then start the manager. Confirm
   `/run/ziggy-runtime-manager` is `0750 ziggy-runtime:ziggy-control`, the socket
   is `0660` with that group, and a second manager exits without replacing the
   live socket. Confirm an unrelated UID/GID is rejected. Do not expose the
   socket through HTTP or a host socket mount.
2. Send `ensure_running` for tenant A and B over the Unix socket. Capture the
   manager lifecycle logs, generated Quadlet unit checksum, image digest, and
   generated-config checksum. Do not archive generated config content.
3. Confirm health for both tenants. The generated probe must require HTTP 200
   and the exact Nanobot `{"status":"ok"}` body; a raw TCP listener must fail.
   This endpoint is neither authenticated nor versioned, so it does not satisfy
   a production readiness contract. Then run:

   ```sh
   services/ziggy-runtime-manager/deploy/verify-isolation.sh \
     ziggy-tenant-tenant-a ziggy-tenant-tenant-b trusted-egress-gateway
   ```

4. Record both container PID UID/GID maps from `/proc/<pid>/uid_map` and
   `/proc/<pid>/gid_map`; they must be distinct `auto` allocations. Record
   Podman inspect output as a restricted operational artifact, not in tenant
   workspace storage.
5. Run the adversarial Phase 1 matrix from
   `docs/research/tenant-agent-sandboxing.md`: cross-tenant path/process/socket
   attempts; IPv4/IPv6/UDP/direct-IP/proxy bypasses; environment/config/audit
   disclosure; resource exhaustion; forced stop; generation race; and deletion
   while the other tenant stays healthy. Test all egress denies with a tenant
   built client, not only proxy environment variables.
6. Review external gateway and lifecycle audit for every attempted denial. Only
   after every test passes may one disposable tenant enable Nanobot exec with
   bwrap configured to fail closed. Re-run the full matrix before moving from
   one to four canaries.

Production migration is blocked on atomic broker credential rotation and an
atomic manager policy reload. The reload must preserve a complete old or new
credential set for each runtime generation, coordinate with the PostgreSQL
lease/fence, and revoke old capabilities only after the replacement is fenced
and healthy. It is also blocked on an authenticated, versioned Nanobot
readiness contract and enforceable per-workspace storage quotas.

## Rollback

1. Withdraw the affected tenant's route at the existing control plane before
   touching its runtime. Do not point a client at a replacement generation first.
2. Send `drain`, then `stop`, using the currently fenced generation. Verify the
   process cgroup is empty and the loopback port no longer accepts connections.
3. If the runtime is compromised or policy verification fails, send `delete` to
   remove the container, generation config volume, internal network, and
   generated Quadlet unit. It preserves the workspace volume for recovery or a
   replacement generation. Revoke its model/MCP capabilities at their brokers
   immediately. Never remove the manager's generation state; replacements must
   use a strictly higher generation.
4. Restore the tenant workspace only from a snapshot taken before the canary;
   keep external audit and forensic metadata outside the restored volume.
5. Return the tenant to the existing static, `exec=false` runtime only after
   the original routing and health checks pass. Do not reuse the old generation
   or capabilities. Preserve the failed image digest, Quadlet checksum, kernel,
   Podman version, and test output for investigation.

Use `delete_tenant_data` only for an intentional tenant-data destruction after
the runtime is absent and retention approval is recorded. The control-group
peer is intentionally forbidden from this operation; an approved operator must
invoke it as the `ziggy-runtime` manager UID. It is not part of generation
upgrade or ordinary rollback. Once accepted, its durable terminal intent blocks
every future `ensure_running` generation for that workspace; reallocation
requires a separately reviewed new workspace identity, never state-file removal.
