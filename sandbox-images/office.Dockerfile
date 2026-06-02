# syntax=docker/dockerfile:1.7
#
# agentscope-sandbox-office — base + LibreOffice headless.
#
# Tenants on the "office" template use this for document-generation
# agents: read/write .docx / .xlsx / .pptx via LibreOffice's CLI
# converters, then return them to the user via /workspace/result.json.
#
# Layered on the base image — the only delta is the apt install + the
# default-template config (no LibreOffice GUI).
#
# Build context = agent-platform repo root.

ARG BASE_TAG=0.1
FROM agentscope-sandbox-base:${BASE_TAG}

USER root

# LibreOffice headless ships a lot — only install what we actually need
# for headless document IO.  ``unoconv`` is the conversion CLI most
# agents reach for; ``fonts-noto-cjk`` makes Chinese text render
# correctly in produced PDFs.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      libreoffice-core libreoffice-writer libreoffice-calc libreoffice-impress \
      libreoffice-script-provider-python \
      unoconv \
      fonts-noto-cjk \
 && rm -rf /var/lib/apt/lists/*

USER agent

# Sanity check — fail the build if LibreOffice's Python bridge is broken
# (we've been bitten by debian packaging glitches here).  ``--headless``
# avoids requiring an X display.
RUN soffice --headless --version

# Entrypoint inherited from base (runtime_worker).
