#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
installer_input="$repo_root/sandbox-images/matlab-control-system-installer-input.txt"

base_image=${MATLAB_BASE_IMAGE:-127.0.0.1:5000/claude-sdk-cube:0.2-matlab-r4}
output_image=${MATLAB_CONTROL_IMAGE:-127.0.0.1:5000/claude-sdk-cube:0.2-matlab-control-r2}
media_dir=${MATLAB_MEDIA_DIR:-/tmp/matlab-control-media}
container_name="matlab-control-install-$$"

if [[ ! -x "$media_dir/install" ]]; then
  echo "MATLAB installer is missing or not executable: $media_dir/install" >&2
  exit 1
fi

if [[ ! -f "$installer_input" ]]; then
  echo "installer input file not found: $installer_input" >&2
  exit 1
fi

if docker image inspect "$output_image" >/dev/null 2>&1; then
  echo "output image already exists: $output_image" >&2
  exit 1
fi

cleanup() {
  docker rm -f "$container_name" >/dev/null 2>&1 || true
}
trap cleanup EXIT

# MATLAB R2019b's installer exits before its Java child finishes under a
# BuildKit RUN. Use a temporary ordinary container instead, then commit only the
# installed toolbox files. The media bind mount is never copied into the image.
docker create \
  --name "$container_name" \
  --entrypoint "" \
  --mount "type=bind,src=$media_dir,dst=/media,readonly" \
  --mount "type=bind,src=$installer_input,dst=/tmp/installer_input.txt,readonly" \
  "$base_image" \
  /bin/sleep infinity >/dev/null
docker start "$container_name" >/dev/null

docker exec "$container_name" \
  /media/install -inputFile /tmp/installer_input.txt

set +e
docker exec "$container_name" \
  matlab -batch \
  "restoredefaultpath; rehash toolboxcache; status = savepath('/opt/matlab/toolbox/local/pathdef.m'); assert(status == 0, 'failed to persist MATLAB path cache'); v = ver('control'); assert(~isempty(v), 'Control System Toolbox is absent from ver output'); tfPath = which('tf'); disp(['tf path: ' tfPath]); assert(~isempty(tfPath), 'tf is not on the MATLAB path'); sys = tf(1, [1 1]); gain = dcgain(sys); disp(['dcgain: ' num2str(gain)]); assert(abs(gain - 1) < 1e-12, 'Control System Toolbox calculation failed')"
matlab_status=$?
set -e

# R2019b can report 129 after a successful non-interactive `docker exec` even
# though all batch statements completed (it receives SIGHUP while tearing down
# its Java process). Accept that one legacy exit code only after checking the
# installed entry point and persisted path definition directly.
if [[ "$matlab_status" -ne 0 && "$matlab_status" -ne 129 ]]; then
  echo "MATLAB toolbox verification failed with status $matlab_status" >&2
  exit "$matlab_status"
fi
docker exec "$container_name" \
  test -f /opt/matlab/toolbox/control/ctrlmodels/@tf/tf.m
docker exec "$container_name" \
  grep -q toolbox/control /opt/matlab/toolbox/local/pathdef.m
docker exec "$container_name" \
  ln -sf /opt/matlab/bin/matlab /usr/local/bin/matlab

docker exec "$container_name" \
  rm -f /tmp/matlab-control-system-install.log

docker commit \
  --change 'CMD ["/usr/local/bin/start-lightweight-code-interpreter.sh"]' \
  --change 'WORKDIR /workspace' \
  "$container_name" \
  "$output_image" >/dev/null

cleanup
trap - EXIT

# Validate a brand-new process, proving the persisted path cache works without
# calling restoredefaultpath again.
docker run --rm --entrypoint test "$output_image" \
  -L /usr/local/bin/matlab
docker run --rm --entrypoint matlab "$output_image" -batch \
  "v = ver('control'); assert(~isempty(v)); assert(exist('tf', 'file') == 2); sys = tf(1, [1 1]); assert(abs(dcgain(sys) - 1) < 1e-12); disp(['Control System Toolbox ' v.Version])"

docker image inspect "$output_image" \
  --format 'image={{join .RepoTags ","}} size={{.Size}} cmd={{json .Config.Cmd}} entrypoint={{json .Config.Entrypoint}}'
