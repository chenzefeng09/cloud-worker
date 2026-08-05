# syntax=docker/dockerfile:1.7
# =============================================================================
# claude-sdk-cube-matlab — Cube sandbox + scientific Python + MATLAB 2019b
#                          + Control System Toolbox
#
# Reproduces the image the dev deployment runs as
# ``127.0.0.1:5000/claude-sdk-cube:0.2-matlab-r4``.
#
# Requires next to this file (not in git — multi-GB / licensed material):
#   * the extracted MATLAB 2019b installer, passed as the ``matlab-iso``
#     build context (hence buildx, which is what supports --build-context)
#   * license.lic and libmwlmgrimpl.so
#
# Build:
#   docker buildx build --platform linux/amd64 \
#     --build-context matlab-iso=/tmp/matlab-iso \
#     -f sandbox-images/claude-sdk-cube-matlab.Dockerfile \
#     -t 127.0.0.1:5000/claude-sdk-cube:0.2-matlab-r4 \
#     .
#
# Then push to the registry Cube pulls from and create the template:
#   docker push 127.0.0.1:5000/claude-sdk-cube:0.2-matlab-r4
#   cubemastercli tpl create-from-image \
#     --image 127.0.0.1:5000/claude-sdk-cube:0.2-matlab-r4 \
#     --alias claude-sdk-matlab --node <node-ip> \
#     --cpu 2000 --memory 4096 --writable-layer-size 8Gi \
#     --probe 49999 --probe-path /health
# =============================================================================

ARG BASE_IMAGE=claude-sdk-cube:0.2-scientific
FROM ${BASE_IMAGE}

USER root

COPY sandbox-images/matlab-2019b-installer-input.txt /tmp/installer_input.txt

COPY --from=matlab-iso . /tmp/matlab-iso/

RUN echo "=== Starting MATLAB 2019b silent install ===" \
 && /tmp/matlab-iso/install -inputFile /tmp/installer_input.txt \
 && echo "=== MATLAB install complete ===" \
 && rm -f /tmp/installer_input.txt \
 && rm -rf /tmp/matlab-iso

RUN mkdir -p /opt/matlab/licenses
COPY sandbox-images/license.lic /opt/matlab/licenses/license.lic
COPY sandbox-images/libmwlmgrimpl.so /opt/matlab/bin/glnxa64/matlab_startup_plugins/lmgrimpl/libmwlmgrimpl.so

ENV MATLAB_HOME=/opt/matlab \
    PATH=/opt/matlab/bin:${PATH}

# E2B/envd command sessions use their own conservative PATH instead of the
# image-level PATH. Keep `matlab` directly executable in those sessions too.
RUN ln -s /opt/matlab/bin/matlab /usr/local/bin/matlab

# CJK-capable default font for figures. MATLAB's stock Helvetica has no CJK
# glyphs, so Chinese titles/labels/legends export as tofu boxes; Noto Sans CJK
# SC comes from the base image's fonts-noto-cjk. This has to go in matlabrc.m,
# NOT the usual toolbox/local/startup.m: files dropped into the toolbox tree
# after installation are invisible to MATLAB's path cache, so such a startup.m
# is silently never run (verified — `which('startup')` came back empty).
RUN cat >> /opt/matlab/toolbox/local/matlabrc.m <<'MATLABRC'

try
    set(groot, 'defaultAxesFontName', 'Noto Sans CJK SC');
    set(groot, 'defaultTextFontName', 'Noto Sans CJK SC');
    set(groot, 'defaultLegendFontName', 'Noto Sans CJK SC');
    set(groot, 'defaultColorbarFontName', 'Noto Sans CJK SC');
catch
    % Never let a font default abort a batch run.
end
MATLABRC

RUN matlab -batch "disp(['MATLAB ' version]); assert(sum(sum(magic(4))) == 136); assert(exist('tf', 'file') == 2); sys = tf(1, [1 1]); assert(abs(dcgain(sys) - 1) < 1e-12)" \
 && echo "=== All checks passed ==="

WORKDIR /workspace

# Restate the Cube base's startup command explicitly. Cube boots the template
# VM with this command and then probes http://<vm>:49999/health; anything that
# drops it (notably building this layer by hand with ``docker commit``, which
# records the running container's command instead) leaves the VM idle, and
# ``tpl create-from-image`` fails at CREATING_TEMPLATE with a refused probe.
CMD ["/usr/local/bin/start-lightweight-code-interpreter.sh"]
