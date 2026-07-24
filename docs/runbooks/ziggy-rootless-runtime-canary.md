# Rootless Runtime Canary

This is a Phase 1 proof procedure. It does not change `ziggy-control` routing,
deploy a production runtime, or enable Nanobot shell execution by default.

## Preconditions

1. Use a dedicated unprivileged `ziggy-runtime` account with a systemd user
   session enabled through `loginctl enable-linger ziggy-runtime`. Give it valid,
   non-overlapping `/etc/subuid` and `/etc/subgid` ranges. Do not use the normal
   deployment account, `--userns=keep-id`, host networking, or a shared Podman
   socket.
2. Confirm cgroup v2, rootless Podman, Quadlet, an enforcing SELinux or AppArmor
   profile, and a working rootless `UserNS=auto` allocation. Install the manager
   binary and its user unit under that account.
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

## Canary Sequence

1. Install `ziggy-tenant.slice` and `ziggy-runtime-manager.service` as user
   units, run `systemctl --user daemon-reload`, then start the manager. Restrict
   its Unix socket group to the future control service only; do not expose it
   through HTTP or a host socket mount.
2. Send `ensure_running` for tenant A and B over the Unix socket. Capture the
   manager lifecycle logs, generated Quadlet unit checksum, image digest, and
   generated-config checksum. Do not archive generated config content.
3. Confirm health for both tenants and run:

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
   one to five canaries.

## Rollback

1. Withdraw the affected tenant's route at the existing control plane before
   touching its runtime. Do not point a client at a replacement generation first.
2. Send `drain`, then `stop`, using the currently fenced generation. Verify the
   process cgroup is empty and the loopback port no longer accepts connections.
3. If the runtime is compromised or policy verification fails, send `delete` to
   remove the container, config/root volumes, internal network, and generated
   Quadlet unit. Revoke its model/MCP capabilities at their brokers immediately.
4. Restore the tenant workspace only from a snapshot taken before the canary;
   keep external audit and forensic metadata outside the restored volume.
5. Return the tenant to the existing static, `exec=false` runtime only after
   the original routing and health checks pass. Do not reuse the old generation
   or capabilities. Preserve the failed image digest, Quadlet checksum, kernel,
   Podman version, and test output for investigation.
