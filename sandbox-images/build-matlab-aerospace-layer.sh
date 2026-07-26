#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
installer_input="$repo_root/sandbox-images/matlab-aerospace-core-installer-input.txt"

base_image=${MATLAB_BASE_IMAGE:-127.0.0.1:5000/claude-sdk-cube:0.2-matlab-control-r2}
output_image=${MATLAB_AEROSPACE_IMAGE:-127.0.0.1:5000/claude-sdk-cube:0.2-matlab-aerospace-r1}
media_dir=${MATLAB_MEDIA_DIR:-/tmp/matlab-aerospace-media}
container_name="matlab-aerospace-install-$$"

matlab_verify="restoredefaultpath; rehash toolboxcache; status = savepath('/opt/matlab/toolbox/local/pathdef.m'); assert(status == 0, 'failed to persist MATLAB path cache'); productFolders = {'simulink','aero','aeroblks','stateflow','slcontrol'}; for k = 1:numel(productFolders), v = ver(productFolders{k}); assert(~isempty(v), ['missing product: ' productFolders{k}]); disp([v.Name ' ' v.Version]); end; disp('AEROSPACE_CORE_PRODUCTS_OK'); [temperature, speedOfSound, pressure, density] = atmosisa(10000); assert(all(isfinite([temperature speedOfSound pressure density]))); disp('AEROSPACE_ATMOSPHERE_OK'); load_system('simulink'); assert(~isempty(which('aerolib'))); disp('AEROSPACE_LIBRARIES_OK'); model = 'aerospace_core_smoke'; new_system(model); add_block('simulink/Sources/Constant',[model '/Constant']); add_block('simulink/Sinks/Out1',[model '/Out']); add_line(model,'Constant/1','Out/1'); sim(model,'StopTime','0.1'); disp('AEROSPACE_SIMULATION_OK'); op = operspec(model); assert(~isempty(op)); disp('AEROSPACE_CONTROL_DESIGN_OK'); root = sfroot; assert(~isempty(root)); disp('AEROSPACE_STATEFLOW_OK'); close_system(model,0); fid = fopen('/tmp/aerospace-core-smoke.ok','w'); assert(fid ~= -1); fprintf(fid,'ok'); fclose(fid); disp('AEROSPACE_CORE_SMOKE_OK')"

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

docker create \
  --name "$container_name" \
  --entrypoint "" \
  --mount "type=bind,src=$media_dir,dst=/media,readonly" \
  --mount "type=bind,src=$installer_input,dst=/tmp/installer_input.txt,readonly" \
  "$base_image" \
  /bin/sleep infinity >/dev/null
docker start "$container_name" >/dev/null

docker exec "$container_name" sh -lc \
  "apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends libasound2 libatk1.0-0 libcups2 libdbus-1-3 libgdk-pixbuf-2.0-0 libnspr4 libnss3 libx11-xcb1 libxcomposite1 libxcursor1 libxdamage1 libxfixes3 libxi6 libxinerama1 libxrandr2 libxss1 libxtst6 && rm -rf /var/lib/apt/lists/*"

docker exec "$container_name" \
  /media/install -inputFile /tmp/installer_input.txt

set +e
docker exec "$container_name" matlab -batch "$matlab_verify"
matlab_status=$?
set -e

if [[ "$matlab_status" -ne 0 && "$matlab_status" -ne 129 ]]; then
  echo "MATLAB aerospace verification failed with status $matlab_status" >&2
  exit "$matlab_status"
fi

docker exec "$container_name" test -d /opt/matlab/toolbox/simulink
docker exec "$container_name" test -d /opt/matlab/toolbox/stateflow
docker exec "$container_name" test -d /opt/matlab/toolbox/aero
docker exec "$container_name" test -d /opt/matlab/toolbox/aeroblks
docker exec "$container_name" test -d /opt/matlab/toolbox/slcontrol
docker exec "$container_name" grep -q simulink /opt/matlab/toolbox/local/pathdef.m
docker exec "$container_name" grep -q aeroblks /opt/matlab/toolbox/local/pathdef.m
docker exec "$container_name" grep -q slcontrol /opt/matlab/toolbox/local/pathdef.m
docker exec "$container_name" test -f /tmp/aerospace-core-smoke.ok
docker exec "$container_name" ln -sf /opt/matlab/bin/matlab /usr/local/bin/matlab
docker exec "$container_name" rm -f /tmp/matlab-aerospace-core-install.log /tmp/aerospace-core-smoke.ok

docker commit \
  --change 'CMD ["/usr/local/bin/start-lightweight-code-interpreter.sh"]' \
  --change 'WORKDIR /workspace' \
  "$container_name" \
  "$output_image" >/dev/null

cleanup
trap - EXIT

docker run --rm --entrypoint test "$output_image" -L /usr/local/bin/matlab
docker run --rm --entrypoint matlab "$output_image" -batch "$matlab_verify"

docker image inspect "$output_image" \
  --format 'image={{join .RepoTags ","}} size={{.Size}} cmd={{json .Config.Cmd}} entrypoint={{json .Config.Entrypoint}}'
