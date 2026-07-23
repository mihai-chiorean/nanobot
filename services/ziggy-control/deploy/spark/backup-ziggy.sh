#!/usr/bin/env bash
set -Eeuo pipefail

umask 077

backup_root="${ZIGGY_BACKUP_ROOT:-/var/backups/ziggy}"
retention_days="${ZIGGY_BACKUP_RETENTION_DAYS:-14}"
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
