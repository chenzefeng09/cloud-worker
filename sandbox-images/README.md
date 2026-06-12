# sandbox-images — agent-side container/microVM images

> **Smoke-test note (B-M1):** ``base.Dockerfile`` is the *only* image
> required to run the local end-to-end smoke (login → session → LLM
> reply).  It currently ships AgentScope 2.x + httpx + pydantic + pypdf
> and intentionally omits Chromium/Playwright to keep the image ~700 MB
> and the smoke-build under 6 minutes.  ``office.Dockerfile`` and
> ``matlab.Dockerfile`` are layered tiers used by later milestones and
> are not required for the smoke flow.

Three layers, each adds capability on top of the previous:

```
agentscope-sandbox-base    ─── always pre-warmed
       ▲
       │
agentscope-sandbox-office  ─── + LibreOffice headless (Word/Excel/PPT IO)
       ▲
       │
agentscope-sandbox-matlab  ─── + MATLAB Runtime + Engine for Python
                                (requires license server at runtime)
```

The image the scheduler launches is controlled by the ``SCHEDULER_AGENT_IMAGE``
env var (per-deployment) and the ``template`` field of the
``sandbox-pool::acquire`` payload (per-run, B-M3).

## What ships in every image

* Python 3.12 + ``pip``
* Pinned [AgentScope 2.x](https://github.com/agentscope-ai/agentscope)
* Our extensions: ``agentscope-ext/runtime_worker`` + ``event_audit_sink``
  + ``llm_gateway_enforcer`` + ``mcp_tenant_injector`` + ``skills_loader``
  (installed from the repo as editable wheels; pinned by image tag)
* Entrypoint: ``python -m agentscope_ext.runtime_worker``  (contract in
  ``docs/design/b-m2-sandbox-runtime-v1.md § 2.2``)
* Non-root ``agent`` user (uid 1000) — top-plan §安全要求, mirrors the
  agent-platform services convention.

## Per-tier additions

| Image | Adds | Why this tier exists | Approx size |
|---|---|---|---|
| ``base``   | NumPy / Pandas / Polars / Requests / Node 20 / Playwright + Chromium | Default agent workload — data wrangling, web scraping, code gen | ~1.6 GiB |
| ``office`` | LibreOffice headless + ``unoconv`` | Generate / read .docx / .xlsx / .pptx for document agents | ~2.4 GiB |
| ``matlab`` | MATLAB Runtime + ``matlabengine`` Python binding | Customer demand: numerical scripts; **needs license server at runtime** | ~5 GiB |

## How to build

```bash
# base — everyone needs this
docker build -t agentscope-sandbox-base:0.1 -f base.Dockerfile ..

# office — layered on base
docker build -t agentscope-sandbox-office:0.1 -f office.Dockerfile ..

# matlab — layered on base; MATLAB R2024a runtime installer must be
# present at build time (NOT included in repo — see matlab.Dockerfile)
docker build -t agentscope-sandbox-matlab:0.1 -f matlab.Dockerfile ..
```

> All three build contexts are the agent-platform repo root (note the
> trailing ``..``) so the Dockerfiles can ``COPY agentscope-ext/`` into
> the image without a separate vendor step.

## Claude SDK worker image (separate lineage)

``claude-sdk.Dockerfile`` builds ``ghcr.io/agent-platform/claude-sdk-sandbox:0.1``
— a **TypeScript/Node.js** worker, NOT part of the AgentScope/Python lineage
above.  It runs the ``worker-claude-sdk/`` worker (Claude Agent SDK ⇄
llm-gateway) on ``node:22-slim`` and honours the same runtime contract:
reads ``/workspace/task.json`` or ``AGENT_TASK_JSON``, writes
``/workspace/result.json`` + ``/workspace/events.jsonl``, skills under
``/home/skills/``.  Same non-root ``agent`` user (uid/gid 1000) so the
scheduler's bind-mounted ``/workspace`` files are writable.

**Two-step build (compile, then containerise).**  The Dockerfile copies a
*prebuilt* ``dist/`` — it does not run ``tsc``.  So ``worker-claude-sdk/dist/``
must exist before ``docker build``.  Ordered command:

```bash
# from the repo root — build context is the repo ROOT (note trailing ".")
cd worker-claude-sdk && npm ci && npm run build && cd ..
docker build \
  -f sandbox-images/claude-sdk.Dockerfile \
  -t ghcr.io/agent-platform/claude-sdk-sandbox:0.1 \
  .
```

Or just ``make build-claude-sdk-image`` (runs both steps) and
``make push-claude-sdk-image`` from the repo root.

> Prerequisite: ``worker-claude-sdk/dist/main.js`` is produced by
> ``npm run build`` (``tsc``).  If you build before the worker code's
> ``dist/`` exists, the ``COPY worker-claude-sdk/dist/`` step fails.

> CI note: sandbox images are not built in ``.github/workflows`` today (those
> are per-service lint/test only); images are built manually via the Makefile
> targets above, matching the AgentScope image workflow.

## How the scheduler picks an image

For the docker runtime, the image is set globally per deployment via
``SCHEDULER_AGENT_IMAGE``.  Per-run template selection lives in the
``cube`` runtime (B-M2): the pool service warms multiple templates and
the scheduler asks for the right one based on the agent config.

## Pinning + upgrade

The image tag controls the AgentScope version inside.  Bumping
AgentScope is a coordinated change:

1. Update ``base.Dockerfile`` ``AGENTSCOPE_VERSION`` arg.
2. Rebuild + retag (e.g. ``0.1`` → ``0.2``).
3. Run the conformance suite against the new image.
4. Bump ``SCHEDULER_AGENT_IMAGE`` default in
   ``services/agent-scheduler/src/agent_scheduler/config.py``.
5. Update ADR 0002 follow-ups if the upgrade required code changes in
   ``runtime_worker``.

## Compliance notes

* Images are built reproducibly (pinned base image digest, pinned pip
  dependencies via ``constraints.txt`` — B-M2 follow-up).
* CI signs the image via cosign (B-M3 follow-up).
* SBOM emitted via syft (top-plan §SBOM).
