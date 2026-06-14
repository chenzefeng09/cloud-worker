#!/usr/bin/env bash
# =============================================================================
# install.sh — install the sandbox-broker-only host firewall on a CubeSandbox
#              sandbox node (the nftables fallback to CubeVS allow_out).
# =============================================================================
#
# Run this ON EACH CubeSandbox sandbox node (the node running Cubelet /
# CubeHypervisor that hosts the microVM TAP devices). It resolves the
# tool-broker + cluster-DNS ClusterIPs, substitutes them into the ruleset, and
# loads it into the host's inet netfilter table. Idempotent: re-running
# replaces the `sandbox_egress` table.
#
# Prefer CubeVS allow_out (../sandbox-broker-only.policy.yaml). Use this only
# for the cases the policy.yaml WARNING describes (broker ClusterIP inside a
# CubeVS-guarded RFC-1918 range, or CubeVS disabled).
#
# PARAMETERS (env vars, with the helm in-cluster defaults):
#   TOOL_BROKER_ADDR   tool-broker ClusterIP            (required, no default)
#   TOOL_BROKER_PORT   tool-broker port                 (default: 8024)
#   CLUSTER_DNS_ADDR   CoreDNS/kube-dns ClusterIP        (required, no default)
#   SANDBOX_IIF        nft interface set for sandbox TAPs(default: { "cube*", "tap*" })
#
# Example:
#   TOOL_BROKER_ADDR=$(kubectl -n agent-platform get svc ap-tool-broker \
#       -o jsonpath='{.spec.clusterIP}') \
#   CLUSTER_DNS_ADDR=$(kubectl -n kube-system get svc kube-dns \
#       -o jsonpath='{.spec.clusterIP}') \
#   sudo -E ./install.sh
# -----------------------------------------------------------------------------
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="${HERE}/sandbox-broker-only.nft"

: "${TOOL_BROKER_ADDR:?set TOOL_BROKER_ADDR to the ap-tool-broker ClusterIP}"
: "${CLUSTER_DNS_ADDR:?set CLUSTER_DNS_ADDR to the cluster DNS ClusterIP}"
TOOL_BROKER_PORT="${TOOL_BROKER_PORT:-8024}"
SANDBOX_IIF="${SANDBOX_IIF:-{ \"cube*\", \"tap*\" \}}"

command -v nft >/dev/null 2>&1 || { echo "nftables (nft) not installed" >&2; exit 1; }

RENDERED="$(mktemp /tmp/sandbox-broker-only.XXXXXX.nft)"
trap 'rm -f "$RENDERED"' EXIT

# Substitute the `define` lines with the resolved values. We rewrite the four
# parameterised defines and leave the rest of the ruleset verbatim.
sed -E \
  -e "s|^define TOOL_BROKER_ADDR = .*|define TOOL_BROKER_ADDR = ${TOOL_BROKER_ADDR}|" \
  -e "s|^define TOOL_BROKER_PORT = .*|define TOOL_BROKER_PORT = ${TOOL_BROKER_PORT}|" \
  -e "s|^define CLUSTER_DNS_ADDR = .*|define CLUSTER_DNS_ADDR = ${CLUSTER_DNS_ADDR}|" \
  -e "s|^define SANDBOX_IIF = .*|define SANDBOX_IIF = ${SANDBOX_IIF}|" \
  "$SRC" > "$RENDERED"

echo "==> validating rendered ruleset"
nft -c -f "$RENDERED"

echo "==> loading sandbox_egress table"
nft -f "$RENDERED"

echo "==> active sandbox_egress table:"
nft list table inet sandbox_egress
echo "OK — sandbox egress restricted to tool-broker (${TOOL_BROKER_ADDR}:${TOOL_BROKER_PORT}) + DNS (${CLUSTER_DNS_ADDR}:53)."
