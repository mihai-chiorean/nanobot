#!/usr/bin/env bash
set -euo pipefail

# Read-only Phase 1 check. Run after the egress gateway is attached and before
# enabling Nanobot exec for a canary. It changes no containers or host policy.
if [[ $# -ne 3 ]]; then
  echo "usage: $0 RUNTIME_A RUNTIME_B EGRESS_GATEWAY" >&2
  exit 2
fi
runtime_a=$1
runtime_b=$2
egress=$3
for command in podman jq; do command -v "$command" >/dev/null; done

[[ $(podman info --format '{{.Host.Security.Rootless}}') == true ]]
[[ $(podman info --format '{{.Host.Security.SELinuxEnabled}}') == true || $(podman info --format '{{.Host.Security.AppArmorEnabled}}') == true ]]

inspect_runtime() {
  local runtime=$1 json network
  json=$(podman inspect "$runtime")
  [[ $(jq -r '.[0].HostConfig.ReadonlyRootfs' <<<"$json") == true ]]
  [[ $(jq -r '.[0].HostConfig.ImageVolumeMode' <<<"$json") == ignore ]]
  [[ $(jq -r '.[0].HostConfig.NetworkMode' <<<"$json") != host ]]
  [[ $(jq -r '.[0].HostConfig.PidsLimit' <<<"$json") == 256 ]]
  [[ $(jq -r '.[0].HostConfig.Memory' <<<"$json") -gt 0 ]]
  [[ $(jq -r '.[0].Config.Labels["io.ziggy.egress"]' <<<"$json") == enforce-required ]]
  [[ $(jq -r '.[0].Config.Labels["io.ziggy.generation"]' <<<"$json") =~ ^[1-9][0-9]*$ ]]
  [[ $(jq -r '.[0].HostConfig.Binds | length' <<<"$json") == 0 ]]
  [[ $(jq -r '[.[0].Mounts[] | select(.Type != "volume")] | length' <<<"$json") == 0 ]]
  [[ $(jq -r '[.[0].HostConfig.CapAdd // [] | .[]] | length' <<<"$json") == 0 ]]
  [[ $(jq -r '[.[0].HostConfig.SecurityOpt // [] | .[] | select(test("seccomp=unconfined"))] | length' <<<"$json") == 0 ]]
  [[ $(jq -r '[.[0].NetworkSettings.Ports | to_entries[]?.value[]?.HostIp] | all(. == "127.0.0.1")' <<<"$json") == true ]]
  network=$(jq -r '.[0].NetworkSettings.Networks | keys[0]' <<<"$json")
  [[ $(podman network inspect "$network" --format '{{.Internal}}') == true ]]
  [[ $(podman network inspect "$network" --format '{{range .Containers}}{{.Name}} {{end}}') == *"$egress"* ]]
}

inspect_runtime "$runtime_a"
inspect_runtime "$runtime_b"

generation_a=$(podman inspect "$runtime_a" --format '{{index .Config.Labels "io.ziggy.generation"}}')
generation_b=$(podman inspect "$runtime_b" --format '{{index .Config.Labels "io.ziggy.generation"}}')
[[ $generation_a != "$generation_b" || $runtime_a != "$runtime_b" ]]

# The gateway must publish an explicit enforcing policy, never audit-only mode.
gateway_json=$(podman inspect "$egress")
[[ $(jq -r '.[0].Config.Labels["io.ziggy.egress.enforcement"]' <<<"$gateway_json") == enforce ]]
[[ $(jq -r '.[0].Config.Labels["io.ziggy.egress.allowed_routes"]' <<<"$gateway_json") == model,mcp ]]

echo "static isolation checks passed; run the canary matrix before enabling exec"
