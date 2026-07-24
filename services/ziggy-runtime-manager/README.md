# Ziggy Runtime Manager

This service is the Phase 1 rootless Podman proof boundary for one Nanobot
runtime per tenant generation. It is deliberately not wired into
`ziggy-control`: it owns a narrow Unix-socket lifecycle contract and no public
HTTP listener.

The request protocol accepts only `operation`, `workspace_id`, and monotonic
`generation`. The manager-owned mode-`0600` policy selects the image digest,
tenant's loopback port, resource limits, named volumes, internal network, and
short-lived model/MCP capabilities. There is no request field for image,
command, environment, mount, device, network, secret, port, or Podman flag.

## Lifecycle contract

`ensure_running`, `health`, `drain`, `stop`, and `delete` map to the
`runtime.Driver` interface. Existing labels are checked before every action:
a newer observed generation rejects an old request as stale; an older live
generation rejects a replacement until the control plane has drained and
stopped it. This prevents accidental two-writer promotion, but it is not a
substitute for the PostgreSQL lease/fence required in Phase 2.

Manager logs contain operation, workspace ID, generation, and result only.
They do not write prompts, workspace paths, credentials, config, command
output, or runtime stderr.

## Profile

Each generated, generation-labeled Quadlet unit uses a pinned image digest, rootless
`UserNS=auto:size=65536`, non-root UID, read-only root filesystem, all Linux
capabilities dropped, no-new-privileges, bounded PID/memory/CPU limits, and an
internal network. The only writable runtime data is a tenant-named Podman
volume. A separate named config volume is mounted read-only at runtime and is
staged by a hard-coded, networkless helper in the reviewed image.

The Nanobot config is generated from scratch: `tools.exec.enable=false`,
`allowedEnvKeys=[]`, `restrictToWorkspace=true`, web tools disabled, and a
single model/MCP endpoint through `egress-proxy`. It does not import a user
config or operator environment.

`deploy/verify-isolation.sh` is read-only and validates rootless mode, LSM,
volume-only mounts, no capability additions, loopback-only ingress, internal
network membership, and an egress gateway that labels itself `enforce` with
only `model,mcp` routes.

## Egress boundary

The tenant network is created with `podman network create --internal`; without
the separately deployed dual-homed egress gateway, a runtime has no route to
the model or MCP broker and fails closed. The gateway is outside the tenant
namespace, attaches to each tenant internal network and a controlled uplink,
and must enforce only these routes:

- `egress-proxy:18080/model/v1` to the model gateway
- `egress-proxy:18080/mcp` to the tenant-scoped MCP broker

The manager does not deploy that trusted gateway or host firewall. Before a
canary, the operator must prove IPv4, IPv6, UDP, direct-IP, DNS-rebinding,
CONNECT, redirect, and alternate-port bypasses fail; the verification script
is intentionally not evidence of those dynamic tests by itself.

Use only a host where rootless Podman supports subordinate UID/GID ranges,
systemd user lingering for the dedicated `ziggy-runtime` account, SELinux or
AppArmor enforcement, cgroup v2, and user namespace allocation. Record the
effective UID/GID map with `podman inspect --format '{{.Id}} {{.Config.User}}'`
plus `/proc/<container-pid>/uid_map` and `gid_map` for both canaries.

See [the canary runbook](../../docs/runbooks/ziggy-rootless-runtime-canary.md)
for the operational sequence and rollback.
