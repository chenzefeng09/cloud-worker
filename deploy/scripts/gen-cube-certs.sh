#!/usr/bin/env bash
# =============================================================================
# gen-cube-certs.sh — generate self-signed mTLS certificate bundle for the
#                     CubeSandbox controller ↔ scheduler trust boundary.
#
# Creates three certificate pairs:
#   ca.{crt,key}                — self-signed root CA (one per environment)
#   controller.{crt,key}        — server cert for the cube-controller
#   scheduler-client.{crt,key}  — client cert for agent-scheduler + sandbox-pool
#
# Usage:
#   bash deploy/scripts/gen-cube-certs.sh [OUTPUT_DIR]
#
# OUTPUT_DIR defaults to ./deploy/certs (relative to the repo root, or the
# current directory if you run the script from elsewhere).
#
# IMPORTANT — these are development/staging self-signed certs.
# Do NOT use them in production — provision real certs from your PKI.
# Do NOT commit the generated files — they are in .gitignore.
#
# Requirements: openssl >= 1.1.1
# =============================================================================

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUTPUT_DIR="${1:-${REPO_ROOT}/deploy/certs}"
DAYS="${CERT_DAYS:-825}"          # ~2 years — rotate before expiry
KEY_BITS="${CERT_KEY_BITS:-4096}"
CURVE="${CERT_EC_CURVE:-}"        # if non-empty, use EC (e.g. prime256v1) instead of RSA

echo "==> Generating CubeSandbox mTLS certs in: ${OUTPUT_DIR}"
mkdir -p "${OUTPUT_DIR}"

# ---------------------------------------------------------------------------
# Helper: generate a key (RSA or EC depending on $CURVE)
# ---------------------------------------------------------------------------
_gen_key() {
    local out="$1"
    if [[ -n "${CURVE}" ]]; then
        openssl ecparam -genkey -name "${CURVE}" -noout -out "${out}"
    else
        openssl genrsa -out "${out}" "${KEY_BITS}" 2>/dev/null
    fi
}

# ---------------------------------------------------------------------------
# 1. Root CA
# ---------------------------------------------------------------------------
echo "  [1/3] Root CA"
_gen_key "${OUTPUT_DIR}/ca.key"
openssl req -x509 -new -nodes \
    -key "${OUTPUT_DIR}/ca.key" \
    -sha256 \
    -days "${DAYS}" \
    -subj "/C=CN/O=AgentPlatform/CN=CubeSandbox Dev CA" \
    -out "${OUTPUT_DIR}/ca.crt"

# ---------------------------------------------------------------------------
# 2. cube-controller server certificate
#    SAN must match the service name used inside docker compose networking
#    (cube-controller) plus localhost for local testing.
# ---------------------------------------------------------------------------
echo "  [2/3] Controller server certificate"
_gen_key "${OUTPUT_DIR}/controller.key"

cat > "${OUTPUT_DIR}/controller.ext" <<EOF
[req]
req_extensions = v3_req
distinguished_name = dn
[dn]
[v3_req]
subjectAltName = @alt_names
[alt_names]
DNS.1 = cube-controller
DNS.2 = localhost
IP.1  = 127.0.0.1
EOF

openssl req -new -nodes \
    -key "${OUTPUT_DIR}/controller.key" \
    -subj "/C=CN/O=AgentPlatform/CN=cube-controller" \
    -out "${OUTPUT_DIR}/controller.csr"

openssl x509 -req \
    -in "${OUTPUT_DIR}/controller.csr" \
    -CA "${OUTPUT_DIR}/ca.crt" \
    -CAkey "${OUTPUT_DIR}/ca.key" \
    -CAcreateserial \
    -days "${DAYS}" \
    -sha256 \
    -extfile "${OUTPUT_DIR}/controller.ext" \
    -extensions v3_req \
    -out "${OUTPUT_DIR}/controller.crt"

rm -f "${OUTPUT_DIR}/controller.csr" "${OUTPUT_DIR}/controller.ext"

# ---------------------------------------------------------------------------
# 3. Scheduler / sandbox-pool client certificate
#    Used by agent-scheduler and sandbox-pool to authenticate to cube-controller.
#    The CN is "agent-scheduler" — cube-controller should be configured to
#    accept any client cert signed by the CA (not just a specific CN).
# ---------------------------------------------------------------------------
echo "  [3/3] Scheduler client certificate"
_gen_key "${OUTPUT_DIR}/scheduler-client.key"

cat > "${OUTPUT_DIR}/scheduler-client.ext" <<EOF
[req]
req_extensions = v3_req
distinguished_name = dn
[dn]
[v3_req]
extendedKeyUsage = clientAuth
EOF

openssl req -new -nodes \
    -key "${OUTPUT_DIR}/scheduler-client.key" \
    -subj "/C=CN/O=AgentPlatform/CN=agent-scheduler" \
    -out "${OUTPUT_DIR}/scheduler-client.csr"

openssl x509 -req \
    -in "${OUTPUT_DIR}/scheduler-client.csr" \
    -CA "${OUTPUT_DIR}/ca.crt" \
    -CAkey "${OUTPUT_DIR}/ca.key" \
    -CAcreateserial \
    -days "${DAYS}" \
    -sha256 \
    -extfile "${OUTPUT_DIR}/scheduler-client.ext" \
    -extensions v3_req \
    -out "${OUTPUT_DIR}/scheduler-client.crt"

rm -f "${OUTPUT_DIR}/scheduler-client.csr" "${OUTPUT_DIR}/scheduler-client.ext"

# ---------------------------------------------------------------------------
# Harden permissions: private keys must not be world-readable.
# ---------------------------------------------------------------------------
chmod 600 "${OUTPUT_DIR}"/*.key
chmod 644 "${OUTPUT_DIR}"/*.crt

echo ""
echo "==> Done.  Files written to ${OUTPUT_DIR}:"
ls -lh "${OUTPUT_DIR}"/*.{crt,key}

echo ""
echo "  Verify with:"
echo "    openssl verify -CAfile ${OUTPUT_DIR}/ca.crt ${OUTPUT_DIR}/controller.crt"
echo "    openssl verify -CAfile ${OUTPUT_DIR}/ca.crt ${OUTPUT_DIR}/scheduler-client.crt"
echo ""
echo "  IMPORTANT: Do not commit these files.  They are in .gitignore."
echo "  Rotate before expiry: $(date -v+"${DAYS}"d '+%Y-%m-%d' 2>/dev/null || date --date="${DAYS} days" '+%Y-%m-%d' 2>/dev/null || echo "${DAYS} days from now")"
