# syntax=docker/dockerfile:1.7
# =============================================================================
# claude-sdk-sandbox — Claude Agent SDK runtime worker image (TypeScript/Node)
#
# Parallel sibling to base.Dockerfile (the AgentScope/Python worker).  This
# image ships the `worker-claude-sdk/` TypeScript worker compiled to JS,
# running under Node.js 22.  It honours the same sandbox runtime contract as
# the AgentScope worker:
#   * reads the task from /workspace/task.json OR the AGENT_TASK_JSON env var
#   * writes /workspace/result.json + /workspace/events.jsonl
#   * /workspace is the scheduler bind-mount target
#   * skills are host-mounted under /home/skills/<name>
#
# Build context: the agent-platform REPO ROOT (not sandbox-images/), because
# we COPY from worker-claude-sdk/.  See the invocation below.
#
# Build PREREQUISITE: worker-claude-sdk/dist/ must already exist.  It is
# produced by `npm run build` (tsc) inside worker-claude-sdk/.  This Dockerfile
# does NOT compile TypeScript — it only copies the prebuilt dist/ plus a
# production-only node_modules.  The full ordered build is:
#
#   cd worker-claude-sdk && npm ci && npm run build && cd ..
#   docker build \
#     -f sandbox-images/claude-sdk.Dockerfile \
#     -t ghcr.io/agent-platform/claude-sdk-sandbox:0.1 \
#     .
#
# Or simply: `make build-claude-sdk-image` from the repo root.
#
# Notes
# -----
# * Non-root `agent` user, uid 1000 / gid 1000 — MUST match the AgentScope
#   image's `agent` user, because the scheduler bind-mounts /workspace files
#   owned by uid 1000.  A mismatched uid makes result.json/events.jsonl writes
#   fail with EACCES.
# * --omit=dev keeps the @anthropic-ai/claude-agent-sdk + aws-sdk + ioredis
#   runtime tree only; tsc/vitest/tsx (devDependencies) are excluded.
# * The Claude Agent SDK bundles its own `cli.js` binary under node_modules;
#   it ships in the production tree, so no extra install step is needed.
# =============================================================================

FROM node:22-slim

# ── Non-root agent user (uid/gid 1000) ───────────────────────────────────────
# node:22-slim already creates a `node` user at uid 1000.  We want the username
# `agent` at uid/gid 1000 to mirror the AgentScope image, so create a dedicated
# group+user, falling back gracefully if 1000 is already taken by `node`.
RUN groupadd --gid 1000 agent 2>/dev/null || groupmod -n agent node \
 && useradd --uid 1000 --gid 1000 --create-home --shell /bin/bash agent 2>/dev/null \
    || usermod -l agent -d /home/agent -m node

WORKDIR /app

# Install production deps first (better layer caching: deps change rarely).
# Requires package.json + package-lock.json in the build context.
COPY worker-claude-sdk/package*.json ./
RUN npm ci --omit=dev \
 && npm cache clean --force

# Then the compiled worker.  dist/ is produced by `npm run build` beforehand.
COPY worker-claude-sdk/dist/ ./dist/

# ── Runtime directories ──────────────────────────────────────────────────────
# /workspace  — scheduler bind-mount target (task.json in, result.json out)
# /home/skills — skills host-mount root (/home/skills/<name>)
RUN mkdir -p /workspace /home/skills \
 && chown -R agent:agent /app /workspace /home/skills

USER agent
WORKDIR /workspace

# The scheduler writes /workspace/task.json (docker runtime) or passes
# AGENT_TASK_JSON (cube runtime) before starting the container; main.js reads
# whichever is present and drives the Claude Agent SDK loop.
ENTRYPOINT ["node", "/app/dist/main.js"]
