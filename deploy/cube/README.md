# `deploy/cube/` — CubeSandbox per-microVM egress isolation (`sandbox-broker-only`)

These assets enforce, in production, the SAME reachability set as the dev
compose `internal: true` `sandbox_broker` network: an agent sandbox may reach
the **tool-broker façade** and **DNS** — and nothing else. Default-deny egress;
everything else (Redis, MinIO, Keycloak, mcp-registry, the public internet, the
cloud metadata IP `169.254.169.254`, and other RFC-1918 hosts) is dropped.

This is the prod analogue of the dev network: in dev a sandbox joins
`sandbox_broker` whose only peer is `tool-broker:8024`. Here we reproduce that
reachability set at the CubeSandbox microVM layer.

See `docs/design/b-m2-sandbox-runtime-v1.md §6` for the runtime design and
`deploy/helm/agent-platform/templates/sandbox-isolation.yaml` for why a k8s
`NetworkPolicy` does NOT apply (sandboxes are microVMs, not Pods).

## What enforces this (the mechanism — NOT a k8s CRD)

CubeSandbox has a **first-class per-sandbox egress primitive**: **CubeVS**, its
eBPF kernel-level virtual switch. It is configured **per sandbox at create
time** through the E2B sandbox-create call, which Cubelet translates into a
CubeVS `MVMOptions` struct:

| E2B create kwarg | CubeVS `MVMOptions` field | Effect |
| ---------------- | ------------------------- | ------ |
| `allow_internet_access=False` | `AllowInternetAccess=false` | drop all public-IP egress |
| `network={"allow_out": [CIDR…]}` | `AllowOut` (CIDR list) | forward only matching dests |
| `network={"deny_out": [CIDR…]}` | `DenyOut` (CIDR list) | drop matching dests |

CubeVS keys on **destination CIDR**, NOT on a sandbox label, and there is **no
CRD / CubeProxy policy file** for this. So our scheduler's
`agent-platform.network-policy=sandbox-broker-only` metadata label cannot be
consumed by CubeVS directly — an operator resolves that named policy into the
concrete CIDR allowlist + E2B create-shape in `sandbox-broker-only.policy.yaml`,
and points their CubeMaster/Cubelet admission glue at it.

> Source: CubeSandbox `docs/architecture/network.md` (CubeVS model, `MVMOptions`,
> `AddTAPDevice`) and `examples/network-policy/` (the `allow_internet_access` /
> `network.allow_out` / `network.deny_out` create kwargs).

The scheduler already stamps the selecting labels on every spawn
(`services/agent-scheduler/.../services/cube_runtime.py`, broker egress mode):

```
metadata "agent-platform.egress-mode"     = "broker"
metadata "agent-platform.network-policy"  = "sandbox-broker-only"
metadata "agent-platform.tool-broker-url" = "<in-cluster tool-broker URL>"
```

As of the CubeVS-shape change, the scheduler also emits the CubeVS create
overlay **directly** on every broker-mode spawn:
`allow_internet_access=false` plus `network.allow_out=<CIDRs>`, where the CIDRs
come from `SCHEDULER_CUBE_SANDBOX_ALLOW_OUT` (comma-separated). That config
**must** list the tool-broker + cluster-DNS CIDRs — the same two ClusterIPs from
prerequisite 4, which are NOT auto-discovered. Broker mode with an empty
`SCHEDULER_CUBE_SANDBOX_ALLOW_OUT` is a deployment error: the scheduler
fail-closes the spawn (an empty allowlist + `allow_internet_access=false`
air-gaps the sandbox from the broker). So a controller that honours CubeVS gets
the policy without the label→policy glue; the label above still drives the
nftables fallback for the RFC-1918 hard-deny caveat (see Tuning).

## Files

