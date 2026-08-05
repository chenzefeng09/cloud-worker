# syntax=docker/dockerfile:1.7
# Refresh only the Claude worker in an existing Cube image.
#
# This keeps heavyweight/private layers such as MATLAB and its toolboxes
# untouched while replacing /app/dist and /app/node_modules with a worker built
# from the current source tree.

ARG BASE_IMAGE=127.0.0.1:5000/claude-sdk-cube:0.2-matlab-aerospace-r1

FROM node:22-slim@sha256:e21fc383b50d5347dc7a9f1cae45b8f4e2f0d39f7ade28e4eef7d2934522b752 AS worker-build

WORKDIR /app
COPY worker-claude-sdk/package*.json ./
RUN npm ci
COPY worker-claude-sdk/tsconfig*.json ./
COPY worker-claude-sdk/src/ ./src/
RUN npm run build \
 && npm prune --omit=dev \
 && npm cache clean --force

FROM ${BASE_IMAGE}

USER root
RUN rm -rf /app/dist /app/node_modules
COPY --from=worker-build /app/package.json /app/package-lock.json /app/
COPY --from=worker-build /app/node_modules/ /app/node_modules/
COPY --from=worker-build /app/dist/ /app/dist/

RUN node --version \
 && test -f /app/dist/main.js

WORKDIR /workspace

