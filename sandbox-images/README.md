# sandbox-images — agent sandbox images

Every image here runs the **Claude Agent SDK worker** (`worker-claude-sdk/`,
TypeScript, entrypoint `node /app/dist/main.js`) — the platform's only agent
runtime (see [ADR 0003](../docs/adr/0003-claude-agent-sdk-sole-agent-runtime.md)).
The AgentScope `base` / `bootstrap` / `overlay` / `office` / `matlab` images and
the pre-fetched `wheels/` directory have been removed.

```
claude-sdk.Dockerfile                    ghcr.io/agent-platform/claude-sdk-sandbox:0.1
                                         docker runtime (dev/CI) · always pre-warmed

claude-sdk-cube.Dockerfile               claude-sdk-cube:0.2-scientific
  │                                      CubeSandbox-native · scientific Python · CMake
  └─ claude-sdk-cube-matlab.Dockerfile   claude-sdk-cube:0.2-matlab-*
       └─ build-matlab-control-layer.sh      + Control System Toolbox
            └─ build-matlab-aerospace-layer.sh + Simulink / Aerospace products

claude-sdk-cube-worker-refresh.Dockerfile   swap only /app/dist + node_modules
                                            into an existing Cube image
```

## The runtime contract every image honours

* Reads the task from `/workspace/task.json` (docker) or the `AGENT_TASK_JSON`
  env var (cube).
* Reads `/workspace/.secrets.json` (mode `0600`) into the worker's environment,
  then unlinks it.
* Writes `/workspace/result.json` + `/workspace/events.jsonl`; streams token
  deltas and hot-state to Redis (or the tool-broker `/state` façade).
* Skills are host-mounted read-only under `/home/skills/<name>`.
* Non-root `agent` user, **uid/gid 1000** — the scheduler bind-mounts
  `/workspace` files owned by uid 1000, so a different uid makes
  `result.json` writes fail with `EACCES`.
* All model traffic goes through the worker's in-process adapter to the
  foundation LLM gateway with a per-tenant virtual key. No provider API key is
  baked into or passed to any image.

The full wire format is [contracts/worker-wire-contract.md](../contracts/worker-wire-contract.md).

## Default image: `claude-sdk.Dockerfile`

`ghcr.io/agent-platform/claude-sdk-sandbox:0.1` — `node:22-slim` plus a
production-only `node_modules` and the prebuilt worker `dist/`. It also installs
a Python toolchain the agent can call through Bash for office and data work:
`python-docx`, `openpyxl`, `python-pptx`, `pypdf`, `Pillow`, `lxml`,
`xlsxwriter`, `pandas`, `numpy`, `matplotlib`, `graphviz`, `diagrams`, with
`fonts-noto-cjk` wired into `matplotlibrc`. The build asserts these imports and
the CJK font resolve, so a broken image fails at build time.

**Two-step build (compile, then containerise).** The Dockerfile copies a
*prebuilt* `dist/` — it does not run `tsc` — so `worker-claude-sdk/dist/` must
exist before `docker build`:

```bash
# from the repo root — build context is the repo ROOT (note trailing ".")
cd worker-claude-sdk && npm ci && npm run build && cd ..
docker build \
  -f sandbox-images/claude-sdk.Dockerfile \
  -t ghcr.io/agent-platform/claude-sdk-sandbox:0.1 \
  .
```

Or just `make build-claude-sdk-image` (runs both steps) and
`make push-claude-sdk-image` from the repo root.

> CI note: `ci-worker-claude-sdk.yml` typechecks, tests and builds the worker;
> the sandbox images themselves are built manually via the Makefile targets.

## Cube-native scientific image

``claude-sdk-cube.Dockerfile`` layers the compiled Claude worker onto Cube's
native code-interpreter base. It keeps Cube's envd and ``/health`` probe while
adding NumPy, Pandas, Matplotlib, SciPy, common Office/PDF Python libraries,
Git, GCC/G++, Make, CMake, Graphviz, and CJK fonts.

```bash
make build-claude-sdk-cube-image \
  CLAUDE_SDK_CUBE_IMAGE=127.0.0.1:5000/claude-sdk-cube:0.2-scientific
```

