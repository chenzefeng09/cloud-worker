#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "usage: $0 /path/to/MatlabR2019b_lin64.iso /path/to/output-dir" >&2
  exit 2
fi

iso_path=$1
output_dir=$2
repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
resolver="$repo_root/sandbox-images/resolve-matlab-product-archives.py"

products=(
  Simulink
  Aerospace_Toolbox
  Aerospace_Blockset
  Stateflow
  Simulink_Control_Design
)

if [[ ! -f "$iso_path" ]]; then
  echo "MATLAB ISO not found: $iso_path" >&2
  exit 1
fi

if ! command -v bsdtar >/dev/null 2>&1; then
  echo "bsdtar is required to extract the ISO" >&2
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required to resolve MATLAB product archives" >&2
  exit 1
fi

if [[ ! -f "$resolver" ]]; then
  echo "MATLAB archive resolver not found: $resolver" >&2
  exit 1
fi

if [[ -e "$output_dir" ]] && [[ -n "$(find "$output_dir" -mindepth 1 -print -quit 2>/dev/null)" ]]; then
  echo "output directory must be empty: $output_dir" >&2
  exit 1
fi

mkdir -p "$output_dir"

# Extract only the installer runtime and metadata first. The resolver reads the
# productdata XML files in the two platform archives and emits the exact payload
# paths for the selected R2019b products.
bsdtar -xf "$iso_path" -C "$output_dir" \
  sys \
  java \
  etc \
  bin \
  ui \
  help/install \
  activate.ini \
  install \
  installer_input.txt \
  license_agreement.txt \
  patents.txt \
  readme.txt \
  trademarks.txt \
  version.txt \
  VersionInfo.xml \
  .dvd1 \
  archives/platform_common.zip \
  archives/platform_glnxa64.zip

bsdtar -xf "$iso_path" -C "$output_dir" -T <(
  python3 "$resolver" \
    "$output_dir/archives/platform_common.zip" \
    "$output_dir/archives/platform_glnxa64.zip" \
    "${products[@]}"
)

test -x "$output_dir/install"
test -f "$output_dir/archives/common/aero_common_1564985553.enc"
test -f "$output_dir/archives/common/aeroblks_common_1564002157.enc"
test -f "$output_dir/archives/common/simulink_common_1564143811.enc"
test -f "$output_dir/archives/common/stateflow_core_common_1566440120.enc"
test -f "$output_dir/archives/common/slcontrol_common_1566441651.enc"

payload_count=$(find "$output_dir/archives" -type f -name '*.enc' | wc -l | tr -d ' ')
du -sh "$output_dir"
echo "Prepared $payload_count payloads for MATLAB R2019b aerospace core products at $output_dir"
