# syntax=docker/dockerfile:1.7
# CubeSandbox-native Claude worker with scientific Python and native build tools.

FROM node:22-slim@sha256:e21fc383b50d5347dc7a9f1cae45b8f4e2f0d39f7ade28e4eef7d2934522b752 AS worker-build

WORKDIR /app
COPY worker-claude-sdk/package*.json ./
RUN npm ci
COPY worker-claude-sdk/tsconfig*.json ./
COPY worker-claude-sdk/src/ ./src/
RUN npm run build \
 && npm prune --omit=dev \
 && npm cache clean --force

FROM cube-sandbox-cn.tencentcloudcr.com/cube-sandbox/sandbox-code:latest@sha256:5ad589957ae13e596554a723a95716b28cd7137b0353ed6bda1ddcafc89ca76e

ARG PYPI_INDEX_URL=https://mirrors.cloud.tencent.com/pypi/simple

USER root

# Keep the image useful for native Python extensions and CMake-based projects.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      build-essential \
      ca-certificates \
      cmake \
      fontconfig \
      fonts-noto-cjk \
      git \
      graphviz \
      pkg-config \
      unzip \
 && rm -rf /var/lib/apt/lists/*

# Pin the core scientific stack so template rebuilds remain reproducible.
RUN python3 -m pip install --no-cache-dir \
      --index-url "${PYPI_INDEX_URL}" \
      numpy==2.2.6 \
      pandas==2.3.1 \
      matplotlib==3.10.3 \
      scipy==1.15.3 \
      python-docx==1.2.0 \
      openpyxl==3.1.5 \
      python-pptx==1.0.2 \
      pypdf==5.7.0 \
      Pillow==11.3.0 \
      lxml==5.4.0 \
      XlsxWriter==3.2.5 \
      graphviz==0.21

# The Cube base intentionally omits npm. Only the Node runtime is needed by the
# production worker; dependencies and compiled JavaScript come from the stage above.
COPY --from=worker-build /usr/local/bin/node /usr/local/bin/node
RUN ln -sf /usr/local/bin/node /usr/local/bin/nodejs
COPY --from=worker-build /app/package.json /app/package-lock.json /app/
COPY --from=worker-build /app/node_modules/ /app/node_modules/
COPY --from=worker-build /app/dist/ /app/dist/

ENV MPLBACKEND=Agg

RUN python3 -c "import numpy, pandas, matplotlib, scipy, docx, openpyxl, pptx, pypdf, PIL, lxml, xlsxwriter, graphviz" \
 && cmake --version \
 && node --version \
 && test -f /app/dist/main.js

# Inherit Cube's lightweight interpreter command and /health probe. The
# scheduler launches /app/dist/main.js through envd after the VM is ready.
WORKDIR /workspace
