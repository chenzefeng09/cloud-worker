# syntax=docker/dockerfile:1.7
#
# agentscope-sandbox-matlab — base + MATLAB Runtime + matlabengine.
#
# Tenants with MATLAB workloads run scripts through ``matlabengine``
# (Python binding) inside the sandbox.  This image bundles the MATLAB
# Runtime (free redistributable) but **needs a license server at runtime**
# — the Runtime is restricted to running already-compiled apps from a
# licensed MATLAB installation.
#
# Build context = agent-platform repo root.
#
# The Runtime installer is NOT checked into git (multi-GB binary + EULA
# distribution restrictions); CI fetches it from internal artefact
# storage before the build runs.  Skip this image entirely if your
# customer doesn't need MATLAB.

ARG BASE_TAG=0.1
FROM agentscope-sandbox-base:${BASE_TAG}

USER root

# Build-time arg: where the MATLAB Runtime installer was downloaded to
# (set by CI / build script).  R2024a (release 9.14) is the floor; older
# Runtimes lack the ``matlabengine`` wheel built against Python 3.12.
ARG MATLAB_RUNTIME_INSTALLER=/tmp/MATLAB_Runtime_R2024a.zip
ARG MATLAB_RUNTIME_VERSION=R2024a

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      unzip xorg \
 && rm -rf /var/lib/apt/lists/*

# The Runtime installer is a graphical app with a silent-install mode;
# we feed it a minimal answers file so it lands at /opt/matlab-runtime.
COPY sandbox-images/matlab-installer-input.txt /tmp/installer_input.txt
RUN --mount=type=bind,source=sandbox-images,target=/build-ctx \
    cp /build-ctx/matlab-installer-input.txt /tmp/installer_input.txt && \
    test -f "${MATLAB_RUNTIME_INSTALLER}" \
      || (echo "MATLAB Runtime installer missing at ${MATLAB_RUNTIME_INSTALLER}" \
          && echo "CI fetches this from internal artefact storage before docker build." \
          && exit 1) && \
    unzip -q "${MATLAB_RUNTIME_INSTALLER}" -d /tmp/matlab-runtime && \
    /tmp/matlab-runtime/install -inputFile /tmp/installer_input.txt && \
    rm -rf /tmp/matlab-runtime "${MATLAB_RUNTIME_INSTALLER}"

# ``matlabengine`` Python binding — version MUST match the Runtime release.
RUN pip install --no-cache-dir matlabengine

ENV MATLAB_RUNTIME_VERSION=${MATLAB_RUNTIME_VERSION} \
    # Runtime expects to find shared libs via LD_LIBRARY_PATH (per MATLAB docs).
    LD_LIBRARY_PATH=/opt/matlab-runtime/${MATLAB_RUNTIME_VERSION}/runtime/glnxa64:/opt/matlab-runtime/${MATLAB_RUNTIME_VERSION}/bin/glnxa64:/opt/matlab-runtime/${MATLAB_RUNTIME_VERSION}/sys/os/glnxa64

# License server URL is injected at sandbox spawn time via env
# (``MLM_LICENSE_FILE``).  The image MUST NOT bake any license info —
# that's site-specific and changes per customer deployment.

USER agent

# Entrypoint inherited from base (runtime_worker).
