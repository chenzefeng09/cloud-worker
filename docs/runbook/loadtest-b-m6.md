# Runbook: Agent-Platform B-M6 Load Test

**SLO target:** 100 concurrent sessions, 24 h sustained, P95 end-to-end < 8 s, error rate < 1 %

---

## 1. End-to-end API flow (what the script simulates)

```
Browser / loadtest client
        │
        ▼
POST /api/bff/v1/auth/login                    ← credential exchange → HttpOnly cookie
        │
        ▼
POST /api/bff/v1/sessions                      ← create session (returns session_id)
        │
        ▼
POST /api/bff/v1/sessions/{id}/chat            ← send user message
  BFF internally:
    POST  orch/sessions/{id}/messages           ← persist user message
    POST  orch/sessions/{id}/runs               ← start agent run
    GET   orch/sessions/{id}/messages/stream    ← relay SSE back to client
        │
        ▼  (SSE stream, text/event-stream)
event: message_created   ← user message echo
event: run_started       ← run metadata
event: token             ← LLM token deltas   ← TTFT measured here (1st token)
event: tool              ← tool call / result
event: thinking          ← extended thinking blocks
event: message           ← full DB-persisted message rows
event: done              ← run finished       ← run_total measured here
```

**Two key latency definitions:**

| Metric | Start | End |
|--------|-------|-----|
| TTFT (time-to-first-token) | `POST /chat` sent | First `token` SSE event arrives |
| Run total | `POST /chat` sent | `done` SSE event arrives |

---

## 2. Prerequisites

### 2.1 Staging environment

- Staging stack deployed (BFF, session-orchestrator, agent-scheduler, sandbox-pool, Redis, PostgreSQL, S3/MinIO).
- Foundation identity service reachable by the BFF.
- Agent sandbox image (`AGENT_IMAGE`) pushed to the container registry staging can pull.
- Network/firewall: loadtest machine can reach `https://staging.your-domain.example` on 443.

### 2.2 Test accounts

For 100-concurrent users you have two options:

**Option A — single shared account (simplest)**
The server issues per-session cookies so a single account can host many simultaneous sessions.
Works until the identity service rate-limits concurrent logins.
```
LOAD_EMAIL=loadtest@example.com
LOAD_PASSWORD=<password>
```

**Option B — account pool (recommended for 100+ concurrent)**
Pre-create N accounts in Keycloak (10–20 is usually sufficient; Locust round-robins them).
```
export LOAD_ACCOUNTS='[
  ["lt01@example.com","pw01"],
  ["lt02@example.com","pw02"],
  ...
]'
```

### 2.3 Lightweight / deterministic prompt

The default prompt is:
```
Reply with exactly one sentence: 'Load test OK.' Do not add anything else.
```
This keeps agent run duration predictable and avoids burning quota on long LLM completions.

Override via:
```bash
export LOAD_PROMPT="Your custom short prompt here"
```

**Mock LLM option (zero cost, fully deterministic)**
If staging has a mock LLM endpoint (e.g. vLLM returning a fixed response), point the
`SESSION_ORCHESTRATOR_LLM_*` env vars at it and use any prompt.  The script works
identically — it only sees the SSE stream from the BFF.

### 2.4 Install load test dependencies

```bash
cd /path/to/agent-platform
pip install -r deploy/loadtest/requirements.txt
```

---

## 3. Running the load test

### 3.1 Smoke test (1 user / 30 s — verify connectivity)

```bash
locust -f deploy/loadtest/locustfile.py \
  --headless -u 1 -r 1 -t 30s \
  --host http://localhost:8005 \
  --html deploy/loadtest/results/smoke_$(date +%Y%m%d_%H%M%S).html
```

Expected: green console, SLO report printed, no failures.

### 3.2 Ramp-up validation (10 users / 5 min)

```bash
export LOAD_EMAIL=loadtest@staging.example.com
export LOAD_PASSWORD=<password>

locust -f deploy/loadtest/locustfile.py \
  --headless -u 10 -r 2 -t 300s \
  --host https://staging.your-domain.example \
  --html deploy/loadtest/results/rampup_$(date +%Y%m%d_%H%M%S).html \
  --csv deploy/loadtest/results/rampup_$(date +%Y%m%d_%H%M%S)
```

### 3.3 Full SLO run (100 users / 24 h)

```bash
export LOAD_EMAIL=loadtest@staging.example.com
export LOAD_PASSWORD=<password>
# Or: export LOAD_ACCOUNTS='[...]'

# Start resource monitor in background
./deploy/loadtest/monitor_resources.sh \
  MONITOR_CONTAINERS="agent-bff agent-session-orchestrator agent-scheduler" &
MONITOR_PID=$!

# Run load test
locust -f deploy/loadtest/locustfile.py \
  --headless \
  -u 100 \
  -r 5 \
  -t 86400s \
  --host https://staging.your-domain.example \
  --html deploy/loadtest/results/slo_$(date +%Y%m%d_%H%M%S).html \
  --csv deploy/loadtest/results/slo_$(date +%Y%m%d_%H%M%S)

# Stop monitor
kill $MONITOR_PID
```

**Parameter reference:**

