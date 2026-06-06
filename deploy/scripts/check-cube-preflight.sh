#!/usr/bin/env bash
# =============================================================================
# check-cube-preflight.sh — pre-flight checks before enabling CubeSandbox
#
# Run this on the staging host before starting agent-platform.cube.yml.
# Each check prints PASS or FAIL; the script exits non-zero on any failure.
#
# Usage:
#   bash deploy/scripts/check-cube-preflight.sh [--certs-dir <path>]
#
# Options:
#   --certs-dir <path>  Directory containing the mTLS certs (default: ./deploy/certs)
#   --controller-url    cube-controller URL to reachability-check (default: https://cube-controller:8443)
#
# Exit codes:
#   0 — all checks passed
#   1 — one or more checks failed (details printed to stdout)
# =============================================================================

set -uo pipefail

# ---------------------------------------------------------------------------
# Defaults / arg parsing
# ---------------------------------------------------------------------------
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CERTS_DIR="${REPO_ROOT}/deploy/certs"
CONTROLLER_URL="https://cube-controller:8443"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --certs-dir) CERTS_DIR="$2"; shift 2 ;;
        --controller-url) CONTROLLER_URL="$2"; shift 2 ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

FAIL=0

pass() { echo "  [PASS] $*"; }
fail() { echo "  [FAIL] $*"; FAIL=1; }
info() { echo "  [INFO] $*"; }

# ---------------------------------------------------------------------------
# 1. KVM / virtualisation
# ---------------------------------------------------------------------------
echo ""
echo "=== [1] KVM / virtualisation ==="

if [[ -e /dev/kvm ]]; then
    pass "/dev/kvm exists"
else
    fail "/dev/kvm not found — enable KVM on this host (check BIOS/kernel modules)"
fi

if [[ -r /dev/kvm ]]; then
    pass "/dev/kvm is readable by current user ($(whoami))"
else
    fail "/dev/kvm is not readable — add current user to 'kvm' group or run as root"
fi

# egrep-style check for kvm in lsmod
if lsmod 2>/dev/null | grep -qE '^kvm\b'; then
    pass "kvm kernel module loaded"
else
    info "kvm module not found in lsmod — may still work if built-in"
fi

# CPU virtualisation flags
if grep -qE 'vmx|svm' /proc/cpuinfo 2>/dev/null; then
    pass "CPU hardware virtualisation (vmx/svm) flags present"
else
    fail "CPU virtualisation flags (vmx/svm) not in /proc/cpuinfo — nested virt may be disabled"
fi

# ---------------------------------------------------------------------------
# 2. mTLS certificate files
# ---------------------------------------------------------------------------
echo ""
echo "=== [2] mTLS certificate files (${CERTS_DIR}) ==="

REQUIRED_FILES=(
    "ca.crt"
    "controller.crt"
    "controller.key"
    "scheduler-client.crt"
    "scheduler-client.key"
)

all_certs_ok=true
for f in "${REQUIRED_FILES[@]}"; do
    path="${CERTS_DIR}/${f}"
    if [[ -f "${path}" ]]; then
        pass "${f} exists"
    else
        fail "${f} missing — run: bash deploy/scripts/gen-cube-certs.sh"
        all_certs_ok=false
    fi
done

