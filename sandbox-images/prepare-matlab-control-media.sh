#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "usage: $0 /path/to/MatlabR2019b_lin64.iso /path/to/output-dir" >&2
  exit 2
fi

iso_path=$1
output_dir=$2

if [[ ! -f "$iso_path" ]]; then
  echo "MATLAB ISO not found: $iso_path" >&2
  exit 1
fi

if ! command -v bsdtar >/dev/null 2>&1; then
  echo "bsdtar is required to extract the ISO" >&2
  exit 1
fi

if [[ -e "$output_dir" ]] && [[ -n "$(find "$output_dir" -mindepth 1 -print -quit 2>/dev/null)" ]]; then
  echo "output directory must be empty: $output_dir" >&2
  exit 1
fi

mkdir -p "$output_dir"

# Installer runtime and product database. Product payloads are selected below,
# avoiding a copy of the complete 17 GB ISO.
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
  archives/platform_glnxa64.zip \
  'archives/common/*control*.enc' \
  'archives/common/shared_tracking*.enc' \
  'archives/common/examples/control_common_*.enc' \
  'archives/common/examples/controls_id_common_*.enc' \
  'archives/common/examples/ident_common_*.enc' \
  'archives/common/examples/slcontrol_common_*.enc' \
  'archives/glnxa64/*control*.enc' \
  'archives/glnxa64/shared_tracking*.enc' \
  'archives/glnxa64/examples/control_glnxa64_*.enc' \
  'archives/glnxa64/examples/controls_id_glnxa64_*.enc' \
  'archives/glnxa64/examples/ident_glnxa64_*.enc' \
  'archives/glnxa64/examples/slcontrol_glnxa64_*.enc' \
  'archives/glnxa64/3p/slicot_glnxa64_*.enc'

test -x "$output_dir/install"
test -f "$output_dir/archives/platform_common.zip"
test -f "$output_dir/archives/common/control_common_1566300781.enc"
test -f "$output_dir/archives/glnxa64/3p/slicot_glnxa64_1563491839.enc"

du -sh "$output_dir"
echo "Prepared minimal Control System Toolbox installer media at $output_dir"