| Flag | Value | Meaning |
|------|-------|---------|
| `-u 100` | 100 | Peak concurrent virtual users |
| `-r 5` | 5 | Spawn rate (users/second) — reaches 100 in 20 s |
| `-t 86400s` | 24 h | Test duration |
| `--csv` | path prefix | Writes `_stats.csv`, `_failures.csv`, `_exceptions.csv` |

### 3.4 Distributed run (optional, for load generator scaling)

If one machine cannot sustain 100 RPS, use Locust distributed mode:

```bash
# Master (collects results)
locust -f deploy/loadtest/locustfile.py --master \
  --host https://staging.your-domain.example

# Workers (each on a separate machine, same locustfile)
locust -f deploy/loadtest/locustfile.py --worker \
  --master-host <master-ip>
```

---

## 4. SLO pass/fail criteria

| Metric | Threshold | How to check |
|--------|-----------|--------------|
| P95 TTFT | < 3 000 ms | SLO report printed at end; `SSE chat/ttft` row in Locust stats |
| P95 run total | < 8 000 ms | SLO report printed at end; `SSE chat/run_total` row in Locust stats |
| Error rate | < 1 % | Locust failure table; `_failures.csv` |
| OOM / memory leak | no monotone growth | Resource CSV; see Section 5 |

The script exits with code 1 if any SLO threshold is breached.

Example passing SLO report:
```
============================================================
  B-M6 SLO REPORT
============================================================
  Samples collected : TTFT=14320, run_total=14318
  P95 TTFT          :   1842.3 ms  (threshold 3000 ms)  PASS
  P95 run total     :   5971.0 ms  (threshold 8000 ms)  PASS
  Error rate        :      0.12 %   (threshold 1.0 %)        PASS
============================================================
  Overall SLO       : PASS
============================================================
```

---

## 5. Observability during the run

### 5.1 Locust web UI (optional, for live monitoring)

Omit `--headless` and open `http://localhost:8089` during the run.
The `SSE chat/ttft` and `SSE chat/run_total` virtual request rows appear in the table
alongside native HTTP stats.

### 5.2 Resource monitor CSV

```bash
# Check for containers exceeding 2 GB
grep -v "^timestamp" deploy/loadtest/results/resource_*.csv \
  | awk -F',' '$4 > 2048 {print}'

# Plot memory over time (requires gnuplot or Python)
python3 - <<'EOF'
import csv, collections, sys
from pathlib import Path

csvfile = sorted(Path("deploy/loadtest/results").glob("resource_*.csv"))[-1]
rows = list(csv.DictReader(csvfile.open()))
by_container = collections.defaultdict(list)
for r in rows:
    by_container[r["container"]].append(float(r["mem_usage_mb"] or 0))

for name, samples in by_container.items():
    trend = samples[-1] - samples[0] if len(samples) > 1 else 0
    print(f"{name:40s}  start={samples[0]:.0f}MB  end={samples[-1]:.0f}MB  drift={trend:+.0f}MB")
EOF
```

A monotonically growing `drift` value across 24 h indicates a memory leak.
Acceptable drift: < 200 MB over 24 h per container.

### 5.3 If Prometheus / Grafana is available

The staging stack exposes OpenTelemetry (see `SESSION_ORCHESTRATOR_OTEL_ENABLED: "true"` in
`deploy/docker-compose/agent-platform.yml`).  In Grafana:

- **Memory panels:** `process_runtime_upython_memory_bytes` per service
- **Request latency:** `http_server_duration_milliseconds_bucket{service="session-orchestrator"}`
- **Error rate:** `http_server_duration_milliseconds_count{http_response_status_code=~"5.."}`

Look for:
- Memory curves with positive slope over hours (leak signal)
- P95 latency crossing 8 s during peak load
- Redis stream lag growing (backpressure signal)

### 5.4 kubectl (Kubernetes staging)

```bash
# Real-time resource usage
kubectl top pods -n agent-platform --sort-by=memory

# OOM events
kubectl get events -n agent-platform --field-selector reason=OOMKilling
```

---

## 6. Interpreting failures

| Failure pattern | Likely cause |
|-----------------|--------------|
| `login failed: HTTP 401` | Wrong credentials or identity service down |
| `create_session HTTP 503` | Session-orchestrator not ready / DB pool exhausted |
| `chat POST HTTP 504` | Agent scheduler not responding / sandbox cold-start timeout |
| `SSE stream ended without 'done'` | Agent run crashed; check orchestrator logs |
| `agent error event:` | Worker-level error; check agentscope-ext logs in sandbox |
| High error rate after 1 h | Connection pool exhaustion; check `DB_POOL_SIZE` |

---

## 7. Result archival

After each run, archive:
1. `deploy/loadtest/results/slo_<timestamp>_stats.csv` — Locust request stats
2. `deploy/loadtest/results/slo_<timestamp>_failures.csv` — failure details
3. `deploy/loadtest/results/slo_<timestamp>.html` — full HTML report
4. `deploy/loadtest/results/resource_<timestamp>.csv` — memory/CPU log
5. Screenshot of Grafana memory dashboard (if available)

Store in the project wiki or shared drive under `load-tests/B-M6/<date>/`.