if [[ "${all_certs_ok}" == "true" ]]; then
    # Verify signatures
    if openssl verify -CAfile "${CERTS_DIR}/ca.crt" "${CERTS_DIR}/controller.crt" &>/dev/null; then
        pass "controller.crt verifies against ca.crt"
    else
        fail "controller.crt fails verification against ca.crt — regenerate certs"
    fi

    if openssl verify -CAfile "${CERTS_DIR}/ca.crt" "${CERTS_DIR}/scheduler-client.crt" &>/dev/null; then
        pass "scheduler-client.crt verifies against ca.crt"
    else
        fail "scheduler-client.crt fails verification against ca.crt — regenerate certs"
    fi

    # Check expiry (warn if < 30 days remaining)
    for f in controller.crt scheduler-client.crt; do
        expiry_epoch=$(openssl x509 -enddate -noout -in "${CERTS_DIR}/${f}" 2>/dev/null \
            | sed 's/notAfter=//' \
            | date -f - +%s 2>/dev/null || echo 0)
        now_epoch=$(date +%s)
        days_left=$(( (expiry_epoch - now_epoch) / 86400 ))
        if [[ ${days_left} -gt 30 ]]; then
            pass "${f} valid for ${days_left} days"
        elif [[ ${days_left} -gt 0 ]]; then
            info "${f} expires in ${days_left} days — schedule rotation"
        else
            fail "${f} has expired — regenerate certs"
        fi
    done

    # Check key permissions
    for f in controller.key scheduler-client.key; do
        perms=$(stat -c '%a' "${CERTS_DIR}/${f}" 2>/dev/null \
            || stat -f '%OLp' "${CERTS_DIR}/${f}" 2>/dev/null \
            || echo "unknown")
        if [[ "${perms}" == "600" || "${perms}" == "400" ]]; then
            pass "${f} permissions are ${perms} (restrictive)"
        else
            fail "${f} permissions are ${perms} — should be 600 (chmod 600 ${CERTS_DIR}/${f})"
        fi
    done
fi

# ---------------------------------------------------------------------------
# 3. CUBE_CONTROLLER_IMAGE env var
# ---------------------------------------------------------------------------
echo ""
echo "=== [3] Environment variables ==="

if [[ -n "${CUBE_CONTROLLER_IMAGE:-}" ]]; then
    pass "CUBE_CONTROLLER_IMAGE=${CUBE_CONTROLLER_IMAGE}"
else
    fail "CUBE_CONTROLLER_IMAGE is not set — set it to the CubeSandbox controller OCI image URI"
fi

if [[ -n "${SCHEDULER_RUNTIME:-}" ]]; then
    if [[ "${SCHEDULER_RUNTIME}" == "cube" ]]; then
        pass "SCHEDULER_RUNTIME=cube"
    else
        info "SCHEDULER_RUNTIME=${SCHEDULER_RUNTIME} (will be overridden by the cube compose file)"
    fi
else
    info "SCHEDULER_RUNTIME not set (will be set to 'cube' by agent-platform.cube.yml)"
fi

# ---------------------------------------------------------------------------
# 4. cube-controller reachability (optional — only if already started)
# ---------------------------------------------------------------------------
echo ""
echo "=== [4] cube-controller reachability (${CONTROLLER_URL}) ==="
info "Skipping live reachability check — start cube-controller first, then rerun with --controller-url"
info "Manual check after startup:"
echo ""
echo "    curl -f --cacert ${CERTS_DIR}/ca.crt \\"
echo "         --cert ${CERTS_DIR}/scheduler-client.crt \\"
echo "         --key ${CERTS_DIR}/scheduler-client.key \\"
echo "         ${CONTROLLER_URL}/healthz"
echo ""

# ---------------------------------------------------------------------------
# 5. Docker / compose version
# ---------------------------------------------------------------------------
echo "=== [5] Docker / compose toolchain ==="
if docker version &>/dev/null; then
    docker_ver=$(docker version --format '{{.Server.Version}}' 2>/dev/null || echo "unknown")
    pass "Docker daemon accessible (version ${docker_ver})"
else
    fail "Docker daemon not accessible — check docker socket or DOCKER_HOST"
fi

if docker compose version &>/dev/null; then
    compose_ver=$(docker compose version --short 2>/dev/null || echo "unknown")
    pass "docker compose plugin available (version ${compose_ver})"
else
    fail "docker compose plugin not found — install docker-compose-plugin"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
if [[ ${FAIL} -eq 0 ]]; then
    echo "==> All pre-flight checks PASSED.  Ready to start CubeSandbox stack."
    echo ""
    echo "    Next step:"
    echo "      docker compose \\"
    echo "        -f deploy/docker-compose/agent-platform.yml \\"
    echo "        -f deploy/docker-compose/agent-platform.cube.yml \\"
    echo "        up -d"
else
    echo "==> One or more pre-flight checks FAILED.  Fix the issues above before starting."
    exit 1
fi
