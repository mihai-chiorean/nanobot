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
`runtime.Driver` interface through a durable manager-owned fence. Every
operation for one workspace holds the same lock across state checks and driver
side effects. Before a new generation can create resources, the manager
atomically persists its high-watermark in the mode-`0700` state directory. A
higher generation is rejected until the current generation has a durable
deletion tombstone. State files are mode `0600`, replaced through
write/fsync/rename/directory-fsync, retained after container deletion, and
validated on every operation. Malformed, insecure, or unwritable state fails
closed; an absent file is valid only for a workspace's first accepted
generation. Podman labels remain a secondary live-resource ownership check.
Container operations require matching manager-owner, workspace, and generation
labels both before stopping the systemd unit and immediately before removal.

`delete` is normal **generation cleanup**: it removes the container, its
generation-scoped config volume, its internal network, and the generated unit,
but preserves the manager-owned workspace data volume. `delete_tenant_data` is
the separate destructive operation; it refuses while a runtime exists and
requires the exact retained generation plus generation-independent
workspace-volume labels. Old `ensure_running`, `delete`, and
`delete_tenant_data` replays remain stale after manager restart. Tenant-data
deletion first persists a terminal intent, then removes the volume, then marks
completion. Both pending and completed terminal states reject every future
generation, so an intentionally destroyed tenant cannot be resurrected.
Generation cleanup proceeds only after `systemctl stop` succeeds or
`systemctl show` proves the unit inactive; tenant-data deletion verifies the
unit remains inactive again. Podman absence is accepted only from exit status
`1` returned by the resource-specific `container exists`, `volume exists`, or
`network exists` command. Exit status `125` and all other indeterminate results
fail closed and cannot produce a deletion tombstone.

Manager logs contain operation, workspace ID, generation, and result only.
They do not write prompts, workspace paths, credentials, config, command
output, or runtime stderr.

The Unix socket is JSON-lines only: one request line per connection, with a hard
4 KiB `max+1` read cap, 16 bounded request readers, 16 global lifecycle
operations, five-second read/write deadlines, and a 30-second request-scoped
driver deadline. After parsing, at most one operation per workspace is admitted
before global operation capacity is claimed; duplicates receive
`workspace_busy`, so one stalled workspace cannot fill the global operation
budget. Configuration cannot raise the limits above 64 KiB, 256 operations, or
one minute. Excess readers or unrelated operations are rejected instead of
creating unbounded handler goroutines.

Startup holds an exclusive `flock` on the adjacent lock file for the listener's
full lifetime. An existing socket is probed and removed only when it is a stale
Unix socket, so a second manager cannot unlink a live listener. The deployment
uses the root-provisioned mode-`0750`
`/run/ziggy-runtime-manager` directory and a mode-`0660` socket owned by the
configurable `ziggy-control` group. On Linux, `SO_PEERCRED` must identify either
the manager UID or a process whose primary GID is that configured group.
`delete_tenant_data` is more restricted: by default only the manager UID may
request it. Non-Linux peer handling exists only to keep development tests
buildable and is not a deployment boundary.

The 15-minute model/MCP capability lifetime is **Phase 1 canary-only**. This
manager currently reads a static policy at process start. Before production
migration, implement broker-side atomic credential rotation with overlapping
generation validation and an atomic manager policy reload that never starts a
runtime with partially rotated credentials.

## Profile

Each generated, generation-labeled Quadlet unit uses a pinned image digest, rootless
`UserNS=auto:size=65536`, non-root UID, read-only root filesystem, all Linux
capabilities dropped, no-new-privileges, bounded PID/memory/CPU limits, and an
internal network. The only writable runtime data is a tenant-named Podman
volume. A separate named config volume is mounted read-only at runtime and is
staged by a hard-coded, networkless helper in the reviewed image.

Quadlet files are published with the same atomic write/fsync/rename sequence as
manager state. The generated service keeps the host home read-only except for
rootless Podman's documented graphroot (`%h/.local/share/containers`), cache,
and runroot (`%t/containers`). The manager unit has the same Podman paths plus
its state and Quadlet directories. A host with custom `storage.conf` paths must
update and review these allowlists before use.

The Nanobot config is generated from scratch: `tools.exec.enable=false`,
`allowedEnvKeys=[]`, `restrictToWorkspace=true`, web tools disabled, and a
single model/MCP endpoint through `egress-proxy`. It does not import a user
config or operator environment.

The container health command now performs `GET /health`, requires HTTP 200, and
parses the exact Nanobot `{"status":"ok"}` response instead of accepting any TCP
listener. The current Nanobot gateway endpoint has no authentication or
contract-version field. Therefore this is only an application-level Phase 1
probe; authenticated, versioned readiness remains an explicit production
blocker.

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

Phase 1 policy is hard-capped at four workspaces, matching the aggregate
`400%` CPU, `4G` memory, and `1024` task slice and the per-runtime `1 CPU`,
`1G`, and `256` task limits. Four concurrent `UserNS=auto:size=65536`
allocations require at least 262,144 free, non-overlapping subordinate UIDs and
GIDs dedicated to this account. Named local volumes have no manager-enforced
disk quota, so this proof is not a production storage-isolation boundary.

See [the canary runbook](../../docs/runbooks/ziggy-rootless-runtime-canary.md)
for the operational sequence and rollback.