Cube v0.6 fixes memory on a template, so one image can back multiple resource
tiers. The development deployment uses the same image digest for both
template aliases (``.env.example`` ships ``claude-sdk-matlab-aerospace-r2-2g`` /
``claude-sdk-matlab-aerospace-r2-4g``). Configure their aliases
with ``CUBE_CLAUDE_SDK_TEMPLATE_SMALL`` / ``CUBE_CLAUDE_SDK_TEMPLATE_LARGE``.
Runs default to the small tier, switch automatically for large attachments, or
can request the large tier explicitly with
``agent_config.sandbox_memory_tier=large``.

MATLAB is deliberately not installed by this target. MATLAB Runtime can only
run applications compiled by MATLAB Compiler; arbitrary ``.m`` execution and
the Python Engine require a full licensed MATLAB installation. Keep either
variant in a separate private image layer because the installer is multi-GB
and subject to MathWorks distribution terms.

## Add Control System Toolbox to the Cube MATLAB image

For an existing R2019b image, prepare a minimal installer directory from the
full ISO and build an incremental layer. The prepared directory is roughly
hundreds of MB instead of the complete 17 GB ISO. The build script bind-mounts
that directory into a temporary container and commits only the installed
toolbox files, never the installer itself.

```bash
sandbox-images/prepare-matlab-control-media.sh \
  /path/to/MatlabR2019b_lin64.iso \
  /tmp/matlab-control-media

make build-claude-sdk-cube-matlab-control-image \
  MATLAB_ISO_DIR=/tmp/matlab-control-media \
  CLAUDE_SDK_CUBE_MATLAB_BASE_IMAGE=127.0.0.1:5000/claude-sdk-cube:0.2-matlab-r4 \
  CLAUDE_SDK_CUBE_MATLAB_CONTROL_IMAGE=127.0.0.1:5000/claude-sdk-cube:0.2-matlab-control-r2
```

The build script runs a licensed `tf`/`dcgain` smoke test before and after
committing the image, and explicitly restores Cube's code-interpreter `CMD`.
Keep the prior Cube templates until the new image and both memory-tier
templates have passed the same smoke test, so rollback only requires restoring
the old template aliases.

## Add the core aerospace products

The aerospace media resolver reads MathWorks' R2019b `productdata` metadata and
extracts only the payloads for Simulink, Aerospace Toolbox, Aerospace Blockset,
Stateflow, and Simulink Control Design.

```bash
sandbox-images/prepare-matlab-aerospace-media.sh \
  /path/to/MatlabR2019b_lin64.iso \
  /tmp/matlab-aerospace-media

make build-claude-sdk-cube-matlab-aerospace-image \
  MATLAB_ISO_DIR=/tmp/matlab-aerospace-media
```

The incremental build validates a headless Simulink model, the Aerospace
Blockset library, the standard-atmosphere function, Stateflow's API, and
Simulink Control Design's operating-point API before committing the image.

## How the scheduler picks an image

For the docker runtime, the image is set globally per deployment via
`AGENT_IMAGE` / `SCHEDULER_AGENT_IMAGE` (Helm: `agentScheduler.config.agentImage`,
`sandboxPool.config.sandboxImage`); `sandbox-pool` pre-warms that image. For the
cube runtime the scheduler picks a Cube template alias
(`CUBE_CLAUDE_SDK_TEMPLATE_SMALL` / `CUBE_CLAUDE_SDK_TEMPLATE_LARGE`) per run.
There is no per-run worker selection any more — every image runs the same
worker.

## Pinning + upgrade

The image tag pins the worker build and, through
`worker-claude-sdk/package-lock.json`, the Claude Agent SDK version. Bumping
the SDK is a coordinated change:

1. Update `@anthropic-ai/claude-agent-sdk` in `worker-claude-sdk/` and run
   `npm run typecheck && npm test`.
2. Rebuild + retag (e.g. `0.1` → `0.2`); for Cube images with heavy layers use
   `make build-claude-sdk-cube-worker-refresh-image`.
3. Run the conformance suite against the new image.
4. Bump the `AGENT_IMAGE` / `SCHEDULER_AGENT_IMAGE` default (compose
   `.env.example`, Helm values, scheduler config) or the Cube template aliases.

## Compliance notes

* The Cube images pin their base image digests; the worker is installed with
  `npm ci` from the committed lockfile.
* Image signing via cosign and SBOM generation via syft are planned follow-ups.
* MATLAB installer media, license files and binaries are never committed
  (`license.lic` / `*.so` are git-ignored) and must not be redistributed.
