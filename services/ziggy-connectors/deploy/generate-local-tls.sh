#!/usr/bin/env bash
set -Eeuo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "run as root" >&2
  exit 1
fi

umask 077

secret_dir="${ZIGGY_SECRET_DIR:-/etc/ziggy/secrets}"
ca_key="${secret_dir}/connectors-local-ca-key"
ca_cert="${secret_dir}/connectors-local-ca-cert"
server_key="${secret_dir}/connectors-tls-key"
server_cert="${secret_dir}/connectors-tls-cert"
trust_cert="/usr/local/share/ca-certificates/ziggy-connectors-local-ca.crt"

for path in "${ca_key}" "${ca_cert}" "${server_key}" "${server_cert}" "${trust_cert}"; do
  if [[ -e "${path}" ]]; then
    echo "refusing to replace existing ${path}" >&2
    exit 1
  fi
done

install -d -m 0700 "${secret_dir}"
work_dir="$(mktemp -d)"
cleanup() {
  rm -rf -- "${work_dir}"
}
trap cleanup EXIT

openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 -out "${work_dir}/ca.key"
openssl req -x509 -new -sha256 -days 3650 \
  -key "${work_dir}/ca.key" \
  -subj "/CN=Ziggy Local Connector CA" \
  -out "${work_dir}/ca.crt"

openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 -out "${work_dir}/server.key"
openssl req -new -sha256 \
  -key "${work_dir}/server.key" \
  -subj "/CN=localhost" \
  -out "${work_dir}/server.csr"
cat >"${work_dir}/server.ext" <<'EOF'
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectAltName=DNS:localhost,IP:127.0.0.1,IP:::1
EOF
openssl x509 -req -sha256 -days 825 \
  -in "${work_dir}/server.csr" \
  -CA "${work_dir}/ca.crt" \
  -CAkey "${work_dir}/ca.key" \
  -CAcreateserial \
  -extfile "${work_dir}/server.ext" \
  -out "${work_dir}/server.crt"

install -m 0400 "${work_dir}/ca.key" "${ca_key}"
install -m 0444 "${work_dir}/ca.crt" "${ca_cert}"
install -m 0400 "${work_dir}/server.key" "${server_key}"
install -m 0444 "${work_dir}/server.crt" "${server_cert}"
install -m 0444 "${work_dir}/ca.crt" "${trust_cert}"
update-ca-certificates

openssl verify -CAfile "${trust_cert}" "${server_cert}"
echo "installed verified loopback TLS material"
