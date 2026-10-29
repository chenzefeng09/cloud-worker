# syntax=docker/dockerfile:1.7
# =============================================================================
# claude-sdk-sandbox — Claude Agent SDK runtime worker image (TypeScript/Node)
#
# The platform's agent sandbox image.  It ships the `worker-claude-sdk/`
# TypeScript worker (Claude Agent SDK) compiled to JS, running under Node.js 22.
# It honours the sandbox runtime contract (contracts/worker-wire-contract.md):
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
# * Non-root `agent` user, uid 1000 / gid 1000 — MUST stay uid 1000, because
#   the scheduler bind-mounts /workspace files
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
# `agent` at uid/gid 1000 (the uid the scheduler expects), so create a dedicated
# group+user, falling back gracefully if 1000 is already taken by `node`.
RUN groupadd --gid 1000 agent 2>/dev/null || groupmod -n agent node \
 && useradd --uid 1000 --gid 1000 --create-home --shell /bin/bash agent 2>/dev/null \
    || usermod -l agent -d /home/agent -m node

# ── Python toolchain for the agent (Office / data-viz via Bash) ───────────────
# The worker itself is Node-only, but the agent needs `python3` (via Bash) for
# Office docs, charts and graphs, so a Python runtime + those libs are
# installed here.  This is purely an agent-callable runtime — the
# worker itself remains the Node ENTRYPOINT below, unchanged.
#
# bookworm's apt python3 is 3.11.  We install all Python libs from PyPI (not the
# pre-fetched cp312 wheels in sandbox-images/wheels/, which are ABI-incompatible
# with 3.11).  graphviz the apt package provides the `dot` system binary that
# the `graphviz` Python binding shells out to.
# NOTE: this build's proxy is flaky on large .deb fetches (intermittent 502 /
# connection timeouts) and re-signs TLS with an untrusted CA, so HTTPS mirrors
# fail cert verification.  Keep the default HTTP deb.debian.org sources (the
# proxy passes plain HTTP) but add generous retries to make the build reliable.
RUN printf 'Acquire::Retries "12";\nAcquire::http::Timeout "120";\nAcquire::https::Timeout "120";\nAcquire::Retries::Delay::Maximum "30";\n' > /etc/apt/apt.conf.d/80-retries \
 && apt-get update \
 && apt-get install -y --no-install-recommends \
      python3 python3-pip python3-venv graphviz \
      fonts-noto-cjk fontconfig \
 && pip3 install --break-system-packages --no-cache-dir \
      python-docx openpyxl python-pptx pypdf Pillow lxml xlsxwriter \
      pandas numpy matplotlib graphviz diagrams \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/*

# Configure matplotlib for headless (no GUI backend) so agent scripts never
# try to open a display.  MPLBACKEND=Agg is honoured process-wide.
ENV MPLBACKEND=Agg

# CJK fonts: fonts-noto-cjk (installed above) provides Chinese/Japanese/Korean
# glyphs.  matplotlib defaults to DejaVu Sans (no CJK), so we append the Noto CJK
# family as a fallback in the system matplotlibrc — this makes Chinese labels in
# agent-generated charts render instead of showing tofu boxes.  ``axes.unicode_minus``
# is disabled because the CJK font lacks the proper minus glyph.  The matplotlibrc
# path is resolved at build time from matplotlib's install location.
RUN MPLDIR="$(python3 -c 'import matplotlib, os; print(os.path.join(os.path.dirname(matplotlib.__file__), "mpl-data"))')" \
 && printf 'font.family: sans-serif\nfont.sans-serif: Noto Sans CJK SC, Noto Sans CJK JP, DejaVu Sans, sans-serif\naxes.unicode_minus: False\n' >> "$MPLDIR/matplotlibrc" \
 && fc-cache -f >/dev/null 2>&1 || true

# Sanity check: fail the build immediately if any library is missing, AND verify
# the CJK font is actually discoverable by matplotlib's font manager (so a broken
# font install fails the build rather than silently producing tofu charts).
RUN python3 -c "import matplotlib; matplotlib.use('Agg'); import docx, openpyxl, pptx, pypdf, PIL, lxml, xlsxwriter, pandas, numpy, graphviz, diagrams; from diagrams import Diagram; from matplotlib import font_manager; names={f.name for f in font_manager.fontManager.ttflist}; assert any('CJK' in n for n in names), 'Noto CJK font not found by matplotlib: '+str(sorted(n for n in names if 'Noto' in n)); print('py libs + CJK font OK')"

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
