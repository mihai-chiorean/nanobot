#!/usr/bin/env bash
set -Eeuo pipefail

umask 077

backup_root="${ZIGGY_BACKUP_ROOT:-/var/backups/ziggy}"
retention_days="${ZIGGY_BACKUP_RETENTION_DAYS:-14}"
owner_workspace="${ZIGGY_OWNER_WORKSPACE:-/home/mihai/.nanobot/workspace}"
owner_config="${ZIGGY_OWNER_CONFIG:-/home/mihai/.nanobot/config.json}"
tenant_root="${ZIGGY_TENANT_ROOT:-/home/mihai/.local/share/ziggy/tenants}"
credential_store="${ZIGGY_CREDENTIAL_STORE:-/home/mihai/.config/credstore}"
user_systemd_dir="${ZIGGY_USER_SYSTEMD_DIR:-/home/mihai/.config/systemd/user}"
ziggy_config_dir="${ZIGGY_CONFIG_DIR:-/etc/ziggy}"
nanobot_release="${ZIGGY_NANOBOT_RELEASE:-/home/mihai/workspace/ziggy/current-nanobot}"
binary_dir="${ZIGGY_BINARY_DIR:-/usr/local/bin}"
system_unit_dir="${ZIGGY_SYSTEM_UNIT_DIR:-/etc/systemd/system}"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
snapshot_name="snapshot-${timestamp}"
staging="$(mktemp -d "${backup_root}/.${snapshot_name}.XXXXXX")"
destination="${backup_root}/${snapshot_name}"

cleanup() {
  rm -rf -- "${staging}"
}
trap cleanup EXIT

setpriv --reuid=ziggy-work --regid=ziggy-work --clear-groups -- \
  pg_dump --host=/var/run/postgresql --port=5433 --format=custom ziggy_work \
  >"${staging}/ziggy_work.dump"
setpriv --reuid=ziggy-connectors --regid=ziggy-connectors --clear-groups -- \
  pg_dump --host=/var/run/postgresql --port=5433 --format=custom ziggy_connectors \
  >"${staging}/ziggy_connectors.dump"

tar --create --gzip --file="${staging}/ziggy-work-state.tgz" \
  --directory=/var/lib ziggy-work

if [[ -d /var/lib/ziggy-control ]]; then
  tar --create --gzip --file="${staging}/ziggy-control-state.tgz" \
    --directory=/var/lib ziggy-control
fi
if [[ -f /etc/ziggy/tenants.json ]]; then
  install --mode=0600 /etc/ziggy/tenants.json "${staging}/tenants.json"
fi
if [[ -f "${owner_config}" ]]; then
  install --mode=0600 "${owner_config}" "${staging}/ziggy-owner-config.json"
fi
if [[ -d "${owner_workspace}" ]]; then
  owner_parent="$(dirname -- "${owner_workspace}")"
  owner_name="$(basename -- "${owner_workspace}")"
  tar --create --gzip --file="${staging}/ziggy-owner-workspace.tgz" \
    --directory="${owner_parent}" \
    --exclude="${owner_name}/.cache" \
    --exclude="${owner_name}/.venv" \
    --exclude="${owner_name}/venv" \
    --exclude="${owner_name}/node_modules" \
    --exclude="${owner_name}/*/node_modules" \
    --exclude="${owner_name}/rag" \
    --exclude="${owner_name}/rag.bak-*" \
    "${owner_name}"
fi
if [[ -d "${tenant_root}" ]]; then
  tenant_parent="$(dirname -- "${tenant_root}")"
  tenant_name="$(basename -- "${tenant_root}")"
  tar --create --gzip --file="${staging}/ziggy-tenant-workspaces.tgz" \
    --directory="${tenant_parent}" \
    --exclude="${tenant_name}/*/runtime" \
    --exclude="${tenant_name}/*/runtime/**" \
    "${tenant_name}"
  mapfile -d '' runtime_configs < <(
    find "${tenant_root}" -mindepth 3 -maxdepth 3 -type f \
      -path '*/runtime/config.json' -printf '%P\0'
  )
  if (( ${#runtime_configs[@]} > 0 )); then
    printf '%s\0' "${runtime_configs[@]}" |
      tar --create --gzip --null \
        --directory="${tenant_root}" \
        --file="${staging}/ziggy-tenant-runtime-configs.tgz" \
        --files-from=-
  fi
fi
if [[ -d "${credential_store}" ]]; then
  mapfile -d '' credential_files < <(
    find "${credential_store}" -maxdepth 1 -type f -name 'ziggy-mcp-*' -printf '%f\0'
  )
  if (( ${#credential_files[@]} > 0 )); then
    printf '%s\0' "${credential_files[@]}" |
      tar --create --gzip --null \
        --directory="${credential_store}" \
        --file="${staging}/ziggy-runtime-credentials.tgz" \
        --files-from=-
  fi
fi
if [[ -d "${user_systemd_dir}" ]]; then
  tar --create --gzip --file="${staging}/ziggy-user-systemd.tgz" \
    --directory="$(dirname -- "${user_systemd_dir}")" \
    "$(basename -- "${user_systemd_dir}")"
fi
if [[ -d "${ziggy_config_dir}" ]]; then
  tar --create --gzip --file="${staging}/ziggy-etc.tgz" \
    --directory="$(dirname -- "${ziggy_config_dir}")" \
    "$(basename -- "${ziggy_config_dir}")"
fi
if [[ -d "${nanobot_release}" ]]; then
  tar --create --gzip --file="${staging}/ziggy-nanobot-release.tgz" \
    --directory="$(dirname -- "${nanobot_release}")" \
    --exclude='*/.git' \
    --exclude='*/__pycache__' \
    --exclude='*/node_modules' \
    "$(basename -- "${nanobot_release}")"
fi
if [[ -d "${binary_dir}" ]]; then
  mapfile -d '' ziggy_binaries < <(
    find "${binary_dir}" -maxdepth 1 -type f \
      \( -name 'ziggy-*' -o -name 'provision-runtime-oauth-client*' \) \
      -printf '%f\0'
  )
  if (( ${#ziggy_binaries[@]} > 0 )); then
    printf '%s\0' "${ziggy_binaries[@]}" |
      tar --create --gzip --null \
        --directory="${binary_dir}" \
        --file="${staging}/ziggy-binaries.tgz" \
        --files-from=-
  fi
fi
if [[ -d "${system_unit_dir}" ]]; then
  mapfile -d '' ziggy_system_units < <(
    find "${system_unit_dir}" -mindepth 1 -maxdepth 1 \
      -name 'ziggy-*' -printf '%f\0'
  )
  if (( ${#ziggy_system_units[@]} > 0 )); then
    printf '%s\0' "${ziggy_system_units[@]}" |
      tar --create --gzip --null \
        --directory="${system_unit_dir}" \
        --file="${staging}/ziggy-system-units.tgz" \
        --files-from=-
  fi
fi

printf 'created_at=%s\nhost=%s\nrelease=%s\n' \
  "${timestamp}" "$(hostname)" "${ZIGGY_RELEASE:-unknown}" \
  >"${staging}/METADATA"
(
  cd "${staging}"
  sha256sum ./* >SHA256SUMS
)

mv -- "${staging}" "${destination}"
trap - EXIT

find "${backup_root}" -mindepth 1 -maxdepth 1 -type d \
  -name 'snapshot-*' -mtime "+${retention_days}" -exec rm -rf -- {} +
