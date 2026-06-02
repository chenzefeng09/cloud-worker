# ADR 0001 — Sandbox runtime: CubeSandbox default, Kata Containers as fallback

- **Status**: Accepted
- **Date**: 2026-06
- **Phase**: B-M2
- **Plan reference**: top-plan [§ 决策 — 沙盒（B 阶段）] + [§ 风险 #4]

## Context

Agent workloads execute untrusted, model-generated code (file I/O, network,
subprocesses).  Plain Docker containers share the host kernel, which is the
wrong primitive for this — a syscall-level escape lands the attacker on the
host.  B-M1 used Docker as a stop-gap so the end-to-end flow could land first;
B-M2 is where we replace the runtime with proper kernel isolation.

The top-plan locks in **CubeSandbox** (Tencent, microVM + E2B SDK protocol) as
the default and lists **Kata Containers** as the fallback (risk row #4).
This ADR captures *why* we're keeping CubeSandbox in front of Kata even
though Kata has the longer track record, and *how* we limit the blast
radius if CubeSandbox doesn't pan out.

## Decision drivers

1. **Kernel isolation is non-negotiable.**  Both candidates deliver it via
   a microVM under containerd's `shim v2` interface, so the upgrade from
   Docker is real either way.
2. **E2B SDK compatibility keeps the sandbox runtime swap a 1-line config change.**
   CubeSandbox speaks the E2B HTTP protocol, so ``cube_runtime.py`` is a
   thin httpx client.  If we ever needed AgentScope to use a hosted E2B
   instead, the same code path works.  With Kata we'd have to write an
   adapter that maps spawn/exec onto raw containerd / shim-v2 calls.
   See ADR 0002 for the AgentScope decision that makes this concrete.
3. **Startup latency caps useful agent throughput.**  CubeSandbox's
   bare-metal benchmark shows P95 ≈90 ms cold start; Kata + Cloud
   Hypervisor sits at ~250 ms.  At 100 concurrent active sessions a 160 ms
   spawn delta translates to ~15 RPS of additional pool warm-up capacity
   we don't have to provision for.
4. **Operational maturity matters.**  CubeSandbox is < 3 months public
   (first commit 2026-04-10, current release 0.3.0); Kata is CNCF-graduated
   with multi-cloud production deployments.
5. **The exit cost from a wrong choice is bounded.**  Both runtimes plug in
   at the same point (containerd shim + RuntimeClass).  Our own
   `SandboxRuntime` interface (see ADR 0002 / B-M2 design) gives us a
   single seam to swap implementations without touching agent-scheduler
   call sites.

## Decision

* **Default runtime: CubeSandbox.**  Driven by the E2B compatibility (#2)
  and lower startup latency (#3).  We accept the operational-maturity risk
  in exchange.
* **Fallback runtime: Kata Containers + Cloud Hypervisor.**  Wired into
  the same `SandboxRuntime` interface; selected per deployment via
  `SANDBOX_RUNTIME=cube|kata|docker` env var.
* **Dev/CI runtime: Docker (the B-M1 implementation).**  Stays in tree
  behind the same interface so contributors don't need KVM-capable hosts
  for local work.

## Risk management

Four risks, each with the early-warning signal and the agreed response:

| Risk | Signal | Response |
|---|---|---|
| CubeSandbox abandons E2B compat or breaks API in 0.x churn | Quarterly upgrade smoke test fails | Pin to last-good version; cut over to Kata via `SANDBOX_RUNTIME=kata` (single deploy var) |
| Real-world memory overhead far above the 5 MB/instance claim | `sandbox-pool` exporter shows MB/active-VM > 100 | Re-benchmark; reduce idle pool; if structural, cut to Kata |
| K8s production deployment guide stays missing (CubeSandbox issue #147 unresolved at our scale) | We hit a deployment edge case that requires source diving | Engage Tencent maintainers; if unanswered ≥2 weeks, cut to Kata |
| Severe CVE in CubeSandbox with no upstream patch | Vendor advisory | Cut to Kata immediately; CubeSandbox containers drained within 1h |

The escape hatch is **always** the runtime env var.  We never let
CubeSandbox-specific concepts leak into agent-scheduler or sandbox-pool
business logic; everything goes through `SandboxRuntime` ABC.

## Consequences

* B-M2 must ship the `SandboxRuntime` abstraction *before* writing the
  CubeSandbox-specific code, so the Kata fallback can be slotted in
  later without refactor.
* `services/agent-scheduler/src/agent_scheduler/services/docker_runtime.py`
  (B-M1) becomes one of three implementations of the abstraction.  It's
  kept as the dev/CI runtime — removing it would force every contributor
  to provision KVM.
* The sandbox image (``sandbox-images/base.Dockerfile``) installs the
  pinned AgentScope plus ``agentscope_ext.runtime_worker`` so any runtime
  (docker / cube / kata) can invoke the same entrypoint:
  ``python -m agentscope_ext.runtime_worker``.  See ADR 0002 + the B-M2
  design doc.
* Sandbox images (`sandbox-images/base|office|matlab.Dockerfile`) target
  the OCI spec, not CubeSandbox-specific image format, so the same
  images run on Kata if we cut over.

## Follow-ups tracked

* Quarterly CubeSandbox upgrade & compatibility-smoke-test routine — owner
  Platform SRE, first run 2026-09.
* Independent memory benchmark at 100 concurrent VMs before GA — owner
  Platform SRE, before B-M6 load test.
* Document the cutover procedure (Kata RuntimeClass + image pull + drain)
  before B-M2 sign-off — owner: this ADR's author, captured in
  `docs/design/b-m2-sandbox-runtime-v1.md § 8`.