| File | Purpose |
| ---- | ------- |
| `sandbox-broker-only.policy.yaml` | The CubeVS-native policy: resolves the `sandbox-broker-only` label into the `allow_internet_access=false` + `allow_out` CIDR overlay Cubelet applies via the E2B create call. **The canonical asset — use this first.** |
| `nftables/sandbox-broker-only.nft` | Host-firewall **fallback** ruleset (valid `nft` syntax): default-deny egress, ALLOW tool-broker + DNS, DROP rest. For backends where CubeVS can't express the policy (broker ClusterIP inside a CubeVS-guarded RFC-1918 range, or CubeVS disabled). |
| `nftables/install.sh` | Renders the `.nft` parameters from env/`kubectl` and loads it on a sandbox node. Selects sandbox traffic by TAP interface name. |
| `nftables/sandbox-broker-only.service` | systemd unit to persist the host firewall across boots, ordered before Cubelet. |

## Prerequisites

1. **A CubeSandbox deployment** fronted by CubeProxy, reachable from the
   scheduler at `agentScheduler.cubeControllerUrl` (helm) /
   `SCHEDULER_CUBE_CONTROLLER_URL`.
2. **Broker egress mode on** in the scheduler so the labels are stamped:
   `agentScheduler.config.sandboxEgressMode=broker` (helm default). Without it
   no sandbox carries `agent-platform.network-policy=sandbox-broker-only` and
   there is nothing for this policy to select.
3. **The tool-broker deployed** (`toolBroker.enabled=true`, helm default) so
   `ap-tool-broker:8024` exists in-cluster. This is the only infra peer the
   policy allows.
4. **The two ClusterIPs resolved** for your cluster (they are NOT
   auto-discovered):

   ```bash
   # tool-broker façade
   kubectl -n agent-platform get svc ap-tool-broker -o jsonpath='{.spec.clusterIP}'
   # cluster DNS resolver
   kubectl -n kube-system get svc kube-dns -o jsonpath='{.spec.clusterIP}'
   ```

5. *(nftables fallback only)* **root on each sandbox node** and `nft`
   installed; knowledge of how your CubeSandbox build names the per-microVM TAP
   devices (default glob `cube*` / `tap*`).

## Apply

### Option A — CubeVS native (preferred)

1. Edit `sandbox-broker-only.policy.yaml`: set `reachability.toolBroker.cidr`
   and `reachability.clusterDns.cidr` (and the matching `e2bCreateOverlay.
   network.allow_out` entries) to the ClusterIPs from prerequisite 4.
2. Wire it into your CubeMaster/Cubelet admission glue so that any sandbox
   carrying `agent-platform.network-policy: sandbox-broker-only` is created with
   the `e2bCreateOverlay` — i.e. `allow_internet_access=false` +
   `network.allow_out=[tool-broker, DNS]`. (CubeSandbox has no built-in
   label→policy mapper; this glue is the integration point you own in front of
   CubeProxy.)

### Option B — host nftables fallback

Use when the policy.yaml WARNING applies (broker ClusterIP inside a
CubeVS-guarded range, or CubeVS off). On each sandbox node:

```bash
TOOL_BROKER_ADDR=$(kubectl -n agent-platform get svc ap-tool-broker -o jsonpath='{.spec.clusterIP}') \
CLUSTER_DNS_ADDR=$(kubectl -n kube-system get svc kube-dns -o jsonpath='{.spec.clusterIP}') \
sudo -E deploy/cube/nftables/install.sh
```

To persist across reboots, drop the resolved values in
`/etc/agent-platform/sandbox-egress.env`, copy `nftables/` to
`/opt/agent-platform/cube/nftables`, then:

```bash
sudo cp deploy/cube/nftables/sandbox-broker-only.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now sandbox-broker-only.service
```

## Verify isolation

The acceptance check is: **from inside a sandbox, the tool-broker is reachable
and everything else is refused.** Spawn a sandbox in broker mode (any normal
agent run does this) and exec into it, or use the E2B SDK's command API.

