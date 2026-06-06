#!/usr/bin/env bash
# deploy/loadtest/monitor_resources.sh
#
# Lightweight resource monitor for 24 h load test runs.
# Polls docker stats every INTERVAL seconds and appends to a CSV log.
# Designed for environments without Prometheus/Grafana.
# If Prometheus IS available, see the runbook for the recommended dashboard.
#
# Usage:
#   chmod +x deploy/loadtest/monitor_resources.sh
#   ./deploy/loadtest/monitor_resources.sh &
#   # ... run the load test ...
#   kill %1   # stop monitor when test ends
#
# Output: deploy/loadtest/results/resource_YYYYMMDD_HHMMSS.csv
#
# OOM / leak detection:
#   grep for rows where MEM_USAGE_MB grows monotonically over 30+ minutes
#   or crosses a threshold (see WARN_MEM_MB below).

set -euo pipefail

INTERVAL="${MONITOR_INTERVAL:-30}"    # seconds between samples
WARN_MEM_MB="${WARN_MEM_MB:-2048}"   # warn if any container exceeds this

RESULT_DIR="$(dirname "$0")/results"
mkdir -p "$RESULT_DIR"
OUTFILE="$RESULT_DIR/resource_$(date +%Y%m%d_%H%M%S).csv"

# Containers to watch — match docker-compose service container_name values.
# Override via MONITOR_CONTAINERS env var (space-separated list).
CONTAINERS="${MONITOR_CONTAINERS:-agent-bff agent-session-orchestrator agent-scheduler agent-sandbox-pool}"

echo "Monitoring containers: $CONTAINERS"
echo "Output: $OUTFILE"
echo "Interval: ${INTERVAL}s"

# CSV header
echo "timestamp,container,cpu_pct,mem_usage_mb,mem_limit_mb,mem_pct,net_in_mb,net_out_mb,block_in_mb,block_out_mb" \
  > "$OUTFILE"

while true; do
  TS=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
  for CNAME in $CONTAINERS; do
    # docker stats --no-stream returns one row per container
    ROW=$(docker stats --no-stream --format \
      "{{.CPUPerc}},{{.MemUsage}},{{.MemPerc}},{{.NetIO}},{{.BlockIO}}" \
      "$CNAME" 2>/dev/null || echo "N/A,N/A / N/A,N/A,N/A / N/A,N/A / N/A")

    # Parse "123.4MiB / 2GiB" style fields into plain MB numbers
    parse_mb() {
      local raw="$1"
      # Strip non-numeric suffix, convert GiB/GB → MiB
      python3 -c "
import re, sys
s = '$raw'.strip()
m = re.match(r'([0-9.]+)\s*(GiB|GB|MiB|MB|KiB|kB|B)', s)
if not m:
    print('0')
    sys.exit()
v, u = float(m.group(1)), m.group(2)
if u in ('GiB','GB'): v *= 1024
elif u in ('KiB','kB'): v /= 1024
elif u == 'B': v /= (1024*1024)
print(f'{v:.1f}')
" 2>/dev/null || echo "0"
    }

    CPU=$(echo "$ROW" | cut -d',' -f1 | tr -d '%')
    MEM_PAIR=$(echo "$ROW" | cut -d',' -f2)
    MEM_USED_RAW=$(echo "$MEM_PAIR" | awk -F' / ' '{print $1}')
    MEM_LIMIT_RAW=$(echo "$MEM_PAIR" | awk -F' / ' '{print $2}')
    MEM_PCT=$(echo "$ROW" | cut -d',' -f3 | tr -d '%')
    NET_PAIR=$(echo "$ROW" | cut -d',' -f4)
    NET_IN_RAW=$(echo "$NET_PAIR" | awk -F' / ' '{print $1}')
    NET_OUT_RAW=$(echo "$NET_PAIR" | awk -F' / ' '{print $2}')
    BLK_PAIR=$(echo "$ROW" | cut -d',' -f5)
    BLK_IN_RAW=$(echo "$BLK_PAIR" | awk -F' / ' '{print $1}')
    BLK_OUT_RAW=$(echo "$BLK_PAIR" | awk -F' / ' '{print $2}')

    MEM_MB=$(parse_mb "$MEM_USED_RAW")
    MEM_LMT=$(parse_mb "$MEM_LIMIT_RAW")
    NET_IN=$(parse_mb "$NET_IN_RAW")
    NET_OUT=$(parse_mb "$NET_OUT_RAW")
    BLK_IN=$(parse_mb "$BLK_IN_RAW")
    BLK_OUT=$(parse_mb "$BLK_OUT_RAW")

    echo "$TS,$CNAME,$CPU,$MEM_MB,$MEM_LMT,$MEM_PCT,$NET_IN,$NET_OUT,$BLK_IN,$BLK_OUT" \
      >> "$OUTFILE"

    # Warn if memory exceeds threshold
    if python3 -c "exit(0 if float('${MEM_MB}') > ${WARN_MEM_MB} else 1)" 2>/dev/null; then
      echo "WARN [$TS] $CNAME memory ${MEM_MB} MB exceeds threshold ${WARN_MEM_MB} MB" >&2
    fi
  done

  sleep "$INTERVAL"
done