```sh
# 1. tool-broker — MUST SUCCEED (HTTP 200 on the health endpoint)
curl -sS -m 5 http://ap-tool-broker:8024/api/tool-broker/v1/healthz
#    -> {"status":"ok"}   (DNS resolved + connection allowed)

# 2. DNS — MUST SUCCEED (name resolution is allowed)
getent hosts ap-tool-broker        # returns the ClusterIP

# 3. Redis — MUST FAIL (connection refused / timeout)
curl -sS -m 5 redis://ap-redis:6379 ; echo "exit=$?"      # non-zero
nc -w3 -z ap-redis 6379 ; echo "exit=$?"                  # non-zero

# 4. MinIO / object store — MUST FAIL
curl -sS -m 5 http://ap-minio:9000/ ; echo "exit=$?"      # non-zero

# 5. Keycloak / identity — MUST FAIL
curl -sS -m 5 http://foundation-identity:8080/ ; echo "exit=$?"   # non-zero

# 6. Cloud instance metadata — MUST FAIL (no IMDS exfil)
curl -sS -m 5 http://169.254.169.254/latest/meta-data/ ; echo "exit=$?"  # non-zero

# 7. Public internet — MUST FAIL (default-deny egress)
curl -sS -m 5 https://example.com ; echo "exit=$?"        # non-zero
```

Expected: step 1–2 succeed; steps 3–7 all time out or are refused. That is the
exact reachability set the dev `internal: true` `sandbox_broker` network gives —
tool-broker only, plus DNS.

If you ran the nftables fallback, you can watch the drops on the node:

```bash
# add `counter` to the drop rules first if you want hit counts, then:
sudo nft list table inet sandbox_egress
sudo journalctl -k | grep -i drop   # if you also add a `log prefix "sbx-drop "` rule
```

## Tuning / adapt to your backend

- **Pick the mechanism honestly.** CubeVS (`policy.yaml`) is the native,
  preferred path. The nftables ruleset is a host-firewall fallback — it is NOT
  a turnkey CubeSandbox CRD, because CubeSandbox has no such CRD. If your
  CubeSandbox build, version, or fork exposes the egress policy differently than
  the `MVMOptions` / E2B-create shape documented here, adapt the `policy.yaml`
  `e2bCreateOverlay` to that shape — the *intent* (default-deny + allow only
  tool-broker + DNS) is what must hold.
- **CubeVS hard-deny of RFC-1918.** CubeVS always drops `10/8`, `127/8`,
  `169.254/16`, `172.16/12`, `192.168/16` regardless of `allow_out`. If your
  `ap-tool-broker` ClusterIP is inside one of these (common: `10.96.0.0/12`),
  CubeVS allowlist mode alone air-gaps the sandbox. Either expose the broker on
  a non-RFC-1918 IP to the sandbox netns, or use the nftables fallback (no such
  guard). The `policy.yaml` WARNING block covers this.
- **TAP selection (nftables).** `SANDBOX_IIF` defaults to `{ "cube*", "tap*" }`.
  If your backend names sandbox TAPs differently, or isolates by cgroup/netns
  rather than interface, change the `iifname` match in
  `sandbox-broker-only.nft` to a `meta cgroup` / `ct mark` match accordingly.
- **DNS scope.** The policy allows DNS only to the cluster resolver ClusterIP on
  port 53. If your nodes use a node-local DNS cache (e.g. NodeLocal DNSCache at
  `169.254.20.10`), add that address to `clusterDns` / `CLUSTER_DNS_ADDR` — and
  note it is link-local, so under CubeVS it hits the hard-deny guard and you
  must use the nftables fallback for it.
- **Multiple tool-broker replicas / non-pinned ClusterIP.** If the broker IP is
  not stable, use the Service CIDR (e.g. `10.96.0.0/12`) instead of a `/32` in
  `allow_out` — but narrow it as much as your environment allows; a wide
  allowlist weakens the isolation.
- **Keep in sync with the chart.** The broker host:port defaults
  (`ap-tool-broker:8024`) track `toolBroker` in
  `deploy/helm/agent-platform/values.yaml`. If you rename the Service or change
  the port, update both the policy CIDR/port and the nft defines.
